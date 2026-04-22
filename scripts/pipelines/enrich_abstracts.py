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
# ]
# ///
"""Enrich Zotero items by fetching missing abstracts.

For each journal article in the library that does not have an
`abstractNote`, run the abstract-source cascade
(see `fetchers.abstract_sources`) until one source returns text, then
patch the Zotero item via `ZoteroClient.update_abstract` (pyzotero's
`update_item`).

The fetcher priority matches the legacy cascade:
    Crossref → Semantic Scholar → Scopus → ScienceDirect → OpenAlex GROBID

--sources filters to a subset, same as enrich_pdfs.py.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = SCRIPT_DIR.parent
for _p in (str(SCRIPT_DIR), str(SCRIPTS_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fetchers  # noqa: E402
import http_client  # noqa: E402
import zotero_io  # noqa: E402
from core.config_loader import get, require  # noqa: E402

DEFAULT_LOG_CSV = os.path.join("output", "abstract_fetch_log.csv")
DEFAULT_CACHE_DIR = os.path.join("output", "fulltext_cache")

LOG_FIELDS = ["run_date", "item_key", "doi", "title", "source", "status"]


@dataclass
class Config:
    elsevier_api_key: str = ""
    openalex_api_key: str = ""
    semantic_scholar_api_key: str = ""
    wos_api_key_extended: str = ""
    wos_api_key: str = ""
    crossref_mailto: str = ""


def _load_config() -> Config:
    return Config(
        elsevier_api_key=get("elsevier", "api_key", env="ELSEVIER_API_KEY"),
        openalex_api_key=get("openalex", "api_key", env="OPENALEX_API_KEY"),
        semantic_scholar_api_key=get(
            "semantic_scholar", "api_key", env="SEMANTIC_SCHOLAR_API_KEY",
        ),
        wos_api_key_extended=get("wos", "expanded_key", env="WOS_API_KEY_EXTENDED"),
        wos_api_key=get("wos", "starter_key", env="WOS_API_KEY"),
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


def _already_done(log_path: str) -> set[str]:
    if not os.path.exists(log_path):
        return set()
    with open(log_path, newline="", encoding="utf-8") as f:
        return {
            (r.get("doi") or "").strip().lower()
            for r in csv.DictReader(f)
            if r.get("status") == "updated"
        }


def _try_cascade(
    item: dict,
    sources: list,
    cache_dir: str,
) -> tuple[str, str] | None:
    """Try each abstract fetcher in priority order.

    Returns (abstract_text, source_name) on first hit.
    """
    data = item.get("data", {})
    doi = (data.get("DOI") or "").strip()
    if not doi:
        return None
    title = (data.get("title") or "").strip()
    for src in sources:
        try:
            text = src.fetch_abstract(doi, title=title or None, cache_dir=cache_dir)
        except NotImplementedError:
            continue
        except Exception as e:
            print(f"    {src.name}: {e}", flush=True)
            continue
        if text:
            return text, src.name
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sources", default="",
        help="Comma-separated fetcher names. Default: full cascade "
             "(crossref,semantic_scholar,scopus,sciencedirect,openalex).",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch abstracts, do not patch Zotero.")
    parser.add_argument("--log-csv", default=DEFAULT_LOG_CSV,
                        help=f"Path to log CSV (default: {DEFAULT_LOG_CSV}).")
    parser.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR,
                        help=f"GROBID XML cache dir (default: {DEFAULT_CACHE_DIR}).")
    parser.add_argument("--workers", type=int, default=4,
                        help="Parallel fetch threads (default: 4).")
    parser.add_argument("--filter-keys-file",
                        help="Text file with Zotero item keys, one per line.")
    parser.add_argument(
        "--group", default=os.environ.get("ZOTERO_GROUP", ""),
        help="Zotero group ID (per-project; default: $ZOTERO_GROUP). "
             "If omitted and only one group is accessible, auto-selected.",
    )
    args = parser.parse_args()

    source_names = [s.strip() for s in args.sources.split(",") if s.strip()]
    require("zotero", "api_key", env="ZOTERO_API_KEY")

    os.makedirs(args.cache_dir, exist_ok=True)
    run_date = date.today().isoformat()
    done_dois = _already_done(args.log_csv)

    config = _load_config()
    session = http_client.build_session(mailto=config.crossref_mailto)
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
        print(f"  After --filter-keys-file: {len(all_items)} items.",
              flush=True)

    missing = [
        it for it in all_items
        if not (it.get("data", {}).get("abstractNote") or "").strip()
        and (it.get("data", {}).get("DOI") or "").strip()
        and it["data"]["DOI"].strip().lower() not in done_dois
    ]
    print(f"Missing abstracts (with DOI): {len(missing)}", flush=True)
    if not missing:
        return 0

    sources = fetchers.abstract_sources(session, config)
    if source_names:
        sources = [s for s in sources if s.name in source_names]
    if not sources:
        print(f"ERROR: no abstract fetchers matched --sources={args.sources!r}",
              file=sys.stderr)
        return 2
    print(f"Active fetchers: {[s.name for s in sources]}", flush=True)

    log_fh, log_writer = _open_log(args.log_csv)
    log_lock = threading.Lock()
    counters = {"updated": 0, "skipped": 0, "failed": 0, "done": 0}
    total = len(missing)

    def _process(item: dict) -> None:
        data = item.get("data", {})
        key = item["key"]
        doi = (data.get("DOI") or "").strip()
        title = (data.get("title") or "")[:70]

        result = _try_cascade(item, sources, args.cache_dir)

        with log_lock:
            counters["done"] += 1
            prefix = f"[{counters['done']}/{total}]"

        if result is None:
            with log_lock:
                counters["skipped"] += 1
                log_writer.writerow({
                    "run_date": run_date, "item_key": key, "doi": doi,
                    "title": title, "source": "none", "status": "not_found",
                })
            print(f"{prefix} {title:<70} no abstract found", flush=True)
            return

        abstract, source = result

        if args.dry_run:
            with log_lock:
                counters["updated"] += 1
                log_writer.writerow({
                    "run_date": run_date, "item_key": key, "doi": doi,
                    "title": title, "source": source, "status": "dry_run",
                })
            print(f"{prefix} {title:<70} found ({source}) [dry-run]",
                  flush=True)
            return

        try:
            zot.update_abstract(key, abstract)
            status = "updated"
            ok = True
        except Exception as e:
            status = "update_failed"
            ok = False
            print(f"{prefix} {title:<70} ({source}) update failed: {e}",
                  flush=True)

        with log_lock:
            if ok:
                counters["updated"] += 1
            else:
                counters["failed"] += 1
            log_writer.writerow({
                "run_date": run_date, "item_key": key, "doi": doi,
                "title": title, "source": source, "status": status,
            })
        if ok:
            print(f"{prefix} {title:<70} ({source}) → updated", flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(_process, it) for it in missing]
        for fut in as_completed(futures):
            fut.result()          # re-raise unexpected exceptions

    log_fh.close()
    print(
        f"\nDone. updated={counters['updated']}, "
        f"skipped={counters['skipped']}, failed={counters['failed']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
