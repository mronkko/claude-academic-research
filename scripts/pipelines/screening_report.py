#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Summarise screening decisions across all passes of an append-only log.

Reads an abstract-screening or full-text-coding CSV produced by
`abstract_screen.py` / `fulltext_code.py`. Both logs are append-
only: a re-screening pass writes new rows for the same `item_key`,
and the LATEST row wins (matching the resume semantics in
`abstract_screen.py`). This script applies that last-row-wins
collapse and prints a counts summary.

Drop-in for the previous shape ("write a heredoc to count the
last-row-wins decisions and list re-screened includes") — invoke
this instead so the summary is auditable, repeatable, and works
the same way in every session.

Usage:
    uv run screening_report.py screening/abstract_screening.csv
    uv run screening_report.py screening/abstract_screening.csv \\
        --list include
    uv run screening_report.py screening/abstract_screening.csv \\
        --list-rescreened
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path


def collapse_last_row_wins(rows: list[dict]) -> dict[str, dict]:
    """Return {item_key: most-recent-row}.

    Append-only logs preserve history; the most recent row per
    `item_key` is the active decision. Order in the file IS the
    order of decisions (timestamps are also written, but reading
    sequentially and overwriting is sufficient and avoids parsing
    timestamps).
    """
    latest: dict[str, dict] = {}
    for row in rows:
        key = (row.get("item_key") or "").strip()
        if key:
            latest[key] = row
    return latest


def find_rescreened(rows: list[dict]) -> set[str]:
    """Return item_keys that have more than one row in the log."""
    counts: Counter[str] = Counter(
        (r.get("item_key") or "").strip()
        for r in rows
        if (r.get("item_key") or "").strip()
    )
    return {k for k, n in counts.items() if n > 1}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log_csv",
                        help="Append-only screening / coding log path "
                             "(e.g., screening/abstract_screening.csv).")
    parser.add_argument(
        "--list", dest="list_decision", default="",
        help="After the summary, list every item with this latest "
             "decision (include / borderline / exclude / error).",
    )
    parser.add_argument(
        "--list-rescreened", action="store_true",
        help="After the summary, list every item_key that appears "
             "more than once in the log (re-screened items).",
    )
    args = parser.parse_args(argv)

    log_path = Path(args.log_csv)
    if not log_path.is_file():
        print(f"ERROR: log not found: {log_path}", file=sys.stderr)
        return 2

    with log_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    latest = collapse_last_row_wins(rows)
    decisions = Counter(
        (r.get("decision") or "").strip()
        for r in latest.values()
    )

    print(f"Log: {log_path}")
    print(f"  Rows in file:       {len(rows)}")
    print(f"  Unique items:       {len(latest)}")
    print()
    print("  Latest decisions (last-row-wins):")
    for d in ("include", "borderline", "exclude", "error"):
        print(f"    {d:<11} {decisions.get(d, 0)}")
    other = {k: v for k, v in decisions.items()
             if k not in ("include", "borderline", "exclude", "error")}
    if other:
        print("  Other / unrecognised:")
        for k, v in sorted(other.items()):
            print(f"    {k!r:<11} {v}")

    if args.list_decision:
        decision = args.list_decision.strip().lower()
        items = [(k, r) for k, r in latest.items()
                 if (r.get("decision") or "").strip().lower() == decision]
        print()
        print(f"  Items with latest decision = {decision!r} ({len(items)}):")
        for k, r in sorted(items):
            title = (r.get("title") or "").strip()[:96]
            print(f"    {k}  {title}")

    if args.list_rescreened:
        rescreened = find_rescreened(rows)
        per_key: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            per_key[(r.get("item_key") or "").strip()].append(r)
        print()
        print(f"  Re-screened items "
              f"(more than one row, latest decision shown) ({len(rescreened)}):")
        for k in sorted(rescreened):
            r = latest.get(k, {})
            title = (r.get("title") or "").strip()[:80]
            decision = (r.get("decision") or "").strip()
            print(f"    {k}  [{len(per_key[k])} rows, last={decision}]  {title}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
