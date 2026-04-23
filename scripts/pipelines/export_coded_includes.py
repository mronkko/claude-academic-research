#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyzotero>=1.6",
# ]
# ///
"""Export the manuscript-facing view of coded includes, reading from
Zotero as the authoritative source (per the `systematic-review` skill's
Zotero-as-ground-truth principle).

Queries the project's Zotero collection for items tagged with
`fulltext:include`, fetches each item's `SLR Coding` child note, parses
the machine-readable JSON payload embedded in the note, and joins that
with the item's bibliographic metadata (title, authors, year, journal,
DOI, Better BibTeX key). Emits a single CSV suitable for downstream
analysis (`analysis/manuscript_stats.py` ingests it; `tbl_*` functions
in `manuscript_tables.py` render it).

No CSV log is read — adjudication flips propagate automatically
because they land on the Zotero tag; exclusion codes propagate because
they live in the `SLR Coding` note.

Usage:
    uv run export_coded_includes.py \\
        --group 6015547 --collection ABCDE1234 \\
        --out analysis/results/coded_papers.csv

Common flags: --columns (restrict output to a named column set),
--drop-columns (omit specific columns), --tag (override the filter tag;
default fulltext:include), --dry-run (count, don't write).

For pre-Zotero-as-truth projects that still have a CSV log but no
tags, run `fulltext_code.py --csv-backfill` first to apply the
tags, then use this script. There is no CSV-based fallback here.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = SCRIPT_DIR.parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import zotero_io  # noqa: E402
from core.config_loader import require  # noqa: E402

# Default column order — bibliographic fields first, then provenance,
# then every coding field the note carries (preserved in note-declared
# order on a best-effort basis).
DEFAULT_BIB_COLUMNS = [
    "item_key", "bibtex_key", "doi", "title", "authors", "year",
    "journal",
]
DEFAULT_PROVENANCE_COLUMNS = [
    "decision", "exclusion_code", "reason",
    "model", "prompt_version", "timestamp",
]


def _bibtex_key_from_extra(extra: str) -> str:
    """Extract the Better BibTeX citation key from a Zotero item's
    `extra` field. BBT writes `Citation Key: foobar2020baz` there."""
    for line in (extra or "").splitlines():
        line = line.strip()
        if line.lower().startswith("citation key:"):
            return line.split(":", 1)[1].strip()
    return ""


def _authors_string(creators: list[dict]) -> str:
    """Flatten a list of Zotero creator dicts into 'Last1, Last2, …'."""
    names = []
    for c in creators:
        if c.get("creatorType") != "author":
            continue
        last = (c.get("lastName") or "").strip()
        if last:
            names.append(last)
        else:
            name = (c.get("name") or "").strip()
            if name:
                names.append(name)
    return "; ".join(names)


def _year_from_date(date: str) -> str:
    """Zotero's `date` field is free-form; extract the first 4-digit
    run as the year."""
    import re
    m = re.search(r"\b(\d{4})\b", date or "")
    return m.group(1) if m else ""


def _row_from_item(
    item: dict,
    coding_payload: dict,
) -> dict:
    """Merge Zotero bibliographic fields with the parsed coding note
    payload into a single flat dict suitable for CSV emission."""
    data = item.get("data", {})
    row: dict = {
        "item_key": data.get("key", item.get("key", "")),
        "bibtex_key": _bibtex_key_from_extra(data.get("extra", "")),
        "doi": (data.get("DOI") or "").strip(),
        "title": (data.get("title") or "").strip(),
        "authors": _authors_string(data.get("creators") or []),
        "year": _year_from_date(data.get("date", "")),
        "journal": (data.get("publicationTitle") or "").strip(),
    }
    # Provenance from note.
    for col in DEFAULT_PROVENANCE_COLUMNS:
        row[col] = coding_payload.get(col, "")
    # Coding fields from note (each field becomes its own column).
    fields = coding_payload.get("fields") or {}
    for name, value in fields.items():
        row[name] = value
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", default=os.environ.get("ZOTERO_GROUP", ""),
                        help="Zotero group ID (default: $ZOTERO_GROUP).")
    parser.add_argument("--collection", required=True,
                        help="Zotero collection key.")
    parser.add_argument("--out", required=True,
                        help="Output CSV path.")
    parser.add_argument("--tag", default="fulltext:include",
                        help="Filter tag (default: fulltext:include).")
    parser.add_argument("--columns", default="",
                        help="Comma-separated output-column restriction "
                             "(default: all bibliographic + provenance + "
                             "coding fields discovered in the notes).")
    parser.add_argument("--drop-columns", default="",
                        help="Comma-separated list of columns to omit.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Count; do not write output.")
    args = parser.parse_args()

    if not args.group:
        sys.exit("ERROR: --group required (or set ZOTERO_GROUP).")
    api_key = require("zotero", "api_key", env="ZOTERO_API_KEY")

    print(f"Querying Zotero (group={args.group}, "
          f"collection={args.collection}, tag={args.tag})...", flush=True)
    zot = zotero_io.ZoteroClient(api_key=api_key, group_id=args.group)
    items = zot.items_with_tag(args.collection, args.tag)
    print(f"  {len(items)} item(s) carry tag {args.tag!r}", flush=True)

    # For each included item, fetch its children and find the SLR Coding note.
    rows: list[dict] = []
    missing_note: list[str] = []
    malformed_note: list[str] = []
    coding_field_order: list[str] = []
    seen_fields: set[str] = set()

    for item in items:
        item_key = item.get("key", "")
        children = zot.cloud.children(item_key)
        payload: dict | None = None
        for child in children:
            cdata = child.get("data", {})
            if cdata.get("itemType") != "note":
                continue
            body = cdata.get("note", "") or ""
            if "SLR_CODING_DATA" not in body:
                continue
            parsed = zotero_io.parse_slr_coding_note(body)
            if parsed is None:
                malformed_note.append(item_key)
                break
            payload = parsed
            break

        if payload is None:
            missing_note.append(item_key)
            continue

        row = _row_from_item(item, payload)
        rows.append(row)

        # Preserve declaration order of coding fields across papers.
        for name in (payload.get("fields") or {}):
            if name not in seen_fields:
                seen_fields.add(name)
                coding_field_order.append(name)

    # Reporting.
    print(f"  Rows built: {len(rows)}", flush=True)
    if missing_note:
        print(f"  WARNING: {len(missing_note)} item(s) tagged "
              f"{args.tag!r} have no SLR Coding note: "
              f"{missing_note[:5]}{'…' if len(missing_note) > 5 else ''}",
              flush=True)
    if malformed_note:
        print(f"  WARNING: {len(malformed_note)} item(s) have a note but "
              f"its SLR_CODING_DATA payload is malformed: "
              f"{malformed_note[:5]}", flush=True)

    if args.dry_run:
        print(f"\n[DRY RUN] Would write {len(rows)} rows to {args.out}",
              flush=True)
        return 0

    # Column assembly.
    if args.columns:
        out_fields = [c.strip() for c in args.columns.split(",") if c.strip()]
    else:
        out_fields = list(DEFAULT_BIB_COLUMNS)
        out_fields += list(DEFAULT_PROVENANCE_COLUMNS)
        out_fields += coding_field_order
        drop = {c.strip() for c in args.drop_columns.split(",") if c.strip()}
        out_fields = [c for c in out_fields if c not in drop]

    out_dir = os.path.dirname(args.out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=out_fields, extrasaction="ignore")
        writer.writeheader()
        sorted_rows = sorted(
            rows,
            key=lambda r: (r.get("year", ""), r.get("authors", "")),
        )
        for row in sorted_rows:
            writer.writerow(row)

    print(f"\nWrote {len(rows)} rows to {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
