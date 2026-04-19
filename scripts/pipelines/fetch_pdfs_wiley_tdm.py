#!/usr/bin/env python3
"""
Fetch Wiley PDFs via the official Wiley TDM Python client.

Wiley provides a first-party Text and Data Mining API that bypasses
Cloudflare, rate limits cleanly, and requires only an API token. This
is the preferred source for any paper with a Wiley DOI (10.1002/,
10.1111/, 10.1046/) — much more reliable than the browser-based
fallback in fetch_pdfs_browser.py.

Workflow:
    1. Read missing Wiley DOIs from a pdf_attach_log.csv (rows with
       status "skipped_no_pdf" that were not later marked "attached").
    2. Download each PDF through wiley-tdm.
    3. Upload successful downloads to Zotero via attach_pdfs helpers.
    4. Append "attached" rows to the same log.

Setup:
    pip3 install --break-system-packages wiley-tdm

Environment variables:
    WILEY_TDM_TOKEN   — Wiley TDM API token (UUID). Institution must
                        have a TDM agreement with Wiley; token is issued
                        per-requester. Set in your shell (e.g. .zshenv).
    ZOTERO_API_KEY, ZOTERO_GROUP — Zotero authentication.

Usage:
    python3 ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/fetch_pdfs_wiley_tdm.py \\
        --log-csv analysis/raw/pdf_attach_log.csv \\
        --cache-dir pdfs

    # Dry-run (report what would be done)
    python3 ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/fetch_pdfs_wiley_tdm.py --dry-run
"""

import argparse
import csv
import os
import sys

# Sibling reusable script for Zotero upload helpers
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import attach_pdfs  # noqa: E402

try:
    from wiley_tdm import TDMClient
    from wiley_tdm.download_status import DownloadStatus
except ImportError:
    sys.exit("ERROR: wiley-tdm not installed. "
             "Run: pip3 install --break-system-packages wiley-tdm")

WILEY_PREFIXES = ("10.1002/", "10.1111/", "10.1046/")


def load_missing_wiley(log_csv: str) -> list[str]:
    """Return sorted list of Wiley DOIs marked skipped_no_pdf and not later attached."""
    if not os.path.exists(log_csv):
        return []
    attached = set(); missing = set()
    with open(log_csv, newline="") as f:
        first = f.readline(); f.seek(0)
        reader = csv.DictReader(f) if "status" in first.lower() else \
                 csv.DictReader(f, fieldnames=["run_date","key","doi","title","status"])
        for row in reader:
            d = (row.get("doi") or "").strip().lower()
            s = row.get("status", "")
            if s == "attached":          attached.add(d)
            elif s == "skipped_no_pdf":   missing.add(d)
    missing -= attached
    return [d for d in sorted(missing)
            if any(d.startswith(p) for p in WILEY_PREFIXES)]


def get_doi_to_key() -> dict[str, str]:
    """Map {doi_lower: zotero_item_key} via the local Zotero client."""
    from pyzotero import zotero
    z = zotero.Zotero(attach_pdfs.ZOTERO_GROUP, "group",
                      attach_pdfs.ZOTERO_API_KEY, local=True)
    items = z.everything(z.items(itemType="journalArticle"))
    return {(it["data"].get("DOI") or "").strip().lower(): it["key"]
            for it in items
            if (it["data"].get("DOI") or "").strip()}


def main():
    parser = argparse.ArgumentParser(
        description="Download Wiley PDFs via the official TDM API and attach to Zotero.")
    parser.add_argument("--log-csv", default="output/pdf_attach_log.csv",
                        help="pdf_attach_log.csv path (reads missing DOIs, "
                             "appends 'attached' rows on success)")
    parser.add_argument("--cache-dir", default="output/pdf_cache",
                        help="Directory for downloaded PDFs")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be done, no downloads or uploads")
    args = parser.parse_args()

    token = os.environ.get("WILEY_TDM_TOKEN", "")
    if not token and not args.dry_run:
        sys.exit("ERROR: WILEY_TDM_TOKEN not set. Get a Wiley TDM token at "
                 "https://onlinelibrary.wiley.com/library-info/resources/text-and-datamining")
    if not os.environ.get("ZOTERO_API_KEY") and not args.dry_run:
        sys.exit("ERROR: ZOTERO_API_KEY not set")
    if not os.environ.get("ZOTERO_GROUP") and not args.dry_run:
        sys.exit("ERROR: ZOTERO_GROUP not set")

    dois = load_missing_wiley(args.log_csv)
    print(f"Missing Wiley DOIs to fetch: {len(dois)}", flush=True)
    for d in dois:
        print(f"  {d}", flush=True)

    if args.dry_run:
        print("\n[DRY RUN] No downloads or uploads.", flush=True)
        return

    if not dois:
        print("Nothing to do.", flush=True)
        return

    print("\nLoading Zotero item keys...", flush=True)
    doi_to_key = get_doi_to_key()
    missing_in_zotero = [d for d in dois if d not in doi_to_key]
    if missing_in_zotero:
        print(f"  WARNING: {len(missing_in_zotero)} DOIs not found in Zotero "
              f"(will still download but skip upload):", flush=True)
        for d in missing_in_zotero[:10]:
            print(f"    {d}", flush=True)

    os.makedirs(args.cache_dir, exist_ok=True)

    print(f"\nDownloading {len(dois)} PDFs via Wiley TDM...", flush=True)
    client = TDMClient(api_token=token, download_dir=args.cache_dir)

    def on_each(result):
        ok = result.status == DownloadStatus.SUCCESS
        status_tag = "ok" if ok else str(result.status).split(".")[-1]
        print(f"  [{status_tag}] {result.doi}  {result.comment or ''}",
              flush=True)

    results = client.download_pdfs(dois, on_result=on_each)
    successes = [r for r in results if r.status == DownloadStatus.SUCCESS]
    failures  = [r for r in results if r.status != DownloadStatus.SUCCESS]
    print(f"\nDownload summary: {len(successes)} ok, {len(failures)} failed",
          flush=True)
    if failures:
        print("Failures:", flush=True)
        for r in failures:
            print(f"  {r.doi} — {r.comment or r.status}", flush=True)

    if not successes:
        return

    print(f"\nUploading {len(successes)} PDFs to Zotero...", flush=True)
    uploaded = upload_failed = 0
    log_rows = []
    for r in successes:
        doi = r.doi.lower()
        key = doi_to_key.get(doi)
        if not key:
            print(f"  [{doi}] no Zotero item — skipping upload", flush=True)
            upload_failed += 1; continue
        pdf_path = getattr(r, "file_path", None) or getattr(r, "path", None)
        if not pdf_path or not os.path.exists(str(pdf_path)):
            print(f"  [{doi}] download path missing — skipping", flush=True)
            upload_failed += 1; continue
        with open(str(pdf_path), "rb") as f:
            pdf_bytes = f.read()
        if pdf_bytes[:5] != b"%PDF-":
            print(f"  [{doi}] not a valid PDF — skipping", flush=True)
            upload_failed += 1; continue
        filename = doi.replace("/", "_") + ".pdf"
        att_key = attach_pdfs.create_attachment_item(key, filename)
        if not att_key:
            print(f"  [{doi}] create attachment FAILED", flush=True)
            upload_failed += 1; continue
        if attach_pdfs.upload_pdf(att_key, pdf_bytes, filename):
            uploaded += 1
            print(f"  [{doi}] uploaded ({len(pdf_bytes)//1024}KB)", flush=True)
            log_rows.append(["wiley_tdm", key, doi, "", "attached"])
        else:
            upload_failed += 1
            print(f"  [{doi}] upload FAILED", flush=True)

    if log_rows:
        os.makedirs(os.path.dirname(args.log_csv) or ".", exist_ok=True)
        with open(args.log_csv, "a", newline="") as f:
            csv.writer(f).writerows(log_rows)

    print(f"\nDone. Uploaded: {uploaded}, upload-failed: {upload_failed}, "
          f"tdm-failed: {len(failures)}", flush=True)


if __name__ == "__main__":
    main()
