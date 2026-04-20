#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyzotero>=1.6",
#     "playwright>=1.40",
# ]
# ///
"""
Interactive browser-based PDF fetcher for papers behind Cloudflare.

Emerald, Sage, Taylor & Francis and similar publishers fire Cloudflare
challenges that ordinary HTTP clients cannot solve. This script launches
a visible Chromium via Playwright. The user passes the challenge once
per publisher domain. The authenticated browser session is then reused
to download every missing PDF for that publisher.

The script is a companion to `attach_pdfs.py`. It picks up the Zotero
items `attach_pdfs.py` could not handle (CF blocks the standard HTTP
cascade), and writes "attached" rows to the same log so a second pass
of `attach_pdfs.py` knows the work is already done.

Usage:
    python3 fetch_pdfs_browser.py                    # all publishers, every missing item
    python3 fetch_pdfs_browser.py --publisher emerald
    python3 fetch_pdfs_browser.py --filter-keys-file keys.txt
    python3 fetch_pdfs_browser.py --log-csv project/analysis/raw/pdf_attach_log.csv \\
                                  --cache-dir project/pdfs

Setup (one-off per machine):
    pip3 install --break-system-packages playwright
    python3 -m playwright install chromium

Required environment variables:
    ZOTERO_API_KEY
    ZOTERO_GROUP

Dependencies:
    Playwright + Chromium (see setup above).
    attach_pdfs.py in the same directory (for Zotero upload helpers).
"""

import argparse
import asyncio
import csv
import json
import os
import sys
import time

try:
    from playwright.async_api import async_playwright
except ImportError:
    sys.exit("ERROR: playwright not installed.\n"
             "Run: pip3 install --break-system-packages playwright "
             "&& python3 -m playwright install chromium")

# Sibling reusable script for Zotero upload helpers
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import attach_pdfs  # noqa: E402

# Scripts root (for cross-package imports)
SCRIPTS_ROOT = os.path.dirname(SCRIPT_DIR)
if SCRIPTS_ROOT not in sys.path:
    sys.path.insert(0, SCRIPTS_ROOT)
from publishers.registry import DEFAULT_PUBLISHERS  # noqa: E402


def load_publishers(path: str | None) -> dict:
    if not path:
        return DEFAULT_PUBLISHERS
    with open(path) as f:
        return json.load(f)


# --------------------------------------------------------------------------- #
# Small helpers                                                                #
# --------------------------------------------------------------------------- #

def cache_path_for(cache_dir: str, doi: str) -> str:
    return os.path.join(cache_dir, doi.replace("/", "_") + ".pdf")


def is_cached(cache_dir: str, doi: str) -> bool:
    p = cache_path_for(cache_dir, doi)
    if not os.path.exists(p) or os.path.getsize(p) < 1000:
        return False
    with open(p, "rb") as f:
        return f.read(5) == b"%PDF-"


def wait_for_user(prompt: str):
    """Block on stdin; read from /dev/tty when stdin is redirected."""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    try:
        with open("/dev/tty") as tty:
            tty.readline()
    except Exception:
        sys.stdin.readline()


def progress_tag(counter, total, t_start):
    done = counter["ok"] + counter["failed"] + counter["cached"]
    elapsed = time.monotonic() - t_start
    if done == 0:
        return f"[{done}/{total} | {elapsed:.0f}s elapsed]"
    avg = elapsed / done
    remaining = (total - done) * avg
    return f"[{done}/{total} | {elapsed:.0f}s | avg {avg:.1f}s/item | ~{remaining:.0f}s left]"


def load_done_dois(log_csv: str) -> set[str]:
    """DOIs already marked as attached in a prior run."""
    if not os.path.exists(log_csv):
        return set()
    done = set()
    with open(log_csv, newline="") as f:
        first = f.readline(); f.seek(0)
        if "status" in first.lower() or "run_date" in first.lower():
            reader = csv.DictReader(f)
        else:
            reader = csv.DictReader(f, fieldnames=["run_date", "key", "doi", "title", "status"])
        for row in reader:
            if row.get("status") == "attached":
                doi = (row.get("doi") or "").strip().lower()
                if doi:
                    done.add(doi)
    return done


# --------------------------------------------------------------------------- #
# Item discovery                                                               #
# --------------------------------------------------------------------------- #

def load_items(publishers: dict, log_csv: str, filter_keys_file: str | None):
    """
    Return list of {doi, item_key, title, publisher} for items that:
      - Are in the Zotero library (read via local client)
      - Match a known publisher DOI prefix
      - Do NOT yet have a PDF attachment
      - Are NOT already marked 'attached' in log_csv
      - Are in filter_keys_file (if provided)
    """
    print("Connecting to local Zotero client...", flush=True)
    local = attach_pdfs.make_local_client()
    print("Fetching Zotero items...", end=" ", flush=True)
    all_items = attach_pdfs.get_all_items(local)
    print(f"{len(all_items)} journal articles.", flush=True)

    if filter_keys_file:
        with open(filter_keys_file) as f:
            keys = {line.strip() for line in f if line.strip()}
        all_items = [it for it in all_items if it["key"] in keys]
        print(f"  After --filter-keys-file: {len(all_items)} items", flush=True)

    print("Checking for existing PDF attachments...", end=" ", flush=True)
    pdf_map = attach_pdfs.get_pdf_map(local)
    print("map built.", flush=True)

    done_dois = load_done_dois(log_csv)

    items = []
    skipped_already_attached = 0
    skipped_unknown_publisher = 0
    skipped_has_pdf = 0
    skipped_no_doi = 0
    for it in all_items:
        data = it.get("data", {})
        key = it["key"]
        doi = (data.get("DOI") or "").strip().lower()
        if not doi:
            skipped_no_doi += 1
            continue
        if doi in done_dois:
            skipped_already_attached += 1
            continue
        # Existing real PDF attached?
        has_real, _ = pdf_map.get(key, (False, []))
        if has_real:
            skipped_has_pdf += 1
            continue

        # Route to publisher by DOI prefix
        pub = None
        for pub_key, info in publishers.items():
            if any(doi.startswith(m) for m in info["match"]):
                pub = pub_key; break
        if not pub:
            skipped_unknown_publisher += 1
            continue

        items.append({
            "doi": doi,
            "item_key": key,
            "title": data.get("title", ""),
            "publisher": pub,
        })

    print(f"  Candidates: {len(items)}", flush=True)
    print(f"  Skipped: {skipped_has_pdf} already have PDF, "
          f"{skipped_already_attached} marked attached in log, "
          f"{skipped_unknown_publisher} non-target publisher, "
          f"{skipped_no_doi} no DOI", flush=True)
    return items


# --------------------------------------------------------------------------- #
# Download strategies                                                          #
# --------------------------------------------------------------------------- #

async def _try_click(page, *selectors, timeout=8000):
    """Click the first selector that resolves to a visible element. Return True on click."""
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            await loc.wait_for(state="visible", timeout=timeout)
            await loc.click()
            return True
        except Exception:
            continue
    return False


async def download_via_psycnet(page, item, url_tmpl, cache_dir, total, counter, t_start):
    """
    APA PsycNET multi-step click-through:
      1. doi.org/{doi} → psycnet.apa.org/doiLanding?doi=...
      2. Click "Get Access"     → overlay appears
      3. Click "CHECK ACCESS"   → /recordAccess/institutional/{apaID}
      4. Click "DOWNLOAD PDF"   → fires download event (requires Chromium's
                                  built-in PDF viewer to be disabled, see
                                  persistent-profile setup in fetch_publisher)
    Session cookies from the initial institutional SSO persist across DOIs.
    """
    doi = item["doi"]
    out = cache_path_for(cache_dir, doi)
    if is_cached(cache_dir, doi):
        counter["cached"] += 1
        return doi, "cached"
    url = url_tmpl.format(doi=doi)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(1500)

        await _try_click(
            page,
            "button:has-text('Get Access')",
            "a:has-text('Get Access')",
            "[data-action='get-access']",
            timeout=5000,
        )
        await page.wait_for_timeout(1000)

        await _try_click(
            page,
            "button:has-text('Check Access')",
            "a:has-text('Check Access')",
            "button:has-text('CHECK ACCESS')",
            timeout=8000,
        )
        try:
            await page.wait_for_url("**/recordAccess/institutional/**", timeout=15000)
        except Exception:
            pass
        await page.wait_for_timeout(1500)

        async with page.expect_download(timeout=30000) as dl_info:
            clicked = await _try_click(
                page,
                "a[href*='/fulltext/'][href*='.pdf']",
                "button:has-text('Download PDF')",
                "a:has-text('Download PDF')",
                "button:has-text('DOWNLOAD PDF')",
                "a:has-text('Download')",
                timeout=15000,
            )
            if not clicked:
                raise RuntimeError("Download button not found")
        dl = await dl_info.value
        await dl.save_as(out)

        with open(out, "rb") as f:
            if f.read(5) == b"%PDF-":
                counter["ok"] += 1
                size = os.path.getsize(out)
                print(f"  {progress_tag(counter, total, t_start)} "
                      f"ok ({size//1024}KB) {item['title'][:50]}", flush=True)
                return doi, "ok"
            else:
                os.remove(out)
                counter["failed"] += 1
                print(f"  {progress_tag(counter, total, t_start)} "
                      f"not a PDF {item['title'][:45]}", flush=True)
                return doi, "failed"
    except Exception as e:
        counter["failed"] += 1
        print(f"  {progress_tag(counter, total, t_start)} "
              f"ERROR: {str(e)[:100]}", flush=True)
        return doi, "error"


async def download_via_page(page, item, url_tmpl, cache_dir, total, counter, t_start):
    """Navigate the browser page — required when ctx.request is CF-blocked."""
    doi = item["doi"]
    out = cache_path_for(cache_dir, doi)
    if is_cached(cache_dir, doi):
        counter["cached"] += 1
        return doi, "cached"
    url = url_tmpl.format(doi=doi)
    try:
        async with page.expect_download(timeout=30000) as dl_info:
            try:
                await page.goto(url, wait_until="commit", timeout=15000)
            except Exception:
                pass  # download event interrupts navigation
        dl = await dl_info.value
        await dl.save_as(out)
        with open(out, "rb") as f:
            if f.read(5) == b"%PDF-":
                counter["ok"] += 1
                size = os.path.getsize(out)
                print(f"  {progress_tag(counter, total, t_start)} "
                      f"ok ({size//1024}KB) {item['title'][:50]}", flush=True)
                return doi, "ok"
            else:
                os.remove(out)
                counter["failed"] += 1
                print(f"  {progress_tag(counter, total, t_start)} "
                      f"not a PDF {item['title'][:45]}", flush=True)
                return doi, "failed"
    except Exception as e:
        counter["failed"] += 1
        print(f"  {progress_tag(counter, total, t_start)} "
              f"ERROR: {str(e)[:70]}", flush=True)
        return doi, "error"


async def download_via_request(ctx, item, url_tmpl, cache_dir, total, sem,
                               counter, t_start, delay=0.0, pdf_dir_for_diag=None):
    """Use Playwright's request API reusing browser cookies — fast when it works."""
    async with sem:
        if delay > 0:
            await asyncio.sleep(delay)
        doi = item["doi"]
        out = cache_path_for(cache_dir, doi)
        if is_cached(cache_dir, doi):
            counter["cached"] += 1
            return doi, "cached"
        url = url_tmpl.format(doi=doi)
        try:
            resp = await ctx.request.get(url, timeout=60000)
            body = await resp.body()
            if body[:5] == b"%PDF-":
                with open(out, "wb") as f:
                    f.write(body)
                counter["ok"] += 1
                print(f"  {progress_tag(counter, total, t_start)} "
                      f"ok ({len(body)//1024}KB) {item['title'][:50]}", flush=True)
                return doi, "ok"
            else:
                text = body[:2000].decode("utf-8", errors="replace").lower()
                if "just a moment" in text or "cf-chl" in text or "cloudflare" in text:
                    hint = "CF challenge"
                elif "access" in text and ("denied" in text or "not available" in text or "subscri" in text):
                    hint = "no subscription"
                elif "purchase" in text or "buy" in text or "rent" in text:
                    hint = "paywall"
                else:
                    hint = f"other ({len(body)}B)"
                counter["failed"] += 1
                print(f"  {progress_tag(counter, total, t_start)} "
                      f"failed {resp.status} [{hint}] {item['title'][:35]}", flush=True)
                if counter["failed"] == 1 and pdf_dir_for_diag:
                    diag = os.path.join(pdf_dir_for_diag, "pdf_403_sample.html")
                    with open(diag, "wb") as f:
                        f.write(body)
                    print(f"    (saved sample → {diag})", flush=True)
                return doi, "failed"
        except Exception as e:
            counter["failed"] += 1
            print(f"  {progress_tag(counter, total, t_start)} "
                  f"ERROR: {str(e)[:60]}", flush=True)
            return doi, "error"


# --------------------------------------------------------------------------- #
# Publisher runner                                                             #
# --------------------------------------------------------------------------- #

async def fetch_publisher(publisher: str, info: dict, items: list[dict],
                          cache_dir: str) -> list[str]:
    print(f"\n{'='*60}\nPublisher: {info['name']} ({len(items)} PDFs)\n{'='*60}",
          flush=True)
    if not items:
        return []
    os.makedirs(cache_dir, exist_ok=True)

    # Persistent user-data-dir so we can disable Chromium's built-in PDF
    # viewer (`plugins.always_open_pdf_externally=true`). Without this, PDF
    # navigations are rendered inline and no download event fires. As a bonus,
    # institutional SSO cookies persist across runs.
    user_data_dir = os.path.join(cache_dir, ".chrome-profile")
    os.makedirs(user_data_dir, exist_ok=True)
    prefs_dir = os.path.join(user_data_dir, "Default")
    os.makedirs(prefs_dir, exist_ok=True)
    prefs_file = os.path.join(prefs_dir, "Preferences")
    prefs = {}
    if os.path.exists(prefs_file):
        try:
            with open(prefs_file) as f:
                prefs = json.load(f)
        except Exception:
            prefs = {}
    prefs.setdefault("plugins", {})["always_open_pdf_externally"] = True
    with open(prefs_file, "w") as f:
        json.dump(prefs, f)

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=False,
            accept_downloads=True,
            viewport={"width": 1200, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        first_url = info["url"].format(doi=items[0]["doi"])
        print(f"\nOpening: {first_url}", flush=True)
        try:
            await page.goto(first_url, wait_until="domcontentloaded", timeout=20000)
        except Exception:
            pass

        print("\n" + "*" * 70, flush=True)
        print("*  1. Click the Chromium window.", flush=True)
        print("*  2. Solve any Cloudflare challenge / institutional SSO login.", flush=True)
        print("*  3. Wait until you see the publisher page (PDF or article).", flush=True)
        print("*  4. Click back to THIS terminal and press Enter.", flush=True)
        print("*" * 70, flush=True)
        await asyncio.to_thread(wait_for_user, "\n>>> Press Enter to start downloads: ")

        concurrency = int(info.get("concurrency", 2))
        delay = float(info.get("delay_s", 0.0))
        use_page = bool(info.get("use_page_nav", False))
        flow = info.get("flow")
        if flow == "psycnet":
            mode = "psycnet click-through (serial)"
        elif use_page:
            mode = "page-nav (serial)"
        else:
            mode = f"request (concurrency={concurrency})"
        print(f"  Mode: {mode}, delay={delay}s/request", flush=True)

        counter = {"ok": 0, "cached": 0, "failed": 0}
        total = len(items)
        t_start = time.monotonic()

        if flow == "psycnet":
            results = []
            for it in items:
                if delay > 0:
                    await asyncio.sleep(delay)
                res = await download_via_psycnet(page, it, info["url"], cache_dir,
                                                 total, counter, t_start)
                results.append(res)
        elif use_page:
            results = []
            for it in items:
                if delay > 0:
                    await asyncio.sleep(delay)
                res = await download_via_page(page, it, info["url"], cache_dir,
                                              total, counter, t_start)
                results.append(res)
        else:
            sem = asyncio.Semaphore(concurrency)
            tasks = [download_via_request(ctx, it, info["url"], cache_dir, total,
                                          sem, counter, t_start, delay, cache_dir)
                     for it in items]
            results = await asyncio.gather(*tasks, return_exceptions=False)

        print(f"\n  Total elapsed: {time.monotonic()-t_start:.0f}s", flush=True)
        print(f"  Summary: {counter['ok']} new, {counter['cached']} cached, "
              f"{counter['failed']} failed", flush=True)

        await ctx.close()

    return [doi for doi, status in results if status in ("ok", "cached")]


# --------------------------------------------------------------------------- #
# Zotero upload                                                                #
# --------------------------------------------------------------------------- #

def upload_to_zotero(items, downloaded_dois, cache_dir, log_csv):
    if not downloaded_dois:
        print("\nNothing to upload.", flush=True)
        return
    print(f"\nUploading {len(downloaded_dois)} PDFs to Zotero...", flush=True)
    by_doi = {it["doi"]: it for it in items}
    uploaded = failed = 0
    for doi in downloaded_dois:
        it = by_doi.get(doi)
        if not it or not it["item_key"]:
            failed += 1; continue
        with open(cache_path_for(cache_dir, doi), "rb") as f:
            pdf_bytes = f.read()
        filename = doi.replace("/", "_") + ".pdf"
        att_key = attach_pdfs.create_attachment_item(it["item_key"], filename)
        if not att_key:
            print(f"  [{doi}] create attachment FAILED", flush=True)
            failed += 1; continue
        if attach_pdfs.upload_pdf(att_key, pdf_bytes, filename):
            uploaded += 1
            print(f"  [{doi}] uploaded ({len(pdf_bytes)//1024}KB)", flush=True)
            os.makedirs(os.path.dirname(log_csv) or ".", exist_ok=True)
            with open(log_csv, "a", newline="") as f:
                csv.writer(f).writerow(
                    ["browser_dl", it["item_key"], doi, it["title"][:100], "attached"])
        else:
            print(f"  [{doi}] upload FAILED", flush=True)
            failed += 1
    print(f"\nUpload summary: {uploaded} ok, {failed} failed", flush=True)


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #

async def main_async():
    parser = argparse.ArgumentParser(
        description="Browser-based PDF fetcher for Cloudflare-protected publishers.")
    parser.add_argument("--publisher", default="all",
                        help="One publisher key or 'all' (default: all). See "
                             "DEFAULT_PUBLISHERS in this file or --publishers-json.")
    parser.add_argument("--publishers-json",
                        help="Override the publisher registry with a JSON file.")
    parser.add_argument("--log-csv", default="output/pdf_attach_log.csv",
                        help="Append 'attached' rows here; also read to skip done DOIs.")
    parser.add_argument("--cache-dir", default="output/pdf_cache",
                        help="Where PDFs are saved before upload.")
    parser.add_argument("--filter-keys-file",
                        help="Text file with Zotero item keys, one per line.")
    args = parser.parse_args()

    if not os.environ.get("ZOTERO_API_KEY"):
        sys.exit("Error: ZOTERO_API_KEY environment variable not set.")
    if not os.environ.get("ZOTERO_GROUP"):
        sys.exit("Error: ZOTERO_GROUP environment variable not set.")

    publishers = load_publishers(args.publishers_json)
    items = load_items(publishers, args.log_csv, args.filter_keys_file)

    # Summary of what will happen, *before* Chromium opens. Non-technical
    # users seeing a browser window pop up with no context is jarring; this
    # block exists specifically to give them advance notice.
    pub_counts = [(info["name"], sum(1 for x in items if x["publisher"] == pub))
                  for pub, info in publishers.items()]
    active = [(name, n) for name, n in pub_counts if n > 0]

    print()
    print("=" * 72, flush=True)
    print("  Browser-based PDF fetcher", flush=True)
    print("=" * 72, flush=True)
    print(flush=True)
    if not active:
        print("  No items match any configured publisher. Nothing to do.",
              flush=True)
        print("=" * 72, flush=True)
        return
    print("  A Chromium browser window is about to open. You will see it on",
          flush=True)
    print("  your desktop. For each publisher, you may need to:", flush=True)
    print(flush=True)
    print("    1. Solve a one-time Cloudflare challenge (click the checkbox",
          flush=True)
    print("       or similar). Only once per publisher domain per session.",
          flush=True)
    print("    2. Sign in via your institution's SSO if the publisher",
          flush=True)
    print("       prompts for it.", flush=True)
    print(flush=True)
    print("  After you pass the challenge for a publisher, the script",
          flush=True)
    print("  downloads every queued PDF for that publisher automatically,",
          flush=True)
    print("  using the same authenticated session.", flush=True)
    print(flush=True)
    print(f"  Publishers queued ({len(active)}):", flush=True)
    for name, n in active:
        print(f"    • {name}: {n} paper{'s' if n != 1 else ''}", flush=True)
    print(flush=True)
    print(f"  Total:  {sum(n for _, n in active)} papers across {len(active)} publishers", flush=True)
    print("  Leave this terminal and the browser window open until done.",
          flush=True)
    print("  Press Ctrl-C to abort at any time.", flush=True)
    print("=" * 72, flush=True)
    print(flush=True)

    to_run = list(publishers.keys()) if args.publisher == "all" else [args.publisher]
    for pub in to_run:
        if pub not in publishers:
            print(f"Unknown publisher: {pub}", flush=True); continue
        pub_items = [x for x in items if x["publisher"] == pub]
        if not pub_items:
            continue
        downloaded = await fetch_publisher(pub, publishers[pub], pub_items,
                                           args.cache_dir)
        upload_to_zotero(pub_items, downloaded, args.cache_dir, args.log_csv)


if __name__ == "__main__":
    asyncio.run(main_async())
