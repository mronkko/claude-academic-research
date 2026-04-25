#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyzotero>=1.6",
#     "tenacity>=8.0",
#     "httpx>=0.25",
# ]
# ///
"""Apply human / QA-evaluator adjudication decisions to Zotero tags.

After post-screening QA evaluators flag items (`qa-flag`, `qa-hard`,
`qa-soft-*`, `qa-wrong-code`) and the human reviews them, this script
applies the adjudications to Zotero in bulk:

  - removes the QA severity tags (`qa-flag`, `qa-hard`, `qa-soft-*`,
    `qa-wrong-code`)
  - adds the permanent record tag (`qa-adjudicated-include` or
    `qa-adjudicated-exclude`)
  - optionally flips the screener's `fulltext:*` tag when the
    adjudicator's verdict overrides the screening decision.

Replaces the user's downstream `apply_qa_adjudications.py` (4 edits in
the SLR session log) — and specifically replaces the pyzotero footgun
that surfaced there: calling `add_tags()` with a stub item dict
silently drops the write. This script routes through
`zotero_io.batch_update_tags`, which (a) reads each item's current
state in one bulk call per batch and (b) constructs a full payload
with the right version field for every PATCH.

Decision input — `decisions.json`:

    [
      {
        "item_key": "ABCD0001",
        "verdict": "include",
        "reason": "Empirical paper using AI in entrepreneurship.",
        "flip_fulltext": true
      },
      {"item_key": "WXYZ9999", "verdict": "exclude",
       "reason": "Commentary, not empirical."}
    ]

Schema:
  - `item_key` (required): Zotero 8-char item key.
  - `verdict` (required): "include" | "exclude" | "borderline".
  - `reason` (optional): free text — written to the apply log only.
  - `flip_fulltext` (optional, default false): when true, also remove
    any existing `fulltext:*` stage tag and add the matching
    `fulltext:include` / `fulltext:exclude`. Only flip when the
    adjudication contradicts the screener.

Usage:
    uv run apply_qa_adjudications.py --group 6015547 \\
        --decisions .claude/qa/decisions.json
    uv run apply_qa_adjudications.py --user --decisions decisions.json --dry-run
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = SCRIPT_DIR.parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import zotero_io  # noqa: E402

VALID_VERDICTS = ("include", "exclude", "borderline")

# Tag prefix used for the permanent adjudication record tags. Mirrors
# the `qa-adjudicated-*` convention documented in
# skills/systematic-review/SKILL.md (QA and adjudication tags table).
ADJUDICATION_PREFIX = "qa-adjudicated-"

# Severity tags removed once a verdict is recorded. Items the screener
# left fine (no qa-* tags at all) skip this removal step.
QA_SEVERITY_PREFIX = "qa-"

# Stage tag prefix for screener decisions. Flipping fulltext membership
# under adjudication uses this prefix.
FULLTEXT_PREFIX = "fulltext:"

LOG_FIELDS = [
    "timestamp", "item_key", "verdict", "reason", "flip_fulltext",
    "applied", "unchanged", "failed",
]


def _build_op(decision: dict) -> dict:
    """Build a tag-operation dict for `zotero_io.batch_update_tags`.

    The op encodes:
      - add: the new permanent qa-adjudicated-<verdict> tag, plus the
        opposite-stage fulltext tag when flip_fulltext is set.
      - remove_prefixed: every `qa-*` severity tag (qa-flag, qa-hard,
        qa-soft-*, qa-wrong-code) AND, when flipping, every
        `fulltext:*` tag (so the new one cleanly replaces the old).

    Note: qa-adjudicated-* tags also start with `qa-` and would be
    swept by the prefix removal — but the same write also re-adds
    the new qa-adjudicated-<verdict>, so the net effect is "replace
    any earlier adjudication tag with the current one". That's the
    intended idempotent behaviour for re-runs.
    """
    verdict = decision["verdict"].lower()
    add: list[str] = [f"{ADJUDICATION_PREFIX}{verdict}"]
    remove_prefixed: list[str] = [QA_SEVERITY_PREFIX]

    if decision.get("flip_fulltext"):
        # The flip only makes sense for include / exclude — borderline
        # adjudication doesn't choose a fulltext bucket.
        if verdict in ("include", "exclude"):
            add.append(f"{FULLTEXT_PREFIX}{verdict}")
            remove_prefixed.append(FULLTEXT_PREFIX)

    return {"add": add, "remove_prefixed": remove_prefixed}


def load_decisions(path: Path) -> list[dict]:
    """Read and validate the decisions.json file. Exits with a clear
    error rather than letting a malformed payload land in Zotero."""
    if not path.is_file():
        sys.exit(
            f"ERROR: decisions file not found: {path}\n"
            f"The QA evaluator agents (post-screening) must emit a "
            f"`decisions.json` alongside their markdown report. See "
            f"skills/systematic-review/SKILL.md (QA evaluator output) "
            f"for the schema."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"ERROR: {path} is not valid JSON: {e}")
    if not isinstance(payload, list):
        sys.exit(
            f"ERROR: {path} must contain a JSON array of decision objects."
        )
    valid: list[dict] = []
    errors: list[str] = []
    for i, entry in enumerate(payload):
        if not isinstance(entry, dict):
            errors.append(f"  [{i}]: not an object")
            continue
        item_key = (entry.get("item_key") or "").strip()
        verdict = (entry.get("verdict") or "").strip().lower()
        if not item_key:
            errors.append(f"  [{i}]: missing item_key")
            continue
        if verdict not in VALID_VERDICTS:
            errors.append(
                f"  [{i}] item_key={item_key}: verdict={verdict!r} not in {VALID_VERDICTS}"
            )
            continue
        valid.append({
            "item_key": item_key,
            "verdict": verdict,
            "reason": entry.get("reason") or "",
            "flip_fulltext": bool(entry.get("flip_fulltext", False)),
        })
    if errors:
        sys.exit(
            f"ERROR: {len(errors)} invalid decision(s) in {path}:\n"
            + "\n".join(errors)
        )
    return valid


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    zotero_io.add_library_args(parser)
    parser.add_argument(
        "--decisions", default=".claude/qa/decisions.json",
        help="Path to decisions.json (default: .claude/qa/decisions.json).",
    )
    parser.add_argument(
        "--log", default="output/qa_adjudications_log.csv",
        help="Path for the apply log (default: output/qa_adjudications_log.csv).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be applied; do not call Zotero.",
    )
    args = parser.parse_args()

    decisions = load_decisions(Path(args.decisions))
    print(f"Loaded {len(decisions)} decision(s) from {args.decisions}", flush=True)

    if not decisions:
        print("Nothing to apply.", flush=True)
        return 0

    if args.dry_run:
        print("\n[DRY RUN] tag operations that would be applied:", flush=True)
        for d in decisions:
            op = _build_op(d)
            flip = " (flip fulltext)" if d.get("flip_fulltext") else ""
            print(
                f"  {d['item_key']}: verdict={d['verdict']}{flip}\n"
                f"    add: {op['add']}\n"
                f"    remove_prefixed: {op['remove_prefixed']}",
                flush=True,
            )
        return 0

    zot = zotero_io.ZoteroClient.from_args(args)
    print(f"Applying to {zot.describe_library()}...", flush=True)

    updates = [(d["item_key"], _build_op(d)) for d in decisions]
    stats = zot.batch_update_tags(updates)
    print(
        f"\nDone. applied={stats['applied']} "
        f"unchanged={stats['unchanged']} failed={stats['failed']}",
        flush=True,
    )

    # Write the apply log.
    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not log_path.exists()
    timestamp = datetime.now(UTC).isoformat()
    with log_path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=LOG_FIELDS)
        if is_new:
            writer.writeheader()
        for d in decisions:
            writer.writerow({
                "timestamp": timestamp,
                "item_key": d["item_key"],
                "verdict": d["verdict"],
                "reason": d["reason"],
                "flip_fulltext": "true" if d["flip_fulltext"] else "false",
                # Per-batch stats apply globally to this run; the log
                # is for audit trail / which-decisions-were-attempted,
                # not per-item success / fail (that's surfaced in
                # batch_update_tags's return on stderr).
                "applied": stats["applied"],
                "unchanged": stats["unchanged"],
                "failed": stats["failed"],
            })
    print(f"Apply log: {log_path}", flush=True)
    return 0 if stats["failed"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
