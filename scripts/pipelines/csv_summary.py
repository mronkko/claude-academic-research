"""Pure summary helpers for screening / pilot / coding CSVs.

Used by `audit_zotero_library.py` (existing macro audits + new
row-level subcommands) and `pilot_analyze.py`. Pure functions; no
side effects, no I/O beyond `read_csv`.

Why this exists: Claude was reaching for `python3 -c "import csv;
rows = list(csv.DictReader(open(...)))"` 22 times in a single SLR
session for the same three operations — count by dimension, find by
substring, diff two CSVs. That's a missing tool, not exploration.
"""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path


def read_csv(path: str | Path) -> list[dict[str, str]]:
    """Slurp a CSV into a list of dicts. UTF-8, no schema validation."""
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def summarize_by(rows: list[dict[str, str]], dimension: str) -> Counter:
    """Count rows grouped by `rows[i][dimension]`. Missing values bucket as ''."""
    return Counter(r.get(dimension, "") for r in rows)


def find_rows(
    rows: list[dict[str, str]],
    substring: str,
    *,
    in_field: str | None = None,
    case_sensitive: bool = False,
) -> list[dict[str, str]]:
    """Rows where `substring` appears in `in_field` (or any field if None).

    Case-insensitive by default. Returns a list (preserves input order).
    """
    needle = substring if case_sensitive else substring.lower()

    def _hit(value: str) -> bool:
        haystack = value if case_sensitive else value.lower()
        return needle in haystack

    if in_field is None:
        return [r for r in rows if any(_hit(v) for v in r.values())]
    return [r for r in rows if _hit(r.get(in_field, ""))]


def diff_csvs(
    path_a: str | Path,
    path_b: str | Path,
    *,
    key: str = "item_key",
) -> dict[str, list]:
    """Three-way diff of two CSVs by `key`.

    Returns a dict with three lists:
      - `only_in_a`: keys present in A but not B (with the A row).
      - `only_in_b`: keys present in B but not A (with the B row).
      - `changed`:   keys in both, but at least one column differs
                     (`{key, a, b}` per entry). Only fields present in
                     *both* rows are compared; new columns added in B
                     don't register as changes unless the value differs.
    """
    rows_a = read_csv(path_a)
    rows_b = read_csv(path_b)

    by_key_a = {r[key]: r for r in rows_a if r.get(key)}
    by_key_b = {r[key]: r for r in rows_b if r.get(key)}

    only_in_a = [{key: k, "row": by_key_a[k]} for k in by_key_a if k not in by_key_b]
    only_in_b = [{key: k, "row": by_key_b[k]} for k in by_key_b if k not in by_key_a]

    changed = []
    for k in by_key_a:
        if k not in by_key_b:
            continue
        a, b = by_key_a[k], by_key_b[k]
        common = set(a.keys()) & set(b.keys())
        if any(a[c] != b[c] for c in common):
            changed.append({key: k, "a": a, "b": b})

    return {"only_in_a": only_in_a, "only_in_b": only_in_b, "changed": changed}
