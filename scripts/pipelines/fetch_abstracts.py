#!/usr/bin/env python3
"""
Fetch missing abstracts for items in a Zotero group library.

Sources (in order of reliability):
  1. Crossref (publisher-deposited abstracts, most reliable)
  2. Semantic Scholar (by DOI)
  3. Semantic Scholar (by title, fallback when DOI lookup misses)
  4. Scopus (via pybliometrics — broad coverage)
  5. ScienceDirect (via pybliometrics — Elsevier journals)
  6. OpenAlex GROBID (full-text extraction, last resort)

Note: OpenAlex abstract_inverted_index is NOT used. It is often
reconstructed from GROBID full-text parsing and returns garbage.

Required environment variables:
  ZOTERO_API_KEY    — Zotero API key
  ZOTERO_GROUP      — Zotero group ID
  CROSSREF_MAILTO   — Email for Crossref polite pool
  SEMANTIC_SCHOLAR_API_KEY        — Semantic Scholar API key (optional but recommended)
  OPENALEX_API_KEY  — OpenAlex Content API key (optional, for GROBID)

Usage:
  python3 fetch_abstracts.py                                    # full run
  python3 fetch_abstracts.py --dry-run                         # report only
  python3 fetch_abstracts.py --log-csv output/abstract_log.csv # custom log
  python3 fetch_abstracts.py --cache-dir output/fulltext_cache # custom cache
"""

import argparse
import csv
import gzip
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

# ---------------------------------------------------------------------------
# Configuration (from environment variables)
# ---------------------------------------------------------------------------
ZOTERO_API_KEY           = os.environ.get("ZOTERO_API_KEY", "")
ZOTERO_GROUP             = os.environ.get("ZOTERO_GROUP", "")
SEMANTIC_SCHOLAR_API_KEY = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "")
CROSSREF_MAILTO          = os.environ.get("CROSSREF_MAILTO", "")
OPENALEX_API_KEY         = os.environ.get("OPENALEX_API_KEY", "")

ZOTERO_BASE = f"https://api.zotero.org/groups/{ZOTERO_GROUP}"

# Defaults (overridable via CLI)
DEFAULT_LOG_CSV    = os.path.join("output", "abstract_fetch_log.csv")
DEFAULT_CACHE_DIR  = os.path.join("output", "fulltext_cache")

# ---------------------------------------------------------------------------
# SSL
# ---------------------------------------------------------------------------
_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def get_json(url: str, headers: dict = None, retries: int = 3) -> dict | None:
    req = urllib.request.Request(url, headers=headers or {})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=20, context=_SSL) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 429:
                wait = 10 * (attempt + 1)
                print(f"    Rate limited — sleeping {wait}s", file=sys.stderr)
                time.sleep(wait)
            elif attempt < retries - 1:
                time.sleep(2)
            else:
                return None
        except Exception:
            if attempt < retries - 1:
                time.sleep(2)
            else:
                return None
    return None


def zotero_get_all_items() -> list[dict]:
    """Fetch all journal articles from the Zotero group."""
    items = []
    start = 0
    limit = 100
    while True:
        url = (
            f"{ZOTERO_BASE}/items"
            f"?format=json&itemType=journalArticle&limit={limit}&start={start}"
        )
        req = urllib.request.Request(
            url,
            headers={"Zotero-API-Key": ZOTERO_API_KEY, "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30, context=_SSL) as resp:
            batch = json.loads(resp.read())
        if not batch:
            break
        items.extend(batch)
        if len(batch) < limit:
            break
        start += limit
        time.sleep(0.2)
    return items


def zotero_update_abstract(item_key: str, version: int, abstract: str) -> bool:
    """Patch a single Zotero item's abstractNote field."""
    url = f"{ZOTERO_BASE}/items/{item_key}"
    payload = json.dumps({"abstractNote": abstract}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        method="PATCH",
        headers={
            "Zotero-API-Key": ZOTERO_API_KEY,
            "Content-Type": "application/json",
            "If-Unmodified-Since-Version": str(version),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20, context=_SSL) as resp:
            return resp.status == 204
    except Exception as e:
        print(f"    Zotero PATCH error ({item_key}): {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Abstract sources
# ---------------------------------------------------------------------------
def fetch_from_crossref(doi: str) -> str | None:
    """Retrieve abstract from Crossref (publisher-deposited, most reliable)."""
    url = (f"https://api.crossref.org/works/{urllib.parse.quote(doi, safe='')}"
           f"?mailto={CROSSREF_MAILTO}")
    data = get_json(url)
    if not data:
        return None
    abstract = data.get("message", {}).get("abstract", "")
    if not abstract:
        return None
    # Crossref abstracts may contain JATS XML tags; strip them
    text = re.sub(r"<[^>]+>", " ", abstract)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) > 50 else None


def fetch_from_semantic_scholar(doi: str) -> str | None:
    url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}?fields=abstract"
    headers = {"x-api-key": SEMANTIC_SCHOLAR_API_KEY} if SEMANTIC_SCHOLAR_API_KEY else {}
    data = get_json(url, headers=headers)
    if data:
        return data.get("abstract") or None
    return None


def fetch_from_semantic_scholar_by_title(doi: str, title: str) -> str | None:
    """Fall back to Semantic Scholar title search when DOI lookup returns nothing."""
    if not title:
        return None
    encoded = urllib.parse.quote(title[:100])
    url = (
        f"https://api.semanticscholar.org/graph/v1/paper/search"
        f"?query={encoded}&fields=externalIds,abstract&limit=5"
    )
    headers = {"x-api-key": SEMANTIC_SCHOLAR_API_KEY} if SEMANTIC_SCHOLAR_API_KEY else {}
    data = get_json(url, headers=headers)
    if not data:
        return None
    doi_norm = doi.lower().strip()
    for hit in data.get("data", []):
        ext = hit.get("externalIds") or {}
        hit_doi = (ext.get("DOI") or "").lower().strip()
        if hit_doi == doi_norm:
            return hit.get("abstract") or None
    return None


def fetch_from_scopus(doi: str) -> str | None:
    """Retrieve abstract via pybliometrics AbstractRetrieval (Scopus)."""
    try:
        from pybliometrics.utils.startup import init
        init()
        from pybliometrics.scopus import AbstractRetrieval
        a = AbstractRetrieval(doi, id_type="doi", view="FULL")
        return str(a.abstract).strip() if a.abstract else None
    except Exception:
        return None


def fetch_from_sciencedirect(doi: str) -> str | None:
    """Retrieve abstract via pybliometrics ArticleRetrieval (Elsevier/ScienceDirect)."""
    try:
        from pybliometrics.utils.startup import init
        init()
        from pybliometrics.sciencedirect import ArticleRetrieval
        a = ArticleRetrieval(doi, view="FULL")
        if a.abstract:
            return str(a.abstract).strip() or None
        raw = str(a.originalText) if a.originalText else ""
        for pattern in [
            r'<abstract[^>]*>(.*?)</abstract>',
            r'<ce:abstract-sec[^>]*>(.*?)</ce:abstract-sec>',
        ]:
            match = re.search(pattern, raw, re.DOTALL | re.IGNORECASE)
            if match:
                text = re.sub(r'<[^>]+>', ' ', match.group(1))
                text = re.sub(r'\s+', ' ', text).strip()
                if len(text) > 100:
                    return text
        return None
    except Exception:
        return None


def fetch_from_openalex_grobid(doi: str, cache_dir: str) -> str | None:
    """Retrieve abstract from OpenAlex GROBID full-text extraction.

    Downloads the GROBID-parsed TEI XML and extracts the abstract element.
    Last-resort fallback — Crossref and Semantic Scholar abstracts are preferred.
    Uses a local cache to avoid redundant downloads ($0.01 per download).
    """
    if not OPENALEX_API_KEY:
        return None
    encoded = urllib.parse.quote(doi, safe='')
    url = (f"https://api.openalex.org/works/doi:{encoded}"
           f"?select=id,has_content&api_key={OPENALEX_API_KEY}")
    data = get_json(url)
    if not data:
        return None
    has_grobid = (data.get("has_content") or {}).get("grobid_xml", False)
    if not has_grobid:
        return None
    work_id = data["id"].rsplit("/", 1)[-1]

    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, f"{work_id}.xml")

    if os.path.exists(cache_path):
        try:
            root = ET.parse(cache_path).getroot()
        except ET.ParseError:
            os.remove(cache_path)
            return None
    else:
        dl_url = (f"https://content.openalex.org/works/{work_id}.grobid-xml"
                  f"?api_key={OPENALEX_API_KEY}")
        req = urllib.request.Request(dl_url)
        try:
            with urllib.request.urlopen(req, timeout=30, context=_SSL) as resp:
                raw = resp.read()
            xml_bytes = gzip.decompress(raw)
            with open(cache_path, "wb") as f:
                f.write(xml_bytes)
            root = ET.fromstring(xml_bytes)
        except Exception:
            return None

    ns = {"tei": "http://www.tei-c.org/ns/1.0"}
    abstract_el = root.find(".//tei:profileDesc/tei:abstract", ns)
    if abstract_el is None:
        return None
    text = ET.tostring(abstract_el, encoding="unicode", method="text").strip()
    return text if len(text) > 50 else None


# ---------------------------------------------------------------------------
# CSV log
# ---------------------------------------------------------------------------
LOG_FIELDS = ["run_date", "item_key", "doi", "title", "source", "status"]


def open_log(path: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    is_new = not os.path.exists(path)
    fh = open(path, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(fh, fieldnames=LOG_FIELDS)
    if is_new:
        writer.writeheader()
    return fh, writer


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def _process_one(item, cache_dir, dry_run, run_date, log_writer, log_lock,
                 counter, total):
    """Fetch abstract for one item via the source cascade, update Zotero, log."""
    data    = item["data"]
    key     = data["key"]
    version = item["version"]
    doi     = data["DOI"].strip()
    title   = data.get("title", "")[:70]

    # 1. Crossref (publisher-deposited, most reliable)
    abstract = fetch_from_crossref(doi); source = "crossref"
    if not abstract:
        abstract = fetch_from_semantic_scholar(doi); source = "semantic_scholar"
    if not abstract:
        abstract = fetch_from_semantic_scholar_by_title(doi, data.get("title", ""))
        source = "semantic_scholar_title"
    if not abstract:
        abstract = fetch_from_scopus(doi); source = "scopus"
    if not abstract:
        abstract = fetch_from_sciencedirect(doi); source = "sciencedirect"
    if not abstract:
        abstract = fetch_from_openalex_grobid(doi, cache_dir); source = "openalex_grobid"

    with log_lock:
        counter["done"] += 1
        prefix = f"[{counter['done']}/{total}]"

    if not abstract:
        status = "not_found"
        with log_lock:
            counter["skipped"] += 1
            log_writer.writerow({"run_date": run_date, "item_key": key, "doi": doi,
                                 "title": title, "source": "none", "status": status})
        print(f"{prefix} {title:<70} no abstract found", flush=True)
        return

    if dry_run:
        status = "dry_run"
        with log_lock:
            counter["updated"] += 1
            log_writer.writerow({"run_date": run_date, "item_key": key, "doi": doi,
                                 "title": title, "source": source, "status": status})
        print(f"{prefix} {title:<70} found ({source}) [dry-run]", flush=True)
        return

    ok = zotero_update_abstract(key, version, abstract)
    status = "updated" if ok else "update_failed"
    with log_lock:
        if ok:
            counter["updated"] += 1
        else:
            counter["failed"] += 1
        log_writer.writerow({"run_date": run_date, "item_key": key, "doi": doi,
                             "title": title, "source": source, "status": status})
    arrow = "→ updated" if ok else "→ FAILED to update"
    print(f"{prefix} {title:<70} ({source}) {arrow}", flush=True)


def main():
    parser = argparse.ArgumentParser(
        description="Fetch missing abstracts for Zotero journal articles. "
                    "Note: if your search script already ingested WoS/Scopus "
                    "abstracts, this is usually a last-resort fill-in.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch abstracts but do not update Zotero")
    parser.add_argument("--log-csv", default=DEFAULT_LOG_CSV,
                        help=f"Path to log CSV (default: {DEFAULT_LOG_CSV})")
    parser.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR,
                        help=f"GROBID XML cache dir (default: {DEFAULT_CACHE_DIR})")
    parser.add_argument("--workers", type=int, default=4,
                        help="Number of parallel fetch threads (default: 4)")
    parser.add_argument("--filter-keys-file",
                        help="Text file with Zotero item keys, one per line. "
                             "Only those items are processed.")
    args = parser.parse_args()

    if not ZOTERO_API_KEY:
        sys.exit("Error: ZOTERO_API_KEY environment variable not set.")
    if not ZOTERO_GROUP:
        sys.exit("Error: ZOTERO_GROUP environment variable not set.")

    run_date = date.today().isoformat()

    previously_found: set[str] = set()
    if os.path.exists(args.log_csv):
        with open(args.log_csv, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["status"] == "updated":
                    previously_found.add(row["doi"].strip().lower())

    print("Fetching all Zotero items...", end=" ", flush=True)
    all_items = zotero_get_all_items()
    print(f"{len(all_items)} journal articles found.", flush=True)

    # Optional key filter
    if args.filter_keys_file:
        with open(args.filter_keys_file) as f:
            target_keys = {line.strip() for line in f if line.strip()}
        all_items = [it for it in all_items if it.get("key") in target_keys]
        print(f"  After --filter-keys-file: {len(all_items)} items "
              f"(filter list had {len(target_keys)} keys)", flush=True)

    missing = [
        item for item in all_items
        if not (item.get("data", {}).get("abstractNote", "")).strip()
        and (item.get("data", {}).get("DOI", "")).strip()
        and item["data"]["DOI"].strip().lower() not in previously_found
    ]
    print(f"Missing abstracts (with DOI): {len(missing)}", flush=True)
    if not missing:
        print("Nothing to do.", flush=True)
        return

    log_fh, log_writer = open_log(args.log_csv)
    log_lock = threading.Lock()
    counter = {"done": 0, "updated": 0, "skipped": 0, "failed": 0}

    print(f"Fetching with {args.workers} parallel workers...", flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [
            pool.submit(_process_one, item, args.cache_dir, args.dry_run,
                        run_date, log_writer, log_lock, counter, len(missing))
            for item in missing
        ]
        for _ in as_completed(futures):
            # Each worker logs inline; just drain here so exceptions surface
            pass

    log_fh.close()

    print(f"\n{'='*60}", flush=True)
    print("Done.", flush=True)
    print(f"  Updated:       {counter['updated']}", flush=True)
    print(f"  No abstract:   {counter['skipped']}", flush=True)
    print(f"  Update failed: {counter['failed']}", flush=True)
    print(f"  Log:           {args.log_csv}", flush=True)


if __name__ == "__main__":
    main()
