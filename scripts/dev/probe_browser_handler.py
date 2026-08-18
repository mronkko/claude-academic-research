# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "playwright>=1.40",
#     "requests>=2.31",
#     "urllib3>=2.0",
#     "tenacity>=8.0",
# ]
# ///
"""Drive one browser handler against a handful of DOIs. No Zotero.

Debugging a publisher handler through `enrich_pdfs.py` costs a Zotero
fetch of the whole key list, an attachment scan, and a resolver
pre-flight over the residual — minutes of unrelated work before the
browser opens, repeated on every attempt. Four rounds of APA fixes were
paid for at that rate.

This runs the handler and nothing else:

    uv run scripts/dev/probe_browser_handler.py --handler apa \
        --doi 10.1037/0882-7974.12.2.376 --doi 10.1037/0021-9010.86.5.943

It reuses the real `launch_context` and the real handler classes, so
what it exercises is what the pipeline runs — the only things missing
are the item queue and the upload. Failures land in the handler's own
`<cache-dir>/diagnostics/` as usual.

`--keep-open` holds the browser at the end so the failing page can be
inspected by hand; `--step` prints the page URL and title after each
handler call, which is the thing that turned out to matter — three
separate APA bugs were all "the handler is on a different page than it
thinks it is".
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "pipelines"))

from fetchers.browser import (  # noqa: E402
    Counter,
    all_handlers,
    launch_context,
)


def _handler_by_name(name: str):
    """Registry lookup that also reaches the handlers kept out of it.

    `EbscoHandler` and `ZoteroConnectorHandler` are deliberately absent
    from `all_handlers()` — nothing routes to them by DOI — but they are
    exactly the ones worth probing by hand.
    """
    for h in all_handlers():
        if h.name == name:
            return h
    if name == "ebsco":
        from fetchers.browser.ebsco import EbscoHandler
        return EbscoHandler()
    raise SystemExit(
        f"unknown handler {name!r}. Known: "
        + ", ".join(sorted(h.name for h in all_handlers())) + ", ebsco"
    )


async def _probe(args: argparse.Namespace) -> int:
    from playwright.async_api import async_playwright

    handler = _handler_by_name(args.handler)
    cache_dir = os.path.abspath(args.cache_dir)
    os.makedirs(cache_dir, exist_ok=True)

    # A probe whose second run is a cache hit cannot answer "did the fix
    # work" — the first run poisons every one after it. Live: two DOIs
    # were fetched, then the same command re-run reported both from
    # cache and tested nothing.
    if args.fresh:
        from fetchers.browser.base import cache_path_for
        for doi in args.doi:
            target = Path(cache_path_for(cache_dir, doi))
            if target.exists():
                target.unlink()
                print(f"removed cached {target.name}", flush=True)
    if args.fresh_profile:
        import shutil
        profile = Path(cache_dir) / ".chrome-profile"
        if profile.exists():
            shutil.rmtree(profile, ignore_errors=True)
            print(f"removed browser profile {profile}", flush=True)
    print(f"handler:   {handler.name} ({handler.display_name})")
    print(f"cache dir: {cache_dir}")
    print(f"dois:      {len(args.doi)}\n")

    failures = 0
    async with async_playwright() as p:
        ctx = await launch_context(p, cache_dir)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        if args.setup:
            print("--- setup ---", flush=True)
            try:
                result = await handler.setup(page, args.doi[0])
                print(f"setup returned: {result!r}\n", flush=True)
            except Exception as e:
                print(f"setup raised: {e}\n", flush=True)

        counter = Counter()
        cached = fetched = 0
        for i, doi in enumerate(args.doi, 1):
            print(f"--- [{i}/{len(args.doi)}] {doi} ---", flush=True)
            t0 = time.monotonic()
            item = {
                "doi": doi,
                "item_key": "",           # no Zotero: nothing to attach to
                "title": doi,
                "item_type": "journalArticle",
                "resolver_target_url": args.resolver_target or "",
            }
            try:
                result = await handler.download(
                    page, ctx, item, cache_dir,
                    counter=counter, total=len(args.doi), t_start=t0,
                )
            except Exception as e:                      # noqa: BLE001
                # Handlers normally swallow and report; anything escaping
                # here is worth seeing whole.
                print(f"  RAISED: {type(e).__name__}: {e}", flush=True)
                result = None

            elapsed = time.monotonic() - t0
            if result is None:
                failures += 1
                print(f"  -> no PDF ({elapsed:.1f}s)", flush=True)
            else:
                path, source_url = result
                size = Path(path).stat().st_size // 1024 if Path(path).exists() else 0
                if source_url.startswith("cache://"):
                    cached += 1
                    print(f"  -> {size}KB from CACHE — nothing was tested. "
                          f"Re-run with --fresh.", flush=True)
                else:
                    fetched += 1
                    print(f"  -> {size}KB from {source_url} ({elapsed:.1f}s)",
                          flush=True)

            if args.step:
                try:
                    print(f"  page.url:   {page.url}", flush=True)
                    print(f"  page.title: {await page.title()}", flush=True)
                except Exception as e:
                    print(f"  (page unreadable: {e})", flush=True)
            print(flush=True)

        print(f"\ndone: {fetched} fetched, {cached} served from cache, "
              f"{failures} failed", flush=True)
        if cached and not fetched:
            print("  NOTE: every item came from the cache, so this run "
                  "exercised nothing. Use --fresh to re-test.", flush=True)
        diagnostics = Path(cache_dir) / "diagnostics"
        if diagnostics.is_dir():
            print(f"diagnostics: {diagnostics}", flush=True)

        if args.keep_open:
            # Deliberately blocking. The point is to leave the failing
            # page on screen, with its session, for a human to poke at.
            print("\n--keep-open: browser stays up. Press Enter to close.",
                  flush=True)
            await asyncio.get_running_loop().run_in_executor(None, input)
        await ctx.close()
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--handler", required=True,
                        help="handler name, e.g. apa / sage / tandf / ebsco")
    parser.add_argument("--doi", action="append", default=[], required=True,
                        help="DOI to fetch; repeat for several")
    parser.add_argument("--cache-dir", default="output/probe_cache",
                        help="where PDFs and diagnostics go "
                             "(default: output/probe_cache). Uses its own "
                             "browser profile, so a probe never disturbs a "
                             "pipeline run's session.")
    parser.add_argument("--fresh", action="store_true",
                        help="delete the cached PDF for each --doi first, so "
                             "the handler actually runs. Without this a "
                             "second run is all cache hits and proves "
                             "nothing.")
    parser.add_argument("--fresh-profile", action="store_true",
                        help="also delete the Chromium profile, discarding "
                             "SSO/session state. Use to test what an "
                             "unattended first item really faces — a warm "
                             "session hides every entitlement problem.")
    parser.add_argument("--setup", action="store_true",
                        help="call handler.setup() first — the interactive "
                             "sign-in / challenge step")
    parser.add_argument("--step", action="store_true",
                        help="print page URL and title after each item")
    parser.add_argument("--keep-open", action="store_true",
                        help="leave the browser open at the end")
    parser.add_argument("--resolver-target",
                        help="resolver_target_url for handlers driven from "
                             "the link resolver (ebsco)")
    args = parser.parse_args()
    return asyncio.run(_probe(args))


if __name__ == "__main__":
    raise SystemExit(main())
