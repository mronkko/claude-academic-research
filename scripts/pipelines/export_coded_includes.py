#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Filter a full-text screening / coding log to the includes-only view.

Reads a CSV produced by `fulltext_code.py` (or any compatible screening
log) and writes a second CSV containing only rows whose final decision
is `include`. Uses last-row-wins semantics on `item_key`, so
adjudication flips recorded as appended rows are respected.

No Claude API calls, no Zotero API calls — pure CSV filtering.

Usage:
    uv run export_coded_includes.py \\
        --log-csv screening/fulltext_screening.csv \\
        --out analysis/results/coded_papers.csv

    uv run export_coded_includes.py --log-csv LOG --out OUT --dry-run

By default, every column in the input CSV is preserved in the output
(minus any you pass with `--drop-columns`), and rows are sorted by
year then authors. Use `--columns` to restrict the output to a
specific, stable column set — recommended for manuscript-facing
outputs so the schema doesn't drift as the coding script evolves.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-csv", required=True,
                        help="Input screening CSV.")
    parser.add_argument("--out", required=True,
                        help="Output CSV path.")
    parser.add_argument("--columns", default="",
                        help="Comma-separated list of output columns "
                             "(default: every column from the input).")
    parser.add_argument("--drop-columns", default="",
                        help="Comma-separated list of columns to omit "
                             "from the output.")
    parser.add_argument("--decision", default="include",
                        help="Which decision to keep (default: include).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Count rows; do not write output.")
    args = parser.parse_args()

    if not os.path.exists(args.log_csv):
        sys.exit(f"ERROR: {args.log_csv} not found.")

    # Last-row-wins on item_key — honours adjudication flips appended to the log.
    by_key: dict[str, dict] = {}
    with open(args.log_csv, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        input_fields = list(reader.fieldnames or [])
        for row in reader:
            if row.get("item_key"):
                by_key[row["item_key"]] = row

    counts = {
        "include":   sum(1 for r in by_key.values() if r.get("decision") == "include"),
        "exclude":   sum(1 for r in by_key.values() if r.get("decision") == "exclude"),
        "error":     sum(1 for r in by_key.values() if r.get("decision") == "error"),
        "borderline": sum(1 for r in by_key.values() if r.get("decision") == "borderline"),
        "other":     sum(1 for r in by_key.values()
                         if r.get("decision") not in
                         ("include", "exclude", "error", "borderline")),
    }
    kept = [r for r in by_key.values() if r.get("decision") == args.decision]

    print(f"Input rows (unique item_keys): {len(by_key)}", flush=True)
    print(f"Includes:                       {counts['include']}", flush=True)
    print(f"Excludes:                       {counts['exclude']}", flush=True)
    print(f"Errors:                         {counts['error']}", flush=True)
    print(f"Borderline:                     {counts['borderline']}", flush=True)
    if counts["other"]:
        print(f"Other / unrecognised decisions: {counts['other']}", flush=True)
    print(f"Kept (decision = {args.decision!r}): {len(kept)}", flush=True)

    if args.dry_run:
        print(f"\n[DRY RUN] Would write {len(kept)} rows to {args.out}",
              flush=True)
        return 0

    if args.columns:
        out_fields = [c.strip() for c in args.columns.split(",") if c.strip()]
    else:
        drop = {c.strip() for c in args.drop_columns.split(",") if c.strip()}
        out_fields = [c for c in input_fields if c not in drop]

    out_dir = os.path.dirname(args.out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=out_fields, extrasaction="ignore")
        writer.writeheader()
        sorted_rows = sorted(
            kept,
            key=lambda r: (r.get("year", ""), r.get("authors", "")),
        )
        for row in sorted_rows:
            writer.writerow(row)

    print(f"\nWrote {len(kept)} rows to {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
