#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Filter and trim a deduplicated search-results CSV.

Reads `search_results.csv` (the output of `search.py`), applies
optional year-range and top-N filters, and writes a new CSV with
the same column shape. Used when the formal search returns more
items than a pilot stage can comfortably screen — pin a year window
or take the most-recent N before importing into Zotero.

Drop-in for the previous shape ("write a heredoc to keep top 100 by
year") — invoke this instead so the trim is auditable, repeatable
across sessions, and not dependent on the agent reading the CSV
into context.

Sort order: descending by `year` (most recent first), with rows
that have no year falling to the bottom.

Usage:
    uv run filter_search_results.py --input search_results.csv \\
        --output search_results_filtered.csv --top-n 100
    uv run filter_search_results.py --input search_results.csv \\
        --output recent.csv --year-min 2019 --year-max 2026
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path


def _year_or_zero(row: dict) -> int:
    """Parse the `year` cell as int; return 0 for empty / non-numeric."""
    raw = (row.get("year") or "").strip()
    try:
        return int(raw[:4]) if raw else 0
    except ValueError:
        return 0


def filter_rows(
    rows: list[dict],
    *,
    year_min: int | None,
    year_max: int | None,
    top_n: int | None,
) -> list[dict]:
    """Apply year-range filter, sort by year desc, then take top N."""
    out = list(rows)
    if year_min is not None:
        out = [r for r in out if _year_or_zero(r) >= year_min]
    if year_max is not None:
        out = [r for r in out if 0 < _year_or_zero(r) <= year_max]
    out.sort(key=_year_or_zero, reverse=True)
    if top_n is not None and top_n > 0:
        out = out[:top_n]
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True,
                        help="Path to deduplicated search-results CSV.")
    parser.add_argument("--output", required=True,
                        help="Path to write the filtered CSV.")
    parser.add_argument("--year-min", type=int, default=None,
                        help="Drop rows with year < this value.")
    parser.add_argument("--year-max", type=int, default=None,
                        help="Drop rows with year > this value.")
    parser.add_argument("--top-n", type=int, default=None,
                        help="After year filter + sort by year desc, "
                             "keep only the first N rows.")
    args = parser.parse_args(argv)

    in_path = Path(args.input)
    if not in_path.is_file():
        print(f"ERROR: --input not found: {in_path}", file=sys.stderr)
        return 2

    with in_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    out_rows = filter_rows(
        rows,
        year_min=args.year_min,
        year_max=args.year_max,
        top_n=args.top_n,
    )

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(out_rows)

    print(f"Filtered {len(rows)} → {len(out_rows)} rows; wrote {out_path}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
