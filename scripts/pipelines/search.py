#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pybliometrics>=3.6",
#     "requests>=2.31",
# ]
# ///
"""Formal systematic search across Scopus / WoS / OpenAlex / Semantic Scholar.

Reads a per-project `search_config.py` (see
`${CLAUDE_PLUGIN_ROOT}/templates/search_config.py`) for year window,
journal ISSN list, Scopus/WoS `QUERY_DEFS`, and OpenAlex/Semantic
Scholar `BLOCK_A_TERMS` / `BLOCK_B_TERMS`. Dispatches each source
via the `searchers/` registry, deduplicates across databases by DOI
with a title+first-author fallback, and writes:

    <output-dir>/search_results_raw.csv   — pre-dedup, all hits
    <output-dir>/search_results.csv       — deduplicated union
    <metadata-dir>/search_metadata.json   — parameters, timestamps, counts
    <metadata-dir>/search_run.json        — DOI-set hash (integrity gatekeeper)

The DOI-set hash in `search_run.json` is the single load-bearing
invariant: downstream test suites compare each manuscript render
against this hash to catch silent scope changes.

Usage:
    uv run search.py --config ./search_config.py
    uv run search.py --config ./search_config.py --databases scopus,wos
    uv run search.py --config ./search_config.py --databases openalex
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
from datetime import UTC, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from searchers import (  # noqa: E402
    SEARCH_ROW_FIELDS,
    SearchContext,
    searchers_by_name,
)


def _load_config(path: str):
    spec = importlib.util.spec_from_file_location("search_config", path)
    assert spec is not None and spec.loader is not None, (
        f"cannot load search config: {path}"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for attr in ("FROM_YEAR", "TO_YEAR", "JOURNALS"):
        if not hasattr(mod, attr):
            sys.exit(f"ERROR: {path} is missing `{attr}`.")
    return mod


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="./search_config.py",
                        help="Path to the project's search_config.py.")
    parser.add_argument("--output-dir", default="analysis/raw",
                        help="Where to write CSV outputs (default: analysis/raw).")
    parser.add_argument("--metadata-dir", default=".",
                        help="Where to write search_metadata.json / "
                             "search_run.json (default: current directory).")
    parser.add_argument("--databases", default="",
                        help="Comma-separated source names (scopus, wos, "
                             "openalex, semantic_scholar). Default: every "
                             "source with usable credentials.")
    args = parser.parse_args()

    cfg = _load_config(args.config)
    ctx = SearchContext(
        from_year=cfg.FROM_YEAR,
        to_year=cfg.TO_YEAR,
        issns=list(cfg.JOURNALS.keys()),
        mailto=os.environ.get("CROSSREF_MAILTO", ""),
    )

    registry = searchers_by_name()
    selected: list[str]
    if args.databases:
        selected = [n.strip() for n in args.databases.split(",") if n.strip()]
        unknown = [n for n in selected if n not in registry]
        if unknown:
            sys.exit(f"ERROR: unknown database(s): {unknown}. "
                     f"Available: {list(registry)}")
    else:
        # Default: every source where credentials_error() returns None
        selected = [name for name, src in registry.items()
                    if src.credentials_error(ctx) is None]
        if not selected:
            sys.exit("ERROR: no database has usable credentials. Check the "
                     "wizard set-up or pass --databases explicitly.")

    output_dir = Path(args.output_dir)
    metadata_dir = Path(args.metadata_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    metadata_dir.mkdir(parents=True, exist_ok=True)
    csv_raw = output_dir / "search_results_raw.csv"
    csv_dedup = output_dir / "search_results.csv"
    metadata_path = metadata_dir / "search_metadata.json"
    run_marker = metadata_dir / "search_run.json"

    run_start = datetime.now(UTC).isoformat()
    print(f"[{run_start}] Starting search")
    print(f"  Databases: {', '.join(selected)}")
    print(f"  Journals:  {len(cfg.JOURNALS)}")
    print(f"  Years:     {ctx.from_year}–{ctx.to_year}")
    if hasattr(cfg, "QUERY_DEFS"):
        print(f"  Queries:   {len(cfg.QUERY_DEFS)} (Scopus/WoS)")
    if getattr(cfg, "BLOCK_A_TERMS", None) or getattr(cfg, "BLOCK_B_TERMS", None):
        a = len(getattr(cfg, "BLOCK_A_TERMS", []) or [])
        b = len(getattr(cfg, "BLOCK_B_TERMS", []) or [])
        print(f"  Blocks:    A={a} terms, B={b} terms (OpenAlex/S2)")
    print()

    all_rows: list[dict] = []
    counts: dict = {}
    for name in selected:
        source = registry[name]
        err = source.credentials_error(ctx)
        if err:
            print(f"SKIP {name}: {err}", flush=True)
            continue
        print(f"── {name} ──", flush=True)
        if name in ("scopus", "wos") and not hasattr(cfg, "QUERY_DEFS"):
            print(f"  ({name} needs QUERY_DEFS in the config — skipping)",
                  flush=True)
            continue
        if (name in ("openalex", "semantic_scholar")
            and not (getattr(cfg, "BLOCK_A_TERMS", None)
                     or getattr(cfg, "BLOCK_B_TERMS", None))):
            print(f"  ({name} needs BLOCK_A_TERMS / BLOCK_B_TERMS — skipping)",
                  flush=True)
            continue
        rows = source.run(cfg, ctx)
        counts[name] = len(rows)
        all_rows.extend(rows)
        print()

    print(f"Total rows before dedup: {len(all_rows)}")

    with csv_raw.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SEARCH_ROW_FIELDS,
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(all_rows)
    print(f"Raw CSV:   {csv_raw}")

    deduped, merged = _dedup(all_rows)
    no_doi_count = sum(1 for r in deduped if not r["doi"])
    print(f"After DOI dedup:        {len(deduped) + merged}")
    print(f"  Merged no-DOI → DOI:  {merged}")
    print(f"After full dedup:       {len(deduped)} ({no_doi_count} without DOI)")

    with csv_dedup.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SEARCH_ROW_FIELDS,
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(deduped)
    print(f"Dedup CSV: {csv_dedup}")

    run_end = datetime.now(UTC).isoformat()
    sorted_dois = sorted(r["doi"] for r in deduped if r["doi"])
    doi_hash = hashlib.sha256("\n".join(sorted_dois).encode()).hexdigest()

    metadata: dict[str, object] = {
        "search_date_start": run_start,
        "search_date_end": run_end,
        "databases": selected,
        "from_year": ctx.from_year,
        "to_year": ctx.to_year,
        "journals": {issn: cfg.JOURNALS[issn][1] for issn in ctx.issns},
        "journal_count": len(cfg.JOURNALS),
        "per_database_counts": counts,
        "total_raw_rows": len(all_rows),
        "total_unique_records": len(deduped),
        "records_without_doi": no_doi_count,
    }
    if hasattr(cfg, "QUERY_DEFS"):
        metadata["query_defs"] = [
            {"label": lbl, "scopus": sc, "wos": wc}
            for lbl, sc, wc in cfg.QUERY_DEFS
        ]
    if getattr(cfg, "BLOCK_A_TERMS", None):
        metadata["block_a_terms"] = list(cfg.BLOCK_A_TERMS)
    if getattr(cfg, "BLOCK_B_TERMS", None):
        metadata["block_b_terms"] = list(cfg.BLOCK_B_TERMS)

    metadata_path.write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    run_marker.write_text(
        json.dumps({
            "run_timestamp": run_start,
            "unique_records": len(deduped),
            "unique_dois": len(sorted_dois),
            "doi_sha256": doi_hash,
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"\nDone. {len(deduped)} unique records.")
    print(f"  Metadata:   {metadata_path}")
    print(f"  Run marker: {run_marker}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
