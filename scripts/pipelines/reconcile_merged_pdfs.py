#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyzotero>=1.15.1",
#     "requests>=2.31",
#     "urllib3>=2.0",
#     "tenacity>=8.0",
#     "httpx>=0.25",
# ]
# ///
"""Find merged PDFs that Zotero Desktop still files under the old item.

The Connector route saves a paper as a new item and then merges its PDF
into the keeper. Until the merge moved local, the re-parent was done on
the Web API, and Zotero Desktop did not always take it: on 2026-09-23, 26
of 213 merged PDFs were under the keeper on the Web API but still under
the trashed Connector item in Desktop, hours later, with both surfaces
claiming to be in sync. Every local-API reader, including enrich_pdfs's
own "already has a PDF" check, then sees the keeper as PDF-less. And if
Desktop ever uploads its copy, the merge is undone.

For each keeper this lists the PDF children the Web API files under it.
It then asks Desktop's local API where each one is:

    uv run reconcile_merged_pdfs.py --user --keys-file keepers.txt
    uv run reconcile_merged_pdfs.py --user --keys-file keepers.txt --fix

`--fix` re-parents each stale PDF in Desktop itself, through the local
API. That needs `[zotero] local_api_key`. Desktop then syncs the change
up, where it matches what the Web API already holds, so there is nothing
left for a later sync to undo. Without `--fix` nothing is written. The
report goes to --report either way.
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = SCRIPT_DIR.parent
for _p in (str(SCRIPT_DIR), str(SCRIPTS_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import zotero_io  # noqa: E402

DEFAULT_REPORT = ".claude/audit/merged_pdf_reconcile.csv"


@dataclass
class Stale:
    keeper: str
    pdf_key: str
    local_parent: str        # "" when Desktop does not have the PDF at all


def find_stale(zot, keepers: list[str]) -> list[Stale]:
    """PDFs under a keeper on the Web API but not under it in Desktop."""
    out: list[Stale] = []
    for keeper in keepers:
        for child in zot.cloud.children(keeper) or []:
            data = child.get("data", {})
            if data.get("contentType") != "application/pdf":
                continue
            key = child.get("key", "")
            try:
                local = zot.local.item(key)
            except Exception:  # noqa: BLE001 — absent locally is a finding
                out.append(Stale(keeper, key, ""))
                continue
            parent = (local.get("data", {}) or {}).get("parentItem", "")
            if parent != keeper:
                out.append(Stale(keeper, key, parent))
    return out


def fix_one(zot, row: Stale) -> None:
    """Re-parent `row.pdf_key` under its keeper in Desktop. The caller
    has checked `local_writes_enabled`, so the write surface is Desktop."""
    zot.reparent(row.pdf_key, row.keeper)


def _write_report(rows: list[Stale], path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["keeper", "pdf_key", "local_parent"])
        for r in rows:
            w.writerow([r.keeper, r.pdf_key, r.local_parent])


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keys-file", required=True,
                        help="Keeper item keys, one per line.")
    parser.add_argument("--fix", action="store_true",
                        help="Re-parent stale PDFs in Desktop (local API).")
    parser.add_argument("--report", default=DEFAULT_REPORT,
                        help=f"CSV of findings (default: {DEFAULT_REPORT}).")
    zotero_io.add_library_args(parser)
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    if not getattr(args, "user", False) and not args.group:
        try:
            zot = zotero_io.ZoteroClient.from_config(group_id=None)
        except zotero_io.GroupSelectionRequired as e:
            print(zotero_io.format_group_selection_error(e.groups), file=sys.stderr)
            return 2
    else:
        zot = zotero_io.ZoteroClient.from_args(args)

    with open(args.keys_file, encoding="utf-8") as f:
        keepers = [line.strip() for line in f if line.strip()]
    print(f"Checking {len(keepers)} keeper(s) against Zotero Desktop…", flush=True)
    rows = find_stale(zot, keepers)
    _write_report(rows, args.report)
    for r in rows:
        where = f"under {r.local_parent}" if r.local_parent else "missing"
        print(f"  {r.keeper}: PDF {r.pdf_key} is {where} in Desktop", flush=True)
    print(f"{len(rows)} stale PDF(s). Report: {args.report}")
    if not rows or not args.fix:
        if rows:
            print("Nothing written. Re-run with --fix to re-parent them in Desktop.")
        return 0
    if not zot.local_writes_enabled:
        print("--fix needs [zotero] local_api_key (run /setup).", file=sys.stderr)
        return 2

    failed = 0
    for r in rows:
        if not r.local_parent:
            print(f"  {r.pdf_key}: not in Desktop yet; left for sync.", flush=True)
            continue
        try:
            fix_one(zot, r)
        except Exception as e:  # noqa: BLE001 — report and continue
            failed += 1
            print(f"  {r.pdf_key}: failed: {str(e)[:120]}", flush=True)
    remaining = find_stale(zot, keepers)
    print(f"After --fix: {len(remaining)} stale PDF(s) remain"
          + (f"; {failed} write(s) failed." if failed else "."))
    return 1 if remaining else 0


if __name__ == "__main__":
    sys.exit(main())
