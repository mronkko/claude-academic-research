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

from core.config_loader import get, require  # noqa: E402

import fetchers  # noqa: E402
import http_client  # noqa: E402
import zotero_io  # noqa: E402

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
) -> None:
    """Open a browser for one publisher, run setup(), then download each
    item. Uploads successful PDFs to Zotero and appends CSV rows inline.

    Concurrency uses the handler's `concurrency` attribute; for page-nav
    handlers this is always 1 (they share a single page). Delay comes
    from `handler.delay_s`.
    """
    from fetchers.browser import Counter, launch_context

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

        proceed = await handler.setup(page, items[0]["doi"])
        if not proceed:
            print(
                f"  Skipping {display}: user indicated no PDF access from "
                f"this session. Logging {len(items)} items as "
                f"skipped_no_access.",
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

        # Serial processing — almost all browser handlers drive a single
        # page, and request-mode handlers still share the same Chromium.
        # The `concurrency` attribute is informational for now; revisit
        # once we have a handler that genuinely benefits from parallelism.
        for item in items:
            if handler.delay_s > 0:
                await asyncio.sleep(handler.delay_s)
            result = await handler.download(
                page, ctx, item, args.cache_dir,
                counter=counter, total=total, t_start=t_start,
            )
            doi = item["doi"]
            title = (item.get("title") or "")[:70]

            if result is None:
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


def _run_browser_in_process(
    to_process: list[dict],
    zot,
    log_writer,
    args: argparse.Namespace,
    run_date: str,
) -> int:
    """Bucket items by publisher and drive each handler in a single
    browser context."""
    from collections import defaultdict

    from fetchers.browser import all_handlers, resolve_by_doi

    handlers = all_handlers()
    handler_by_name = {h.name: h for h in handlers}

    items_by_pub: dict[str, list[dict]] = defaultdict(list)
    unsupported = 0
    for zot_item in to_process:
        doi = (zot_item.get("data", {}).get("DOI") or "").strip().lower()
        if not doi:
            continue
        h = resolve_by_doi(doi, handlers)
        if h is None:
            unsupported += 1
            continue
        items_by_pub[h.name].append({
            "doi": doi,
            "item_key": zot_item.get("key", ""),
            "title": zot_item.get("data", {}).get("title", ""),
        })

    if args.publisher:
        # Restrict to one publisher — useful for debugging a single
        # handler after a flow change.
        items_by_pub = {
            k: v for k, v in items_by_pub.items() if k == args.publisher
        }

    if not items_by_pub:
        print(
            "No items matched any browser-flow publisher "
            f"({unsupported} items had no matching handler).",
            flush=True,
        )
        return 0

    # Library SFX pre-flight: if the user has configured a link
    # resolver, drop items the library has no full-text route for —
    # saves the Chromium window entirely when a publisher's bucket is
    # fully inaccessible.
    from fetchers.library_resolver import (
        has_fulltext_access,
        load_from_config,
    )
    # Separate requests session for SFX — no Crossref mailto needed,
    # and we don't want tenacity retries competing with SFX's own
    # shorter timeouts.
    import requests as _requests
    resolver_session = _requests.Session()
    resolver_cfg = load_from_config(resolver_session, args.cache_dir)
    if resolver_cfg is not None:
        print(
            "\nChecking library access via "
            f"{resolver_cfg.openurl_base}...",
            flush=True,
        )
        filtered_items_by_pub: dict[str, list[dict]] = {}
        for name, pub_items in items_by_pub.items():
            handler = handler_by_name[name]
            # Each handler declares which SFX target domains it can
            # actually reach. If the library reports access via an
            # unrelated platform (JSTOR/EBSCOhost/ProQuest), we skip the
            # item — our handler only knows the direct-publisher URL.
            required_domains = handler.direct_access_domains
            accessible, inaccessible = [], []
            for it in pub_items:
                if has_fulltext_access(
                    it["doi"], resolver_cfg, required_domains=required_domains,
                ):
                    accessible.append(it)
                else:
                    inaccessible.append(it)
            for it in inaccessible:
                log_writer.writerow({
                    "run_date": run_date, "item_key": it["item_key"],
                    "doi": it["doi"],
                    "title": (it.get("title") or "")[:70],
                    "status": "skipped_no_library_coverage", "source": name,
                })
            if inaccessible:
                display = handler.display_name or name
                via = ""
                if required_domains:
                    via = f" via {'/'.join(required_domains)}"
                print(
                    f"  {display}: {len(accessible)} accessible{via}, "
                    f"{len(inaccessible)} not reachable by this handler",
                    flush=True,
                )
            if accessible:
                filtered_items_by_pub[name] = accessible
        items_by_pub = filtered_items_by_pub
        if not items_by_pub:
            print(
                "\nNo items have library full-text coverage. "
                "Nothing to do via the browser path.",
                flush=True,
            )
            return 0

    print(f"\nPublishers queued ({len(items_by_pub)}):", flush=True)
    for name, pub_items in items_by_pub.items():
        display = handler_by_name[name].display_name or name
        print(f"  • {display}: {len(pub_items)} paper"
              f"{'' if len(pub_items) == 1 else 's'}", flush=True)

    async def _run_all() -> None:
        for name, pub_items in items_by_pub.items():
            handler = handler_by_name[name]
            await _drive_handler(handler, pub_items, zot, log_writer, args, run_date)

    asyncio.run(_run_all())
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sources", default="",
        help="Comma-separated fetcher names to use. Special values: "
             "'wiley' (Wiley TDM only), 'browser' (Cloudflare-gated via "
             "Playwright, delegates to fetch_pdfs_browser.py). Default: "
             "automated cascade (elsevier+springer+crossref+pmc+openalex+unpaywall).",
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
    parser.add_argument(
        "--group", default=os.environ.get("ZOTERO_GROUP", ""),
        help="Zotero group ID (per-project; default: $ZOTERO_GROUP). "
             "If omitted and only one group is accessible, auto-selected.",
    )
    parser.add_argument(
        "--legacy-browser", action="store_true",
        help="(browser mode) Delegate to the legacy fetch_pdfs_browser.py "
             "subprocess instead of the new in-process handlers. Rollback "
             "option while the new handlers prove themselves.",
    )
    args = parser.parse_args()

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
    try:
        zot = zotero_io.ZoteroClient.from_config(group_id=args.group or None)
    except zotero_io.GroupSelectionRequired as e:
        print(zotero_io.format_group_selection_error(e.groups), file=sys.stderr)
        return 2

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

    # Browser path (default for --sources browser): drives
    # fetchers.browser handlers in-process. The sources= list is not
    # relevant here — browser handlers are picked per-publisher.
    if source_names == ["browser"]:
        log_fh, log_writer = _open_log(args.log_csv)
        try:
            return _run_browser_in_process(
                to_process, zot, log_writer, args, run_date,
            )
        finally:
            log_fh.close()

    sources = fetchers.pdf_sources(
        session, config, names=source_names if source_names else None,
    )
    if not sources:
        print(f"ERROR: no PDF fetchers matched --sources={args.sources!r}",
              file=sys.stderr)
        return 2
    print(f"Active fetchers: {[s.name for s in sources]}", flush=True)

    log_fh, log_writer = _open_log(args.log_csv)
    attached = no_pdf = failed = 0

    # Phase 1: download in parallel (order preserved by key->future map).
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
                print(f"  [{len(results)}/{len(to_process)}] {title70:<70} "
                      f"({src_name}) {size_kb}KB", flush=True)
            else:
                print(f"  [{len(results)}/{len(to_process)}] {title70:<70} "
                      f"no PDF", flush=True)

    # Phase 2: upload serial (Zotero write API).
    found = [(item, r) for item, r in results if r is not None]
    not_found = [item for item, r in results if r is None]

    print(f"\n  Downloaded: {len(found)}, Not found: {len(not_found)}",
          flush=True)

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

        print(f"  [{j}/{len(found)}] {title:<70} ({src_name})", end=" ",
              flush=True)
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
                "title": title, "status": "upload_failed", "source": src_name,
            })
            failed += 1

    log_fh.close()
    print(
        f"\nDone. attached={attached}, no-pdf={no_pdf}, failed={failed}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
