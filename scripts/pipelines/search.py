#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pybliometrics>=3.6",
#     "requests>=2.31",
# ]
# ///
"""Formal systematic bibliographic search across Scopus and Web of Science.

Reads a per-project `search_config.py` (see
`${CLAUDE_PLUGIN_ROOT}/templates/search_config.py`) for year window,
journal ISSN list, and Boolean query definitions. Runs each query
against Scopus (via pybliometrics) and optionally Web of Science
Expanded API. Deduplicates by DOI with a title+first-author fallback
for no-DOI records. Writes:

    <output-dir>/search_results_raw.csv   — pre-dedup, all hits
    <output-dir>/search_results.csv       — deduplicated union
    <output-dir>/search_metadata.json     — parameters, timestamps, counts
    <output-dir>/search_run.json          — DOI-set hash (integrity gatekeeper)

The DOI-set hash in `search_run.json` is the single load-bearing
invariant: downstream test suites compare each manuscript render
against this hash to catch silent scope changes.

Usage:
    uv run search.py --config ./search_config.py
    uv run search.py --config ./search_config.py --wos --output-dir analysis/raw
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

try:
    import requests
    from pybliometrics import init as pyb_init
    from pybliometrics.scopus import ScopusSearch
except ImportError:
    sys.exit(
        "ERROR: dependencies not available. Run via `uv run`; the PEP 723 "
        "block at the top declares pybliometrics + requests."
    )


WOS_ENDPOINT = "https://api.clarivate.com/api/wos"


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def _load_config(path: str):
    spec = importlib.util.spec_from_file_location("search_config", path)
    assert spec is not None and spec.loader is not None, (
        f"cannot load search config: {path}"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for attr in ("FROM_YEAR", "TO_YEAR", "JOURNALS", "QUERY_DEFS"):
        if not hasattr(mod, attr):
            sys.exit(f"ERROR: {path} is missing `{attr}`.")
    return mod


# ---------------------------------------------------------------------------
# Scopus
# ---------------------------------------------------------------------------


def _scopus_full_query(core: str, issns: list[str],
                       from_year: int, to_year: int) -> str:
    issn_part = " OR ".join(issns)
    return (
        f"{core} AND ISSN({issn_part}) "
        f"AND PUBYEAR > {from_year - 1} AND PUBYEAR < {to_year + 1}"
    )


def _run_scopus_query(label: str, core: str, issns: list[str],
                      from_year: int, to_year: int) -> list[dict]:
    q = _scopus_full_query(core, issns, from_year, to_year)
    print(f"  Scopus {label}: ", end="", flush=True)
    s = ScopusSearch(q, download=True)
    results = s.results or []
    print(f"{len(results)} results", flush=True)
    rows = []
    for r in results:
        doi = (r.doi or "").strip().lower()
        rows.append({
            "db": "scopus",
            "query": label,
            "doi": doi,
            "title": r.title or "",
            "authors": r.author_names or "",
            "year": r.coverDate[:4] if r.coverDate else "",
            "source": r.publicationName or "",
            "issn": r.issn or "",
            "cited_by": r.citedby_count or 0,
            "scopus_id": r.eid or "",
            "wos_id": "",
            "abstract": r.description or "",
        })
    return rows


# ---------------------------------------------------------------------------
# Web of Science Expanded API
# ---------------------------------------------------------------------------


def _wos_full_query(core: str, issns: list[str],
                    from_year: int, to_year: int) -> str:
    issn_part = " OR ".join(issns)
    return f"{core} AND IS=({issn_part}) AND PY={from_year}-{to_year}"


def _wos_fetch_page(api_key: str, query: str, first_record: int,
                    count: int = 100) -> dict:
    resp = requests.get(
        WOS_ENDPOINT,
        headers={"X-ApiKey": api_key},
        params={
            "databaseId": "WOS",
            "usrQuery": query,
            "count": count,
            "firstRecord": first_record,
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def _extract_wos_record(rec: dict, label: str) -> dict:
    uid = rec.get("UID", "")
    static = rec.get("static_data", {})
    dynamic = rec.get("dynamic_data", {})

    titles = static.get("summary", {}).get("titles", {}).get("title", [])
    title = next((t["content"] for t in titles if t.get("type") == "item"), "")
    source = next((t["content"] for t in titles if t.get("type") == "source"), "")

    names = static.get("summary", {}).get("names", {}).get("name", [])
    if isinstance(names, dict):
        names = [names]
    authors = "; ".join(
        n.get("display_name", n.get("full_name", ""))
        for n in names if n.get("role") == "author"
    )

    year = str(static.get("summary", {}).get("pub_info", {}).get("pubyear", ""))

    id_list = (dynamic.get("cluster_related", {})
                      .get("identifiers", {})
                      .get("identifier", []))
    if isinstance(id_list, dict):
        id_list = [id_list]
    doi = next((i["value"].strip().lower() for i in id_list
                if i.get("type") == "doi"), "")
    issn = next((i["value"].strip() for i in id_list
                 if i.get("type") == "issn"), "")

    abstracts = (static.get("fullrecord_metadata", {})
                       .get("abstracts", {})
                       .get("abstract", {}))
    abstract = ""
    if isinstance(abstracts, dict):
        ab_texts = abstracts.get("abstract_text", {}).get("p", "")
        if isinstance(ab_texts, list):
            abstract = " ".join(ab_texts)
        else:
            abstract = str(ab_texts)

    cited_by = 0
    times_cited = (dynamic.get("citation_related", {})
                          .get("tc_list", {})
                          .get("silo_tc", {}))
    if isinstance(times_cited, dict):
        cited_by = int(times_cited.get("local_count", 0))

    return {
        "db": "wos",
        "query": label,
        "doi": doi,
        "title": title,
        "authors": authors,
        "year": year,
        "source": source,
        "issn": issn,
        "cited_by": cited_by,
        "scopus_id": "",
        "wos_id": uid,
        "abstract": abstract,
    }


def _run_wos_query(api_key: str, label: str, core: str, issns: list[str],
                   from_year: int, to_year: int) -> list[dict]:
    q = _wos_full_query(core, issns, from_year, to_year)
    print(f"  WoS    {label}: ", end="", flush=True)

    data = _wos_fetch_page(api_key, q, first_record=1, count=1)
    total = data.get("QueryResult", {}).get("RecordsFound", 0)

    rows: list[dict] = []
    page_size = 100
    first = 1
    while first <= total:
        data = _wos_fetch_page(api_key, q, first_record=first, count=page_size)
        recs_data = data.get("Data", {}).get("Records", {}).get("records", "")
        if not recs_data:
            break
        recs = recs_data.get("REC", [])
        if isinstance(recs, dict):
            recs = [recs]
        for rec in recs:
            rows.append(_extract_wos_record(rec, label))
        first += page_size
        time.sleep(0.3)

    print(f"{len(rows)} results  (API total: {total})", flush=True)
    return rows


# ---------------------------------------------------------------------------
# Dedup + output
# ---------------------------------------------------------------------------


def _title_author_key(title: str, authors: str) -> str:
    t = re.sub(r"\W+", " ", (title or "").lower()).strip()
    first_last = ""
    if authors:
        first_last = authors.split(";")[0].split(",")[0].strip().lower()
    return f"{t}|{first_last}" if t else ""


def _dedup(rows: list[dict]) -> tuple[list[dict], int]:
    by_doi: dict[str, dict] = {}
    no_doi: list[dict] = []
    for r in rows:
        doi = r["doi"].strip()
        if not doi:
            no_doi.append(r)
        elif doi not in by_doi:
            by_doi[doi] = r
        else:
            # Merge abstracts across databases
            if not by_doi[doi]["abstract"] and r["abstract"]:
                by_doi[doi]["abstract"] = r["abstract"]

    title_to_doi: dict[str, str] = {}
    for doi, r in by_doi.items():
        tk = _title_author_key(r.get("title", ""), r.get("authors", ""))
        if tk:
            title_to_doi[tk] = doi

    unresolved: list[dict] = []
    merged = 0
    for r in no_doi:
        tk = _title_author_key(r.get("title", ""), r.get("authors", ""))
        if tk and tk in title_to_doi:
            keeper = by_doi[title_to_doi[tk]]
            if not keeper["abstract"] and r.get("abstract"):
                keeper["abstract"] = r["abstract"]
            merged += 1
        else:
            unresolved.append(r)
    return list(by_doi.values()) + unresolved, merged


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="./search_config.py",
                        help="Path to the project's search_config.py "
                             "(default: ./search_config.py).")
    parser.add_argument("--output-dir", default="analysis/raw",
                        help="Where to write CSV / JSON outputs "
                             "(default: analysis/raw).")
    parser.add_argument("--metadata-dir", default=".",
                        help="Where to write search_metadata.json and "
                             "search_run.json (default: current directory).")
    parser.add_argument("--wos", action="store_true",
                        help="Include Web of Science search (requires "
                             "WOS_API_KEY_EXTENDED env var).")
    parser.add_argument("--skip-scopus", action="store_true",
                        help="Skip Scopus (useful for WoS-only piloting).")
    args = parser.parse_args()

    cfg = _load_config(args.config)
    issns = list(cfg.JOURNALS.keys())
    from_year = cfg.FROM_YEAR
    to_year = cfg.TO_YEAR

    wos_api_key = os.environ.get("WOS_API_KEY_EXTENDED", "")
    if args.wos and not wos_api_key:
        sys.exit("ERROR: --wos requested but WOS_API_KEY_EXTENDED not set.")

    output_dir = Path(args.output_dir)
    metadata_dir = Path(args.metadata_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    metadata_dir.mkdir(parents=True, exist_ok=True)
    csv_raw = output_dir / "search_results_raw.csv"
    csv_dedup = output_dir / "search_results.csv"
    metadata_path = metadata_dir / "search_metadata.json"
    run_marker = metadata_dir / "search_run.json"

    if not args.skip_scopus:
        pyb_init()

    run_start = datetime.now(UTC).isoformat()
    dbs: list[str] = []
    if not args.skip_scopus:
        dbs.append("Scopus")
    if args.wos:
        dbs.append("Web of Science (Expanded API)")

    print(f"[{run_start}] Starting search")
    print(f"  Databases: {', '.join(dbs)}")
    print(f"  Journals:  {len(cfg.JOURNALS)}")
    print(f"  Years:     {from_year}–{to_year}")
    print(f"  Queries:   {len(cfg.QUERY_DEFS)}")
    print()

    all_rows: list[dict] = []
    counts: dict = {}
    for label, scopus_core, wos_core in cfg.QUERY_DEFS:
        print(f"--- {label} ---", flush=True)
        s_rows = ([] if args.skip_scopus
                  else _run_scopus_query(label, scopus_core, issns,
                                         from_year, to_year))
        time.sleep(0.5)
        w_rows = ([] if not args.wos
                  else _run_wos_query(wos_api_key, label, wos_core, issns,
                                      from_year, to_year))
        if args.wos:
            time.sleep(0.5)
        counts[label] = {"scopus": len(s_rows), "wos": len(w_rows)}
        all_rows.extend(s_rows)
        all_rows.extend(w_rows)
        print()

    print(f"Total rows before dedup: {len(all_rows)}")

    fieldnames = ["db", "query", "doi", "title", "authors", "year",
                  "source", "issn", "cited_by", "scopus_id", "wos_id",
                  "abstract"]
    with csv_raw.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(all_rows)
    print(f"Raw CSV:   {csv_raw}")

    deduped, merged = _dedup(all_rows)
    no_doi_count = sum(1 for r in deduped if not r["doi"])
    print(f"After DOI dedup:        {len(deduped) + merged}")
    print(f"  Merged no-DOI → DOI:  {merged}")
    print(f"After full dedup:       {len(deduped)} ({no_doi_count} without DOI)")

    with csv_dedup.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(deduped)
    print(f"Dedup CSV: {csv_dedup}")

    run_end = datetime.now(UTC).isoformat()
    sorted_dois = sorted(r["doi"] for r in deduped if r["doi"])
    doi_hash = hashlib.sha256("\n".join(sorted_dois).encode()).hexdigest()

    metadata = {
        "search_date_start": run_start,
        "search_date_end": run_end,
        "databases": dbs,
        "from_year": from_year,
        "to_year": to_year,
        "journals": {issn: cfg.JOURNALS[issn][1] for issn in issns},
        "journal_count": len(cfg.JOURNALS),
        "queries": {
            label: {
                "scopus": scopus_core,
                "wos": wos_core,
                "counts": counts.get(label, {}),
            }
            for label, scopus_core, wos_core in cfg.QUERY_DEFS
        },
        "total_raw_rows": len(all_rows),
        "total_unique_records": len(deduped),
        "records_without_doi": no_doi_count,
    }
    metadata_path.write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    run_data = {
        "run_timestamp": run_start,
        "unique_records": len(deduped),
        "unique_dois": len(sorted_dois),
        "doi_sha256": doi_hash,
    }
    run_marker.write_text(
        json.dumps(run_data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"\nDone. {len(deduped)} unique records.")
    print(f"  Metadata:   {metadata_path}")
    print(f"  Run marker: {run_marker}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
