#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyzotero>=1.6",
#     "requests>=2.31",
#     "urllib3>=2.0",
#     "tenacity>=8.0",
#     "habanero>=1.2",
#     "pyalex>=0.15",
#     "pybliometrics>=3.6",
#     "wiley-tdm>=0.2",
# ]
# ///
"""Enrich Zotero items by downloading missing PDFs and attaching them.

For each journal article in the Zotero library that does not already
have a PDF attached:

  1. Run the PDF-source cascade for the item's DOI (see
     `fetchers.pdf_sources`).
  2. Upload the first PDF found as a child attachment via
     `ZoteroClient.attach_pdf` (pyzotero's `attachment_simple`).
  3. Log the outcome to a CSV (same schema as the legacy log).

Source selection via `--sources`:

    enrich_pdfs.py                         # default automated cascade
    enrich_pdfs.py --sources wiley         # Wiley TDM only
    enrich_pdfs.py --sources browser       # Cloudflare-gated publishers
    enrich_pdfs.py --sources elsevier,pmc  # custom subset

For `--sources browser`, this script drives the per-publisher
`fetchers.browser` handlers directly — a visible Chromium opens, you
solve Cloudflare once per publisher, and each handler's `download()`
method downloads its items using the shared session. The legacy
`fetch_pdfs_browser.py` is still invoked if `--legacy-browser` is
passed, as a rollback option.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# Make `core`, `fetchers`, `zotero_io`, `http_client` importable without
# the PEP 723 runner touching the repo layout.
SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = SCRIPT_DIR.parent
for _p in (str(SCRIPT_DIR), str(SCRIPTS_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fetchers  # noqa: E402
import http_client  # noqa: E402
import zotero_io  # noqa: E402
from core.config_loader import get, require  # noqa: E402

DEFAULT_LOG_CSV = os.path.join("output", "pdf_attach_log.csv")
DEFAULT_CACHE_DIR = os.path.join("output", "pdf_cache")

LOG_FIELDS = ["run_date", "item_key", "doi", "title", "status", "source"]


@dataclass
class Config:
    elsevier_api_key: str = ""
    openalex_api_key: str = ""
    wiley_tdm_token: str = ""
    semantic_scholar_api_key: str = ""
    crossref_mailto: str = ""


def _load_config() -> Config:
    return Config(
        elsevier_api_key=get("elsevier", "api_key", env="ELSEVIER_API_KEY"),
        openalex_api_key=get("openalex", "api_key", env="OPENALEX_API_KEY"),
        wiley_tdm_token=get("wiley", "tdm_token", env="WILEY_TDM_TOKEN"),
        semantic_scholar_api_key=get(
            "semantic_scholar", "api_key", env="SEMANTIC_SCHOLAR_API_KEY",
        ),
        crossref_mailto=get("crossref", "mailto", env="CROSSREF_MAILTO"),
    )


def _open_log(path: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    is_new = not os.path.exists(path)
    fh = open(path, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(fh, fieldnames=LOG_FIELDS)
    if is_new:
        writer.writeheader()
    return fh, writer


def _load_done_dois(path: str) -> set[str]:
    if not os.path.exists(path):
        return set()
    with open(path, newline="", encoding="utf-8") as f:
        return {
            (r.get("doi") or "").strip().lower()
            for r in csv.DictReader(f)
            if r.get("status") == "attached"
        }


def _run_browser_legacy(args: argparse.Namespace) -> int:
    """Rollback path: delegate to the legacy `fetch_pdfs_browser.py`
    under `legacy/` (moved there in v0.3.1).

    Opt-in via `--legacy-browser`. Kept as a fallback while the new
    in-process handlers prove themselves on production libraries.
    """
    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "legacy" / "fetch_pdfs_browser.py"),
        "--log-csv", args.log_csv,
        "--cache-dir", args.cache_dir,
    ]
    if args.publisher:
        cmd.extend(["--publisher", args.publisher])
    if args.filter_keys_file:
        cmd.extend(["--filter-keys-file", args.filter_keys_file])
    print(f"Delegating to fetch_pdfs_browser.py: {' '.join(cmd)}", flush=True)
    return subprocess.call(cmd)


async def _drive_handler(
    handler,
    items: list[dict],
    zot,
    log_writer,
    args: argparse.Namespace,
    run_date: str,
    *,
    on_failure: str = "log",            # "log" | "retry_bucket"
    retry_bucket: list[dict] | None = None,
    prompt_on_first_failure: bool = False,
    on_always_skip=None,                # callable(handler_name) → None
) -> None:
    """Drive one publisher handler across its items.

    `on_failure="log"` keeps the v0.3.x behaviour: per-item failures
    are written as `skipped_no_pdf` CSV rows and the run continues.

    `on_failure="retry_bucket"` (new in v0.4.0) routes per-item
    failures into `retry_bucket` instead of writing a log row; the
    Connector pass later tries the same items via its own path, and
    its final status is the only row that ends up in the log. This
    cleaner chain matches the "only the final outcome is logged"
    design of the v0.4.0 routing model.

    `prompt_on_first_failure=True` fires the Option-4 prompt ONCE per
    handler per run on the first per-item failure. The user picks:
      * k — keep trying direct for remaining items
      * s — skip remaining direct attempts (default)
      * A — same as s, plus invoke `on_always_skip(handler.name)` so
            the caller can persist the publisher to
            `[library] no_access` in config.toml.

    When `on_failure="retry_bucket"` and the user answers `s`/`A`, the
    remaining un-attempted items go straight into the retry bucket
    without re-opening the page — saves 30s × N of timeouts.
    """
    from fetchers.browser import Counter, launch_context
    from fetchers.browser.base import normalise_setup_result

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("ERROR: playwright not installed. Run via `uv run`.", file=sys.stderr)
        return

    display = handler.display_name or handler.name
    print(f"\n{'='*60}\nPublisher: {display} ({len(items)} PDFs)\n{'='*60}",
          flush=True)
    if not items:
        return

    os.makedirs(args.cache_dir, exist_ok=True)

    async with async_playwright() as p:
        ctx = await launch_context(p, args.cache_dir)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        setup_result = normalise_setup_result(
            await handler.setup(page, items[0]["doi"])
        )
        if setup_result in ("skip", "always_skip"):
            # User bailed out before any item ran. "always_skip" also
            # persists the publisher to [library] no_access so future
            # runs don't bother asking.
            if setup_result == "always_skip" and on_always_skip is not None:
                try:
                    on_always_skip(handler.name)
                    print(
                        f"  {display}: persisted to [library] no_access; "
                        f"future runs will skip this handler.",
                        flush=True,
                    )
                except Exception as e:
                    print(
                        f"  WARN: could not persist [library] no_access "
                        f"+= {handler.name!r}: {e}",
                        flush=True,
                    )

            if on_failure == "retry_bucket" and retry_bucket is not None:
                print(
                    f"  Skipping {display}: routing {len(items)} items "
                    f"to the Connector retry bucket.",
                    flush=True,
                )
                retry_bucket.extend(items)
            else:
                print(
                    f"  Skipping {display}: logging {len(items)} items "
                    f"as skipped_no_access.",
                    flush=True,
                )
                for item in items:
                    log_writer.writerow({
                        "run_date": run_date, "item_key": item["item_key"],
                        "doi": item["doi"],
                        "title": (item.get("title") or "")[:70],
                        "status": "skipped_no_access", "source": handler.name,
                    })
            await ctx.close()
            return

        counter = Counter()
        total = len(items)
        import time
        t_start = time.monotonic()

        # Session-scoped skip state (Option-4 prompt). `skip_remaining`
        # True means don't open any more direct attempts for this
        # handler — subsequent items are routed straight to retry.
        prompt_fired = False
        skip_remaining = False

        for idx, item in enumerate(items):
            if skip_remaining:
                # User picked "skip remaining" for this publisher.
                if on_failure == "retry_bucket" and retry_bucket is not None:
                    retry_bucket.append(item)
                continue

            if handler.delay_s > 0:
                await asyncio.sleep(handler.delay_s)
            result = await handler.download(
                page, ctx, item, args.cache_dir,
                counter=counter, total=total, t_start=t_start,
            )
            doi = item["doi"]
            title = (item.get("title") or "")[:70]

            if result is None:
                # Per-item download failure.
                if prompt_on_first_failure and not prompt_fired:
                    prompt_fired = True
                    remaining = len(items) - idx - 1
                    answer = await asyncio.to_thread(
                        _prompt_on_first_failure,
                        handler, remaining, args,
                    )
                    if answer == "always_skip":
                        skip_remaining = True
                        if on_always_skip is not None:
                            try:
                                on_always_skip(handler.name)
                            except Exception as e:
                                print(
                                    f"  WARN: could not persist "
                                    f"[library] no_access += "
                                    f"{handler.name!r}: {e}",
                                    flush=True,
                                )
                    elif answer == "skip":
                        skip_remaining = True
                    # "keep" → keep looping, same as before.
                if on_failure == "retry_bucket" and retry_bucket is not None:
                    retry_bucket.append(item)
                else:
                    log_writer.writerow({
                        "run_date": run_date, "item_key": item["item_key"],
                        "doi": doi, "title": title,
                        "status": "skipped_no_pdf", "source": handler.name,
                    })
                continue

            pdf_path, source_url = result
            if args.dry_run:
                log_writer.writerow({
                    "run_date": run_date, "item_key": item["item_key"],
                    "doi": doi, "title": title,
                    "status": "dry_run", "source": handler.name,
                })
                continue

            if not item["item_key"]:
                print(f"  [{doi}] no Zotero item key — skipping upload", flush=True)
                log_writer.writerow({
                    "run_date": run_date, "item_key": "",
                    "doi": doi, "title": title,
                    "status": "downloaded_no_item", "source": handler.name,
                })
                continue

            try:
                zot.attach_pdf(item["item_key"], str(pdf_path))
                log_writer.writerow({
                    "run_date": run_date, "item_key": item["item_key"],
                    "doi": doi, "title": title,
                    "status": "attached", "source": handler.name,
                })
            except Exception as e:
                print(f"  [{doi}] upload failed: {e}", flush=True)
                log_writer.writerow({
                    "run_date": run_date, "item_key": item["item_key"],
                    "doi": doi, "title": title,
                    "status": "upload_failed", "source": handler.name,
                })

        print(
            f"\n  Total: {counter.ok} new, {counter.cached} cached, "
            f"{counter.failed} failed",
            flush=True,
        )
        await ctx.close()


def _prompt_on_first_failure(
    handler, remaining: int, args: argparse.Namespace,
) -> str:
    """Option-4 prompt. Returns one of 'keep' | 'skip' | 'always_skip'.

    Non-TTY stdin or `--on-first-failure=<value>` skips the prompt
    entirely and returns the configured answer. Default for
    non-interactive is 'skip' — matches the interactive default on
    Enter, so piped runs don't block.
    """
    override = getattr(args, "on_first_failure", "")
    if override:
        return override
    if not sys.stdin.isatty():
        return "skip"
    display = handler.display_name or handler.name
    print(
        f"\n{display} failed to download the last item.\n"
        f"{remaining} more {display} item"
        f"{'s are' if remaining != 1 else ' is'} queued for this run. "
        f"What do you want to do?\n"
        f"  [k] Keep trying {display} direct for the remaining items\n"
        f"  [s] Skip remaining {display} items this run (default)\n"
        f"  [A] Always skip {display} direct — write to config so "
        f"future runs jump straight to the Connector fallback",
        flush=True,
    )
    try:
        with open("/dev/tty") as tty:
            sys.stdout.write("> ")
            sys.stdout.flush()
            raw = tty.readline()
    except Exception:
        raw = sys.stdin.readline()
    answer = (raw or "").strip()
    if answer == "k" or answer.lower() == "keep":
        return "keep"
    if answer == "A" or answer.lower() in ("always", "always_skip"):
        return "always_skip"
    return "skip"                       # empty (Enter), "s", or anything else


async def _drive_connector(
    handler,
    items: list[dict],
    zot,
    log_writer,
    args: argparse.Namespace,
    run_date: str,
) -> None:
    """Drive the Zotero Connector handler across a single batch.

    Differs from `_drive_handler`:
      * loads the Connector extension into a separate Chromium profile,
      * waits for the extension's service worker,
      * pre-flight-pings Zotero Desktop (aborts cleanly if offline),
      * calls `handler.download_and_attach(...)` per item — the
        handler saves to Zotero itself (no local PDF upload step).
    """
    from fetchers.browser import (
        Counter,
        launch_context,
        ping_zotero_desktop,
        wait_for_service_worker,
    )
    from fetchers.browser.base import normalise_setup_result

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("ERROR: playwright not installed. Run via `uv run`.",
              file=sys.stderr)
        return

    display = handler.display_name or handler.name
    print(f"\n{'='*60}\nPublisher: {display} ({len(items)} items)\n{'='*60}",
          flush=True)
    if not items:
        return

    # Zotero Desktop pre-flight.
    import requests as _requests
    if not ping_zotero_desktop(_requests.Session()):
        print(
            "  ERROR: Zotero Desktop is not running (or its connector\n"
            "  server on localhost:23119 is disabled).  Logging "
            f"{len(items)} items as connector_zotero_unavailable.",
            flush=True,
        )
        for it in items:
            log_writer.writerow({
                "run_date": run_date, "item_key": it["item_key"],
                "doi": it["doi"],
                "title": (it.get("title") or "")[:70],
                "status": "connector_zotero_unavailable",
                "source": handler.name,
            })
        return

    # Library-selection pre-flight. Connector saves go to whichever
    # library Zotero Desktop has selected in its left pane — if that
    # doesn't match our target group, every save lands in the wrong
    # library and our subsequent poll never finds the new item.
    # Compare by cloud group ID (unique); fall back to library name
    # if the response doesn't carry the group ID (older Zotero).
    selected = zot.selected_local_library()
    if selected is not None:
        lib_name = selected.get("libraryName") or "(unknown)"
        # Zotero Desktop may expose the cloud group ID under either
        # `groupID` or `groupId` depending on version — accept both.
        cloud_gid = selected.get("groupID") or selected.get("groupId")
        matched = False
        match_reason = ""
        if cloud_gid is not None:
            matched = str(cloud_gid) == str(zot.group_id)
            match_reason = f"group ID {cloud_gid}"
        else:
            target_name = zot.group_name()
            if target_name:
                matched = lib_name == target_name
                match_reason = (
                    f"name-based comparison (target {target_name!r})"
                )

        if matched:
            print(
                f"\n  Zotero Desktop has {lib_name!r} selected — "
                f"matches target {match_reason}. Saves will land here.",
                flush=True,
            )
        else:
            target_desc = (
                zot.group_name() or f"group {zot.group_id}"
            )
            detail = (
                f"Desktop reports group ID {cloud_gid}, target is "
                f"{zot.group_id}."
                if cloud_gid is not None
                else (
                    "Zotero Desktop did not report a group ID for the\n"
                    "  selected library; the pipeline could not match\n"
                    "  it against the target by ID."
                )
            )
            print(
                f"\n  Zotero Desktop has {lib_name!r} selected, but the\n"
                f"  pipeline is working on {target_desc!r} (group "
                f"{zot.group_id}).\n  {detail}\n"
                f"  Connector saves go to the selected library, not the\n"
                f"  target — every save will land in the wrong place\n"
                f"  unless you fix this.",
                flush=True,
            )
            if sys.stdin.isatty():
                confirm = input(
                    f"  Save to {lib_name!r} anyway? [y/N] "
                ).strip().lower()
                if confirm not in ("y", "yes"):
                    print(
                        "  Aborting. In Zotero Desktop, click on\n"
                        f"  {target_desc!r} in the left pane, then re-run.",
                        flush=True,
                    )
                    for it in items:
                        log_writer.writerow({
                            "run_date": run_date, "item_key": it["item_key"],
                            "doi": it["doi"],
                            "title": (it.get("title") or "")[:70],
                            "status": "connector_wrong_library",
                            "source": handler.name,
                        })
                    return
    else:
        print(
            "\n  WARN: could not determine Zotero Desktop's selected\n"
            "  library. Make sure your target group is selected in\n"
            "  Zotero Desktop's left pane before continuing.",
            flush=True,
        )

    # Extension pre-flight — surfaced in setup() too, but a clean
    # bail-out here avoids opening Chromium for nothing.
    if handler.extension_path is None:
        print(
            "  ERROR: Zotero Connector extension not found. Install "
            "from https://www.zotero.org/download/connectors/ in Chrome,\n"
            "  then re-run the setup wizard.",
            flush=True,
        )
        for it in items:
            log_writer.writerow({
                "run_date": run_date, "item_key": it["item_key"],
                "doi": it["doi"],
                "title": (it.get("title") or "")[:70],
                "status": "connector_extension_missing",
                "source": handler.name,
            })
        return

    os.makedirs(args.cache_dir, exist_ok=True)

    async with async_playwright() as p:
        ctx = await launch_context(
            p, args.cache_dir, extensions=[handler.extension_path],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        setup_result = normalise_setup_result(
            await handler.setup(page, items[0]["doi"])
        )
        if setup_result != "proceed":
            for it in items:
                log_writer.writerow({
                    "run_date": run_date, "item_key": it["item_key"],
                    "doi": it["doi"],
                    "title": (it.get("title") or "")[:70],
                    "status": "connector_setup_failed",
                    "source": handler.name,
                })
            await ctx.close()
            return

        service_worker = await wait_for_service_worker(ctx, timeout_s=15)
        if service_worker is None:
            print(
                "  ERROR: Connector service worker did not start within 15s.",
                flush=True,
            )
            for it in items:
                log_writer.writerow({
                    "run_date": run_date, "item_key": it["item_key"],
                    "doi": it["doi"],
                    "title": (it.get("title") or "")[:70],
                    "status": "connector_sw_timeout",
                    "source": handler.name,
                })
            await ctx.close()
            return

        counter = Counter()
        total = len(items)
        import time
        t_start = time.monotonic()

        # Group items by effective host so reCAPTCHA / EZproxy logins
        # only need to be solved once per platform instead of once per
        # item. `_effective_host` unwraps EZproxy URLs so jstor links
        # under ezproxy.jyu.fi cluster with jstor links that aren't.
        from fetchers.library_resolver import _effective_host
        items_sorted = sorted(
            items,
            key=lambda it: _effective_host(it.get("sfx_target_url", "")),
        )

        current_host = None
        for item in items_sorted:
            host = _effective_host(item.get("sfx_target_url", ""))
            if host != current_host:
                current_host = host
                remaining_on_host = sum(
                    1 for it in items_sorted
                    if _effective_host(it.get("sfx_target_url", "")) == host
                )
                print(
                    f"\n  ══ Batch: {host or '(unknown host)'} "
                    f"({remaining_on_host} "
                    f"item{'s' if remaining_on_host != 1 else ''}) ══\n"
                    f"  Solve any login / reCAPTCHA once for this host; "
                    f"subsequent items reuse the session.",
                    flush=True,
                )

            if handler.delay_s > 0:
                await asyncio.sleep(handler.delay_s)
            ok = await handler.download_and_attach(
                page, ctx, service_worker, item, zot,
                counter=counter, total=total, t_start=t_start,
            )
            doi = item["doi"]
            title = (item.get("title") or "")[:70]
            # Host-scoped skips (user pressed 's' at the first-item
            # prompt on this host) are a distinct status from "the
            # Connector tried to save but failed".
            item_host = _effective_host(item.get("sfx_target_url", ""))
            skipped_by_user = item_host in getattr(
                handler, "_skipped_hosts", set(),
            )
            if ok:
                status = "attached_via_connector"
            elif skipped_by_user:
                status = "skipped_by_user"
            else:
                status = "connector_save_failed"
            log_writer.writerow({
                "run_date": run_date, "item_key": item["item_key"],
                "doi": doi, "title": title,
                "status": status, "source": handler.name,
            })

        print(
            f"\n  Total: {counter.ok} new, {counter.failed} failed",
            flush=True,
        )
        await ctx.close()


def _run_browser_in_process(
    to_process: list[dict],
    zot,
    log_writer,
    args: argparse.Namespace,
    run_date: str,
    *,
    connector_only: bool = False,
    session=None,
    config=None,
) -> int:
    """Classify, drive direct handlers, then drive the Connector fallback.

    Four passes:

      Pass 1 — classify each item. SFX dual lookup tells us whether
               the library has any full-text route and whether the
               per-publisher handler's domain is in range. Three
               outcomes per item:
                 Case 3: direct domain in the date-filtered list →
                         `items_by_pub[handler.name]`.
                 Case 2: direct domain in the ignore-date list only →
                         skip direct (library has publisher but not
                         this year); route to `connector_upfront`.
                 Case 1: direct domain in neither (library has no
                         relationship with this publisher) → try
                         direct anyway (user might be a member).
               Items with no direct handler or in `library.no_access`
               go to `connector_upfront` directly.

      Pass 2 — drive each direct handler with the Option-4 failure
               prompt. Failures feed `connector_retry`.

      Pass 3 — assign an SFX target URL (date-filtered, highest-
               priority platform) to each Connector item. Items with
               no Query-B target are logged as `skipped_no_pdf` /
               `skipped_no_library_coverage` and dropped.

      Pass 4 — single Connector session for the remaining list.

    `connector_only=True` bypasses Pass 1/2 entirely: every DOI goes
    to the Connector upfront bucket. Used by `--sources connector`
    for targeted validation runs.
    """
    from collections import defaultdict
    from urllib.parse import urlparse

    from core import config_writer
    from fetchers.browser import (
        ZoteroConnectorHandler,
        all_handlers,
        resolve_by_doi,
        resolve_by_host,
    )
    from fetchers.doi_resolver import DoiResolverCache, resolve_doi
    from fetchers.library_resolver import (
        SFX_PLATFORM_PRIORITY,
        first_fulltext_target_preferred,
        load_from_config,
        sfx_lookup_dual,
    )
    from fetchers.library_resolver import (
        _target_matches_domains as target_matches_domains,
    )

    direct_handlers = all_handlers()
    handler_by_name = {h.name: h for h in direct_handlers}

    # DOI → canonical URL resolver via Crossref. Catches prefix
    # collisions: ETAP's 10.1111/etap.* DOIs route to Sage (not
    # Wiley) since the journal's migration circa 2021.
    from habanero import Crossref
    crossref_client = Crossref(
        mailto=get("crossref", "mailto", env="CROSSREF_MAILTO"),
    )
    doi_cache = DoiResolverCache(args.cache_dir)

    # Resolver session — no Crossref mailto, no tenacity retries
    # (competing timeouts lead to visible stalls).
    import requests as _requests
    resolver_session = _requests.Session()
    resolver_cfg = load_from_config(resolver_session, args.cache_dir)

    # [library] no_access → short-circuit these direct handlers
    # unconditionally. Populated at runtime by the failure prompt's
    # "Always skip" answer; editable via the setup wizard. Stored as
    # a TOML list, so we read via load_config() directly — get() only
    # returns strings.
    from core.config_loader import load_config
    cfg_no_access = load_config().get("library", {}).get("no_access", [])
    if isinstance(cfg_no_access, list):
        no_access = {str(s).strip() for s in cfg_no_access if s}
    elif isinstance(cfg_no_access, str):
        no_access = {s.strip() for s in cfg_no_access.split(",") if s.strip()}
    else:
        no_access = set()

    # Pass 2 API retry: the prefix-filtering API sources (Wiley TDM,
    # Elsevier, Springer). When Crossref resolution reveals a DOI
    # whose canonical host matches one of these sources, but whose
    # prefix Pass 1 wouldn't have matched, we call the source with
    # `bypass_prefix_filter=True` before resorting to the browser.
    # Skipped in connector_only mode (targeted validation).
    pass2_api_sources: list = []
    if not connector_only and session is not None and config is not None:
        try:
            pass2_api_sources = [
                s for s in fetchers.pdf_sources(session, config)
                if getattr(s, "direct_access_domains", ())
            ]
        except Exception as e:
            print(f"  WARN: Pass 2 API retry init failed: {e}", flush=True)
            pass2_api_sources = []

    # ------------------------------------------------------------------
    # Pass 1 — classify.
    # ------------------------------------------------------------------

    items_by_pub: dict[str, list[dict]] = defaultdict(list)
    connector_upfront: list[dict] = []
    no_handler_count = 0

    if resolver_cfg is not None and not connector_only:
        print(
            f"\nChecking library access via {resolver_cfg.openurl_base}...",
            flush=True,
        )

    for zot_item in to_process:
        doi = (zot_item.get("data", {}).get("DOI") or "").strip().lower()
        if not doi:
            continue
        entry = {
            "doi": doi,
            "item_key": zot_item.get("key", ""),
            "title": zot_item.get("data", {}).get("title", ""),
        }

        if connector_only:
            connector_upfront.append(entry)
            continue

        # Route by DOI's canonical Crossref URL first — covers
        # migrated journals (e.g. ETAP moved Wiley→Sage; its
        # 10.1111/etap.* DOIs now resolve to journals.sagepub.com,
        # not Wiley). Fall back to DOI-prefix matching when Crossref
        # is unreachable or returns no URL.
        direct = None
        resolution = resolve_doi(
            doi, crossref=crossref_client, cache=doi_cache,
        )
        resolved_host = ""
        if resolution and resolution.url:
            resolved_host = urlparse(resolution.url).hostname or ""

        # Pass 2 API retry: if the resolved host matches a
        # prefix-filtering API source (Wiley TDM / Elsevier / Springer),
        # invoke it with `bypass_prefix_filter=True`. Catches DOIs
        # whose prefix Pass 1 didn't match but whose canonical host
        # does — e.g. a journal migrated onto Elsevier while keeping
        # its original non-10.1016 DOI prefix.
        if resolved_host and pass2_api_sources:
            for src in pass2_api_sources:
                if not target_matches_domains(
                    f"https://{resolved_host}/", src.direct_access_domains,
                ):
                    continue
                try:
                    retry_result = src.fetch_pdf(
                        doi, cache_dir=args.cache_dir,
                        bypass_prefix_filter=True,
                    )
                except Exception as e:
                    print(
                        f"  Pass 2 API retry via {src.name} errored: "
                        f"{str(e)[:80]}",
                        flush=True,
                    )
                    retry_result = None
                if retry_result is None:
                    continue
                pdf_path, _source_url = retry_result
                title70 = (entry.get("title") or "")[:70]
                if args.dry_run:
                    log_writer.writerow({
                        "run_date": run_date, "item_key": entry["item_key"],
                        "doi": doi, "title": title70,
                        "status": "dry_run", "source": src.name,
                    })
                    print(
                        f"  Pass 2 API retry hit {src.name} [dry-run] "
                        f"{title70}", flush=True,
                    )
                    break
                try:
                    zot.attach_pdf(entry["item_key"], str(pdf_path))
                    log_writer.writerow({
                        "run_date": run_date, "item_key": entry["item_key"],
                        "doi": doi, "title": title70,
                        "status": "attached", "source": src.name,
                    })
                    print(
                        f"  Pass 2 API retry → attached via {src.name} "
                        f"{title70}", flush=True,
                    )
                except Exception as e:
                    log_writer.writerow({
                        "run_date": run_date, "item_key": entry["item_key"],
                        "doi": doi, "title": title70,
                        "status": "upload_failed", "source": src.name,
                    })
                    print(
                        f"  Pass 2 API retry {src.name} → upload failed: "
                        f"{e}", flush=True,
                    )
                break
            else:
                retry_result = None

            # If any matching source attached (or hit dry-run), skip
            # further routing for this item.
            if retry_result is not None:
                continue

        if resolved_host:
            direct = resolve_by_host(resolved_host, direct_handlers)
        if direct is None:
            direct = resolve_by_doi(doi, direct_handlers)

        if direct and direct.name in no_access:
            direct = None

        if direct is None:
            no_handler_count += 1
            connector_upfront.append(entry)
            continue

        # Classify Case 1 / 2 / 3 via dual SFX lookup.
        if resolver_cfg is not None:
            dual = sfx_lookup_dual(doi, resolver_cfg)
            domains = direct.direct_access_domains
            if domains:
                in_range = any(
                    target_matches_domains(u, domains) for u in dual.in_range
                )
                in_any = any(
                    target_matches_domains(u, domains) for u in dual.any_range
                )
                if in_range:
                    pass                           # Case 3 — run direct
                elif in_any:
                    # Case 2 — skip direct, try Connector via Query B.
                    connector_upfront.append(entry)
                    continue
                # else Case 1 — try direct anyway.

        items_by_pub[direct.name].append(entry)

    if args.publisher:
        items_by_pub = {
            k: v for k, v in items_by_pub.items() if k == args.publisher
        }
        # --publisher restricts direct; drop Connector items to avoid
        # surprising the caller with a second session.
        connector_upfront = []

    # Print the queue.
    total_direct = sum(len(v) for v in items_by_pub.values())
    if total_direct or connector_upfront:
        print("\nBrowser queue:", flush=True)
        for name, pub_items in items_by_pub.items():
            display = handler_by_name[name].display_name or name
            print(
                f"  • {display} (direct): {len(pub_items)} "
                f"paper{'' if len(pub_items) == 1 else 's'}",
                flush=True,
            )
        if connector_upfront:
            print(
                f"  • Zotero Connector (upfront): "
                f"{len(connector_upfront)} "
                f"paper{'' if len(connector_upfront) == 1 else 's'}",
                flush=True,
            )
    else:
        print("\nNothing to do via the browser path.", flush=True)
        return 0

    # ------------------------------------------------------------------
    # Pass 2 — drive direct handlers, collect Connector retries.
    # ------------------------------------------------------------------

    connector_retry: list[dict] = []

    async def _run_direct() -> None:
        for name, pub_items in items_by_pub.items():
            handler = handler_by_name[name]
            await _drive_handler(
                handler, pub_items, zot, log_writer, args, run_date,
                on_failure="retry_bucket",
                retry_bucket=connector_retry,
                prompt_on_first_failure=True,
                on_always_skip=lambda n: config_writer.append_to_list(
                    "library", "no_access", n,
                ),
            )

    if items_by_pub and not connector_only:
        asyncio.run(_run_direct())

    # ------------------------------------------------------------------
    # Pass 3 — assign SFX target URLs to Connector items. Use Query B
    # (date-filtered) so we never hand the Connector a target that
    # SFX knows is out of coverage.
    # ------------------------------------------------------------------

    connector_items: list[dict] = []
    skipped_no_target = 0
    origins = (
        [(it, "upfront") for it in connector_upfront]
        + [(it, "retry") for it in connector_retry]
    )
    for it, origin in origins:
        target = None
        if resolver_cfg is not None:
            # Query B only (date-filtered). When Query B is empty,
            # we do NOT fall back to Query A. The cache data against
            # JYU's SFX (see sfx_cache.json) shows Query A commonly
            # returns targets the user genuinely can't access — the
            # ignore-date list is "SFX knows the journal via these
            # providers", not "you can download this DOI now". Using
            # it as a fallback wastes user time on paywalls.
            target = first_fulltext_target_preferred(
                it["doi"], resolver_cfg,
                priority=SFX_PLATFORM_PRIORITY,
                in_range_only=True,
            )
        if target:
            connector_items.append({**it, "sfx_target_url": target})
        else:
            status = (
                "skipped_no_library_coverage"
                if origin == "upfront" else "skipped_no_pdf"
            )
            log_writer.writerow({
                "run_date": run_date, "item_key": it["item_key"],
                "doi": it["doi"],
                "title": (it.get("title") or "")[:70],
                "status": status, "source": "connector",
            })
            skipped_no_target += 1

    if skipped_no_target:
        print(
            f"\n  {skipped_no_target} item"
            f"{'s' if skipped_no_target != 1 else ''} had no Query-B "
            f"full-text target — logged without opening the Connector.",
            flush=True,
        )

    # ------------------------------------------------------------------
    # Pass 4 — single Connector session for upfront + retry items.
    # ------------------------------------------------------------------

    if connector_items:
        connector_handler = ZoteroConnectorHandler(
            extension_path=get(
                "zotero_connector", "extension_dir",
                env="ZOTERO_CONNECTOR_DIR",
            ) or None,
        )
        asyncio.run(_drive_connector(
            connector_handler, connector_items, zot, log_writer,
            args, run_date,
        ))

    return 0


def _try_cascade(
    item: dict,
    sources: list,
    cache_dir: str,
) -> tuple[Path, str] | None:
    """Try each PDF fetcher in priority order. Returns (path, source_name)
    on the first hit."""
    doi = (item.get("data", {}).get("DOI") or "").strip()
    if not doi:
        return None
    for src in sources:
        try:
            result = src.fetch_pdf(doi, cache_dir=cache_dir)
        except NotImplementedError:
            continue
        except Exception as e:
            print(f"    {src.name}: {e}", flush=True)
            continue
        if result is None:
            continue
        path, _ = result
        return path, src.name
    return None


def _run_api_cascade(
    to_process: list[dict],
    sources: list,
    args: argparse.Namespace,
    run_date: str,
    zot,
    log_writer,
) -> tuple[int, int, int]:
    """Run the API cascade (Pass 1) against `to_process`.

    Downloads in parallel, then uploads serially via
    `ZoteroClient.attach_pdf`. Writes one CSV row per item (status in
    {attached, skipped_no_pdf, upload_failed, dry_run}). Returns the
    counter triple `(attached, no_pdf, failed)` so the caller can
    print a summary — important for `--all` where this runs before
    Pass 2 and the Pass-1 summary must appear before Pass-2 banners.
    """
    attached = no_pdf = failed = 0

    print(f"\n  Downloading PDFs ({args.workers} threads)...", flush=True)
    results: list[tuple[dict, tuple[Path, str] | None]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(_try_cascade, it, sources, args.cache_dir): it
            for it in to_process
        }
        for fut in as_completed(futures):
            item = futures[fut]
            try:
                res = fut.result()
            except Exception as e:
                print(f"  [{item['key']}] cascade error: {e}", flush=True)
                res = None
            results.append((item, res))
            d = item["data"]
            title70 = (d.get("title") or "")[:70]
            if res is not None:
                path, src_name = res
                size_kb = path.stat().st_size // 1024
                print(
                    f"  [{len(results)}/{len(to_process)}] {title70:<70} "
                    f"({src_name}) {size_kb}KB",
                    flush=True,
                )
            else:
                print(
                    f"  [{len(results)}/{len(to_process)}] {title70:<70} "
                    f"no PDF",
                    flush=True,
                )

    found = [(item, r) for item, r in results if r is not None]
    not_found = [item for item, r in results if r is None]

    print(
        f"\n  Downloaded: {len(found)}, Not found: {len(not_found)}",
        flush=True,
    )

    for item in not_found:
        d = item["data"]
        log_writer.writerow({
            "run_date": run_date, "item_key": item["key"],
            "doi": d.get("DOI", ""),
            "title": (d.get("title") or "")[:70],
            "status": "skipped_no_pdf", "source": "",
        })
        no_pdf += 1

    if found and not args.dry_run:
        print(f"  Uploading {len(found)} PDFs to Zotero...\n", flush=True)

    for j, (item, res) in enumerate(found, 1):
        d = item["data"]
        key = item["key"]
        doi = d.get("DOI", "")
        title = (d.get("title") or "")[:70]
        path, src_name = res
        print(
            f"  [{j}/{len(found)}] {title:<70} ({src_name})",
            end=" ", flush=True,
        )
        if args.dry_run:
            print("[dry-run]", flush=True)
            log_writer.writerow({
                "run_date": run_date, "item_key": key, "doi": doi,
                "title": title, "status": "dry_run", "source": src_name,
            })
            attached += 1
            continue
        try:
            zot.attach_pdf(key, str(path))
            print("→ attached", flush=True)
            log_writer.writerow({
                "run_date": run_date, "item_key": key, "doi": doi,
                "title": title, "status": "attached", "source": src_name,
            })
            attached += 1
        except Exception as e:
            print(f"→ upload failed: {e}", flush=True)
            log_writer.writerow({
                "run_date": run_date, "item_key": key, "doi": doi,
                "title": title, "status": "upload_failed",
                "source": src_name,
            })
            failed += 1

    return attached, no_pdf, failed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sources", default="",
        help="Comma-separated fetcher names to use. Special values: "
             "'wiley' (Wiley TDM only), 'browser' (full browser pass: "
             "direct handlers + Connector fallback), 'connector' "
             "(Connector handler only; useful for targeted validation). "
             "Default: automated cascade "
             "(elsevier+springer+crossref+pmc+openalex+unpaywall).",
    )
    parser.add_argument(
        "--publisher",
        help="(browser mode only) Restrict to one publisher key.",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Download PDFs, do not upload to Zotero.")
    parser.add_argument("--log-csv", default=DEFAULT_LOG_CSV,
                        help=f"Path to log CSV (default: {DEFAULT_LOG_CSV}).")
    parser.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR,
                        help=f"PDF cache directory (default: {DEFAULT_CACHE_DIR}).")
    parser.add_argument("--workers", type=int, default=6,
                        help="Parallel download threads (default: 6).")
    parser.add_argument(
        "--filter-keys-file",
        help="Path to a text file of Zotero item keys (one per line) "
             "to restrict processing to.",
    )
    zotero_io.add_library_args(parser)
    parser.add_argument(
        "--legacy-browser", action="store_true",
        help="(browser mode) Delegate to the legacy fetch_pdfs_browser.py "
             "subprocess instead of the new in-process handlers. Rollback "
             "option while the new handlers prove themselves.",
    )
    parser.add_argument(
        "--on-first-failure", default="",
        choices=("", "keep", "skip", "always_skip"),
        help="Answer for the per-publisher failure prompt in non-interactive "
             "runs. Default (empty) asks on a TTY and uses 'skip' when "
             "stdin is piped. 'always_skip' also writes the publisher to "
             "[library] no_access in config.toml.",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Run Pass 1 (API cascade) and Pass 2 (browser + Connector) in "
             "one invocation. Pass 2 only processes items Pass 1 couldn't "
             "attach. Equivalent to running enrich_pdfs.py then "
             "enrich_pdfs.py --sources browser. Cannot be combined with "
             "--sources.",
    )
    args = parser.parse_args()

    if args.all and args.sources:
        print("ERROR: --all cannot be combined with --sources.",
              file=sys.stderr)
        return 2

    source_names = [s.strip() for s in args.sources.split(",") if s.strip()]

    # Rollback path: old subprocess-based browser fetcher.
    if source_names == ["browser"] and args.legacy_browser:
        return _run_browser_legacy(args)

    os.makedirs(args.cache_dir, exist_ok=True)
    run_date = date.today().isoformat()
    done_dois = _load_done_dois(args.log_csv)

    config = _load_config()
    session = http_client.build_session(mailto=config.crossref_mailto)

    # Validate Zotero config via require() — surfaces a clear error.
    require("zotero", "api_key", env="ZOTERO_API_KEY")
    if not getattr(args, "user", False) and not args.group:
        try:
            zot = zotero_io.ZoteroClient.from_config(group_id=None)
        except zotero_io.GroupSelectionRequired as e:
            print(zotero_io.format_group_selection_error(e.groups), file=sys.stderr)
            return 2
    else:
        zot = zotero_io.ZoteroClient.from_args(args)

    print("Fetching Zotero items...", end=" ", flush=True)
    all_items = zot.journal_articles()
    print(f"{len(all_items)} journal articles.", flush=True)

    if args.filter_keys_file:
        with open(args.filter_keys_file) as f:
            target = {line.strip() for line in f if line.strip()}
        all_items = [it for it in all_items if it["key"] in target]
        print(f"  After --filter-keys-file: {len(all_items)} items.", flush=True)

    # Items with DOI that haven't already been attached
    candidates = [
        it for it in all_items
        if (doi := (it.get("data", {}).get("DOI") or "").strip())
        and doi.lower() not in done_dois
    ]
    print(f"Items not yet processed: {len(candidates)}", flush=True)

    print("Checking for existing PDF attachments...", end=" ", flush=True)
    pdf_map = zot.pdf_map()
    to_process: list[dict] = []
    stubs_deleted = 0
    for it in candidates:
        key = it["key"]
        has_real, stubs = pdf_map.get(key, (False, []))
        for stub_key in stubs:
            try:
                zot.delete_item(stub_key)
                stubs_deleted += 1
            except Exception as e:
                print(f"  stub delete {stub_key} failed: {e}", flush=True)
        if not has_real:
            to_process.append(it)
    print(
        f"{len(to_process)} items without real PDF"
        + (f" ({stubs_deleted} stubs deleted)" if stubs_deleted else "") + ".",
        flush=True,
    )
    if not to_process:
        return 0

    # Browser path: drives fetchers.browser handlers in-process. The
    # `sources` list is ignored here — handlers are picked per-publisher.
    # `--sources connector` skips Pass 1/2 and sends every item
    # directly to the Connector (useful for targeted validation).
    if source_names in (["browser"], ["connector"]):
        log_fh, log_writer = _open_log(args.log_csv)
        try:
            return _run_browser_in_process(
                to_process, zot, log_writer, args, run_date,
                connector_only=(source_names == ["connector"]),
                session=session, config=config,
            )
        finally:
            log_fh.close()

    # --all: run API cascade first, then re-read pdf_map for residuals
    # and run the browser pipeline.
    if args.all:
        print("\n--- Pass 1: API cascade ---", flush=True)
        sources = fetchers.pdf_sources(session, config)
        print(f"Active fetchers: {[s.name for s in sources]}", flush=True)

        log_fh, log_writer = _open_log(args.log_csv)
        try:
            attached, no_pdf, failed = _run_api_cascade(
                to_process, sources, args, run_date, zot, log_writer,
            )
        finally:
            log_fh.close()
        print(
            f"\n  Pass 1 summary: attached={attached}, "
            f"no-pdf={no_pdf}, failed={failed}",
            flush=True,
        )

        # Pass 2 residuals — re-read pdf_map so items Pass 1 attached
        # drop out automatically.
        print("\n--- Pass 2: browser + Connector ---", flush=True)
        updated_pdf_map = zot.pdf_map()
        residuals = [
            it for it in to_process
            if not updated_pdf_map.get(it["key"], (False, []))[0]
        ]
        print(
            f"  {len(residuals)} items still missing PDF after Pass 1.",
            flush=True,
        )
        if not residuals:
            return 0

        log_fh, log_writer = _open_log(args.log_csv)
        try:
            return _run_browser_in_process(
                residuals, zot, log_writer, args, run_date,
                session=session, config=config,
            )
        finally:
            log_fh.close()

    # Default / explicit-sources path: API cascade only.
    sources = fetchers.pdf_sources(
        session, config, names=source_names if source_names else None,
    )
    if not sources:
        print(f"ERROR: no PDF fetchers matched --sources={args.sources!r}",
              file=sys.stderr)
        return 2
    print(f"Active fetchers: {[s.name for s in sources]}", flush=True)

    log_fh, log_writer = _open_log(args.log_csv)
    try:
        attached, no_pdf, failed = _run_api_cascade(
            to_process, sources, args, run_date, zot, log_writer,
        )
    finally:
        log_fh.close()
    print(
        f"\nDone. attached={attached}, no-pdf={no_pdf}, failed={failed}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
