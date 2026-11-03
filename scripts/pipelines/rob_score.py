#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Deterministic risk-of-bias scoring: QUIN (in vitro) and QUADAS-2.

The judgements in a risk-of-bias assessment are made by reviewers. The
ARITHMETIC that follows - QUIN percentages and categories, QUADAS-2
overall judgements and per-domain counts - is done here, never by hand or
by a language model, so every number in the manuscript traces to a CSV a
reviewer signed off.

Input and output are CSV files under `analysis/results/rob/`.

QUIN (Sheth et al., J Prosthet Dent 2024;131:1038-42)
    Input columns: `study_key`, `c01` ... `c12`, optional `notes`.
    Each criterion is `2` (adequately specified), `1` (inadequately
    specified), `0` (not specified) or `NA` (not applicable, excluded from
    the calculation).
    score% = 100 * sum(scores) / (2 * number of applicable criteria)
    >70% low risk - 50-70% medium risk (both bounds inclusive) - <50% high.

QUADAS-2 (Whiting et al., Ann Intern Med 2011;155:529-36)
    Input columns: `study_key`, `d1_rob`, `d2_rob`, `d3_rob`, `d4_rob`
    (patient selection, index test, reference standard, flow and timing),
    and `d1_app`, `d2_app`, `d3_app` (applicability concerns). Each cell is
    `low`, `high` or `unclear`.
    Overall risk of bias: `low` only if all four domains are low; `high` if
    any domain is high; otherwise `unclear`. Overall applicability follows
    the same rule over its three domains.

Usage:
    uv run rob_score.py --instrument quin    --input rob/quin_ratings.csv    --out rob/quin_scored.csv
    uv run rob_score.py --instrument quadas2 --input rob/quadas2_ratings.csv --out rob/quadas2_scored.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

QUIN_CRITERIA = [f"c{i:02d}" for i in range(1, 13)]
QUIN_VALUES = {"2": 2, "1": 1, "0": 0}

QUADAS_ROB = ["d1_rob", "d2_rob", "d3_rob", "d4_rob"]
QUADAS_APP = ["d1_app", "d2_app", "d3_app"]
QUADAS_VALUES = {"low", "high", "unclear"}


class RatingError(ValueError):
    """A ratings file that cannot be scored; the message names the cell."""


def quin_category(percent: float) -> str:
    """Category for a QUIN percentage: >70 low, 50-70 medium, <50 high."""
    if percent > 70:
        return "low"
    if percent >= 50:
        return "medium"
    return "high"


def score_quin_row(row: dict, line: int) -> dict:
    key = (row.get("study_key") or "").strip()
    if not key:
        raise RatingError(f"line {line}: study_key is empty")
    total = 0
    applicable = 0
    for col in QUIN_CRITERIA:
        raw = (row.get(col) or "").strip().upper()
        if raw == "NA":
            continue
        if raw not in QUIN_VALUES:
            raise RatingError(
                f"line {line} ({key}): {col} is {raw!r}; use 2, 1, 0 or NA"
            )
        total += QUIN_VALUES[raw]
        applicable += 1
    if applicable == 0:
        raise RatingError(f"line {line} ({key}): every criterion is NA")
    percent = 100.0 * total / (2 * applicable)
    return {
        "study_key": key,
        "total_score": total,
        "applicable_criteria": applicable,
        "max_score": 2 * applicable,
        "percent": round(percent, 1),
        "risk_of_bias": quin_category(percent),
    }


def overall(judgements: list[str]) -> str:
    """QUADAS-2 roll-up: all low -> low; any high -> high; else unclear."""
    if all(j == "low" for j in judgements):
        return "low"
    if any(j == "high" for j in judgements):
        return "high"
    return "unclear"


def score_quadas_row(row: dict, line: int) -> dict:
    key = (row.get("study_key") or "").strip()
    if not key:
        raise RatingError(f"line {line}: study_key is empty")
    out: dict = {"study_key": key}
    for col in QUADAS_ROB + QUADAS_APP:
        raw = (row.get(col) or "").strip().lower()
        if raw not in QUADAS_VALUES:
            raise RatingError(
                f"line {line} ({key}): {col} is {raw!r}; use low, high or unclear"
            )
        out[col] = raw
    out["overall_risk_of_bias"] = overall([out[c] for c in QUADAS_ROB])
    out["overall_applicability"] = overall([out[c] for c in QUADAS_APP])
    return out


def _read(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _require_columns(rows: list[dict], needed: list[str], what: str) -> None:
    if not rows:
        raise RatingError(f"{what}: the ratings file has no rows")
    missing = [c for c in ["study_key", *needed] if c not in rows[0]]
    if missing:
        raise RatingError(f"{what}: missing column(s): {', '.join(missing)}")


def run_quin(src: Path, dest: Path) -> list[dict]:
    rows = _read(src)
    _require_columns(rows, QUIN_CRITERIA, "QUIN")
    scored = [score_quin_row(r, i) for i, r in enumerate(rows, start=2)]
    _write(dest, scored)
    counts = Counter(r["risk_of_bias"] for r in scored)
    print(f"QUIN: {len(scored)} studies scored -> {dest}")
    print("  " + ", ".join(
        f"{c}: {counts.get(c, 0)}" for c in ("low", "medium", "high")))
    return scored


def run_quadas(src: Path, dest: Path) -> list[dict]:
    rows = _read(src)
    _require_columns(rows, QUADAS_ROB + QUADAS_APP, "QUADAS-2")
    scored = [score_quadas_row(r, i) for i, r in enumerate(rows, start=2)]
    _write(dest, scored)
    print(f"QUADAS-2: {len(scored)} studies scored -> {dest}")
    for col in QUADAS_ROB + QUADAS_APP:
        counts = Counter(r[col] for r in scored)
        print(f"  {col}: " + ", ".join(
            f"{v}: {counts.get(v, 0)}" for v in ("low", "high", "unclear")))
    overall_counts = Counter(r["overall_risk_of_bias"] for r in scored)
    print("  overall risk of bias: " + ", ".join(
        f"{v}: {overall_counts.get(v, 0)}" for v in ("low", "high", "unclear")))
    return scored


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--instrument", required=True,
                        choices=("quin", "quadas2"))
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.instrument == "quin":
            run_quin(args.input, args.out)
        else:
            run_quadas(args.input, args.out)
    except (RatingError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
