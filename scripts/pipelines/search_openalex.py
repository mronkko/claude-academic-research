#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "requests>=2.31",
# ]
# ///
"""Systematic search via the OpenAlex REST API (free, no key required).

Runs two block queries (from the project's `search_config.py`:
`BLOCK_A_TERMS` and `BLOCK_B_TERMS`) separately, then merges and
deduplicates by DOI. Running blocks separately maximises recall
because OpenAlex's `search=` parameter is relevance-ranked — a single
combined A+B query drops papers that match one block strongly but the
other weakly.

Uses `CROSSREF_MAILTO` (if set) for the polite-pool identifier.

Note: OpenAlex's `abstract_inverted_index` is often reconstructed from
GROBID full-text parsing and may return body-text fragments rather
than real abstracts. The reconstructed abstracts go into the search
CSV as a best-effort starting point; downstream `fetch_abstracts.py`
re-fetches proper abstracts from Crossref / Semantic Scholar / Scopus.

Usage:
    uv run search_openalex.py --config ./search_config.py
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit(
        "ERROR: requests not available. Run via `uv run`; the PEP 723 "
        "block at the top declares the dependency."
    )

PER_PAGE = 200              # max per page accepted by OpenAlex
RATE_LIMIT_SLEEP = 0.2      # polite pool delay between requests


def _load_config(path: str):
    spec = importlib.util.spec_from_file_location("search_config", path)
    assert spec is not None and spec.loader is not None, (
        f"cannot load search config: {path}"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for attr in ("FROM_YEAR", "TO_YEAR", "JOURNALS",
                 "BLOCK_A_TERMS", "BLOCK_B_TERMS"):
        if not hasattr(mod, attr):
            sys.exit(f"ERROR: {path} is missing `{attr}`.")
    return mod


def _build_filter(issns: list[str], from_year: int, to_year: int) -> str:
    return (
        f"primary_location.source.issn:{'|'.join(issns)},"
        f"publication_year:{from_year}-{to_year},"
        f"type:article"
    )


def _build_block_query(terms: list[str]) -> str:
    return " OR ".join(f'"{t}"' for t in terms)


def _fetch_page(search_query: str, filter_str: str, page: int,
                mailto: str) -> dict:
    params = {
        "search": search_query,
        "filter": filter_str,
        "page": page,
        "per_page": PER_PAGE,
        "select": ",".join([
            "id", "doi", "title", "publication_year", "publication_date",
            "cited_by_count", "type", "authorships",
            "primary_location", "open_access", "abstract_inverted_index",
        ]),
    }
    if mailto:
        params["mailto"] = mailto
    resp = requests.get("https://api.openalex.org/works", params=params,
                        timeout=60)
    resp.raise_for_status()
    return resp.json()


def _fetch_all_pages(search_query: str, filter_str: str, label: str,
                     mailto: str) -> list[dict]:
    all_works: list[dict] = []
    page = 1
    total: int | None = None
    while True:
        print(f"  [{label}] page {page}...", end=" ", flush=True)
        data = _fetch_page(search_query, filter_str, page, mailto)
        if total is None:
            total = data["meta"]["count"]
            print(f"(total: {total})", flush=True)
        else:
            print(flush=True)
        results = data.get("results", [])
        if not results:
            break
        all_works.extend(results)
        if page * PER_PAGE >= min(total, 10000):
            break
        page += 1
        time.sleep(RATE_LIMIT_SLEEP)
    print(f"  [{label}] fetched {len(all_works)} across {page} pages",
          flush=True)
    return all_works


def _reconstruct_abstract(inverted_index: dict | None) -> str:
    if not inverted_index:
        return ""
    positions: list[tuple[int, str]] = []
    for word, ps in inverted_index.items():
        for p in ps:
            positions.append((p, word))
    positions.sort()
    return " ".join(w for _, w in positions)


def _work_to_row(w: dict) -> dict:
    doi = (w.get("doi") or "").replace("https://doi.org/", "")
    primary = w.get("primary_location") or {}
    src = primary.get("source") or {}
    oa = w.get("open_access") or {}
    authorships = w.get("authorships") or []
    authors = "; ".join(
        a.get("author", {}).get("display_name", "") for a in authorships
        if a.get("author", {}).get("display_name")
    )
    return {
        "openalex_id": w.get("id", ""),
        "doi": doi,
        "title": w.get("title", "") or "",
        "authors": authors,
        "publication_year": w.get("publication_year", "") or "",
        "publication_date": w.get("publication_date", "") or "",
        "cited_by_count": w.get("cited_by_count", 0) or 0,
        "source_name": src.get("display_name", "") or "",
        "source_issn": src.get("issn_l", "") or "",
        "source_id": src.get("id", "") or "",
        "oa_status": oa.get("oa_status", "") or "",
        "oa_url": oa.get("oa_url", "") or "",
        "abstract": _reconstruct_abstract(w.get("abstract_inverted_index")),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="./search_config.py",
                        help="Path to search_config.py (default: ./search_config.py).")
    parser.add_argument("--output-dir", default="analysis/raw",
                        help="Output CSV dir (default: analysis/raw).")
    parser.add_argument("--metadata-dir", default=".",
                        help="Where to write search_metadata.json / search_run.json "
                             "(default: current directory).")
    args = parser.parse_args()

    cfg = _load_config(args.config)
    issns = list(cfg.JOURNALS.keys())
    mailto = os.environ.get("CROSSREF_MAILTO", "")

    output_dir = Path(args.output_dir)
    metadata_dir = Path(args.metadata_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    metadata_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "search_results.csv"
    metadata_path = metadata_dir / "search_metadata.json"
    run_marker = metadata_dir / "search_run.json"

    filter_str = _build_filter(issns, cfg.FROM_YEAR, cfg.TO_YEAR)
    query_a = _build_block_query(cfg.BLOCK_A_TERMS)
    query_b = _build_block_query(cfg.BLOCK_B_TERMS)

    run_start = datetime.now(UTC).isoformat()
    print(f"[{run_start}] OpenAlex search")
    print(f"  Journals: {len(cfg.JOURNALS)}")
    print(f"  Years:    {cfg.FROM_YEAR}–{cfg.TO_YEAR}")
    print(f"  Block A:  {len(cfg.BLOCK_A_TERMS)} terms")
    print(f"  Block B:  {len(cfg.BLOCK_B_TERMS)} terms")
    print()

    print("── Block A ──")
    works_a = _fetch_all_pages(query_a, filter_str, "A", mailto)
    print()
    print("── Block B ──")
    works_b = _fetch_all_pages(query_b, filter_str, "B", mailto)
    print()

    # Merge + dedupe by DOI
    seen: set[str] = set()
    unique: list[dict] = []
    no_doi = 0
    overlap = 0
    doi_source: dict[str, set] = {}
    for w in works_a:
        doi = (w.get("doi") or "").replace("https://doi.org/", "")
        if doi:
            doi_source.setdefault(doi, set()).add("A")
            if doi not in seen:
                seen.add(doi)
                unique.append(w)
        else:
            no_doi += 1
            unique.append(w)
    for w in works_b:
        doi = (w.get("doi") or "").replace("https://doi.org/", "")
        if doi:
            if doi in doi_source:
                doi_source[doi].add("B")
                overlap += 1
            else:
                doi_source[doi] = {"B"}
            if doi not in seen:
                seen.add(doi)
                unique.append(w)
        else:
            no_doi += 1
            unique.append(w)

    print("── Merge ──")
    print(f"  Block A unique:  {sum(1 for ss in doi_source.values() if 'A' in ss and 'B' not in ss)}")
    print(f"  Block B unique:  {sum(1 for ss in doi_source.values() if 'B' in ss and 'A' not in ss)}")
    print(f"  Overlap (A∩B):   {overlap}")
    print(f"  Total unique:    {len(unique)} ({no_doi} without DOI)")

    fieldnames = [
        "openalex_id", "doi", "title", "authors", "publication_year",
        "publication_date", "cited_by_count", "source_name", "source_issn",
        "source_id", "oa_status", "oa_url", "abstract",
    ]
    rows = [_work_to_row(w) for w in unique]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"  CSV: {csv_path}")

    run_end = datetime.now(UTC).isoformat()
    sorted_dois = sorted(seen)
    doi_hash = hashlib.sha256("\n".join(sorted_dois).encode()).hexdigest()

    metadata_path.write_text(json.dumps({
        "search_date_start": run_start,
        "search_date_end": run_end,
        "databases": ["OpenAlex"],
        "api_endpoint": "https://api.openalex.org/works",
        "from_year": cfg.FROM_YEAR,
        "to_year": cfg.TO_YEAR,
        "queries": {
            "block_a": {"terms": cfg.BLOCK_A_TERMS, "query": query_a,
                        "api_results": len(works_a)},
            "block_b": {"terms": cfg.BLOCK_B_TERMS, "query": query_b,
                        "api_results": len(works_b)},
        },
        "filter": filter_str,
        "journals": {issn: info[1] for issn, info in cfg.JOURNALS.items()},
        "journal_count": len(cfg.JOURNALS),
        "overlap": overlap,
        "total_unique_records": len(unique),
        "records_without_doi": no_doi,
        "per_page": PER_PAGE,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    run_marker.write_text(json.dumps({
        "run_timestamp": run_start,
        "unique_records": len(unique),
        "unique_dois": len(sorted_dois),
        "doi_sha256": doi_hash,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"  Metadata: {metadata_path}")
    print(f"  Run marker: {run_marker}")
    print(f"\nDone. {len(unique)} unique works.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
