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
"""Repair abstracts already stored in Zotero. Dry run unless --write.

`enrich_abstracts.py` now cleans every abstract before writing it (see
`abstract_clean`), but abstracts written before that still carry the
debris: publisher copyright notices (Scopus embeds them, often fused
onto the first sentence), HTML entities, and a fused "Abstract"
heading. Measured on one library of 22,229 abstracts: 2,715, 624 and
1,716 respectively.

    uv run clean_abstracts.py                  # list what would change
    uv run clean_abstracts.py --write          # change it

Every run writes a CSV of the planned changes, before and after, to
--report (default `.claude/audit/abstract_repairs.csv`) so the diff can
be read before anything is written. An abstract that would become empty
is never written — that would be a deletion — and is left for a human.

Library-wide, like the other `enrich_*` scripts: an abstract is a fact
about the item, not a review's judgement, so there is no --config.
"""

from __future__ import annotations

import argparse
import csv
import html
import sys
from dataclasses import dataclass, field
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = SCRIPT_DIR.parent
for _p in (str(SCRIPT_DIR), str(SCRIPTS_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import zotero_io  # noqa: E402
from abstract_clean import _HEADING, clean_abstract  # noqa: E402

DEFAULT_REPORT = ".claude/audit/abstract_repairs.csv"


@dataclass
class Repair:
    key: str
    doi: str
    before: str
    after: str
    reasons: list[str] = field(default_factory=list)


def _reasons(before: str, after: str) -> list[str]:
    """Which kinds of debris the cleaning removed, for the report."""
    out = []
    if html.unescape(before) != before:
        out.append("entities")
    if _HEADING.match(html.unescape(before)):
        out.append("heading")
    if "©" in before and "©" not in after or "Copyright" in before and "Copyright" not in after:
        out.append("copyright")
    return out or ["whitespace"]


def plan_repairs(items: list[dict]) -> list[Repair]:
    """Items whose stored abstract `clean_abstract` would change."""
    plan = []
    for it in items:
        data = it.get("data", {})
        before = data.get("abstractNote") or ""
        if not before.strip():
            continue
        after = clean_abstract(before)
        if not after or after == " ".join(before.split()):
            continue
        plan.append(Repair(
            key=it["key"], doi=(data.get("DOI") or "").strip(),
            before=before, after=after, reasons=_reasons(before, after),
        ))
    return plan


def _write_report(plan: list[Repair], path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["item_key", "doi", "reasons", "before", "after"])
        for r in plan:
            w.writerow([r.key, r.doi, ";".join(r.reasons), r.before, r.after])


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true",
                        help="Apply the changes. Without it, only report.")
    parser.add_argument("--report", default=DEFAULT_REPORT,
                        help=f"CSV of planned changes (default: {DEFAULT_REPORT}).")
    parser.add_argument("--filter-keys-file",
                        help="Only these Zotero item keys, one per line.")
    zotero_io.add_library_args(parser)
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    if not getattr(args, "user", False) and not args.group:
        try:
            zot = zotero_io.ZoteroClient.from_config(
                group_id=None, prefer_local=not args.remote,
            )
        except zotero_io.GroupSelectionRequired as e:
            print(zotero_io.format_group_selection_error(e.groups), file=sys.stderr)
            return 2
    else:
        zot = zotero_io.ZoteroClient.from_args(args)

    print("Fetching Zotero items...", end=" ", flush=True)
    if args.filter_keys_file:
        with open(args.filter_keys_file) as f:
            keys = [line.strip() for line in f if line.strip()]
        items = zot.items_by_keys(keys)
    else:
        items = zot.abstractable_items(None)
    print(f"{len(items)} items.", flush=True)

    plan = plan_repairs(items)
    _write_report(plan, args.report)
    counts: dict[str, int] = {}
    for r in plan:
        for reason in r.reasons:
            counts[reason] = counts.get(reason, 0) + 1
    print(f"{len(plan)} abstract(s) to repair "
          f"({', '.join(f'{k} {v}' for k, v in sorted(counts.items())) or 'none'}).")
    print(f"Before/after for each: {args.report}")
    if not args.write:
        print("Dry run: nothing written. Re-run with --write to apply.")
        return 0

    failed = 0
    for i, r in enumerate(plan, 1):
        try:
            zot.update_abstract(r.key, r.after)
        except Exception as e:  # noqa: BLE001 — report and continue
            failed += 1
            print(f"  [{i}/{len(plan)}] {r.key} failed: {str(e)[:100]}", flush=True)
    print(f"Wrote {len(plan) - failed} of {len(plan)}"
          + (f"; {failed} failed (see above)." if failed else "."))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
