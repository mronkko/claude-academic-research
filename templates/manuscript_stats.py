#!/usr/bin/env python3
"""Manuscript-facing stats dictionary for an SLR — project template.

This is the per-project **producer** of every number the manuscript cites.
It reads all pipeline outputs (search_metadata.json, search_run.json,
screening CSVs, coded_papers.csv) and returns a FLAT dict of derived
numbers. The Quarto manuscript's inline expressions look these up
directly, so there is NO hand-typed methodology number in the prose.

This module is PROJECT-OWNED (lives in `analysis/manuscript_stats.py`).
It is NOT a plugin-shipped pipeline script. The plugin ships this file
as a starting template; you extend `build_stats()` as the manuscript
needs new facts. Every value returned must trace back to (a) a pipeline
artefact file, (b) file-system metadata, or (c) a pipeline subprocess
call — never a literal typed inline.

Flat keys with dotted namespaces (e.g. `screen.n_included`). Flat
lookup fails loudly on typos in the manuscript — `s['screen.xxx']`
raises KeyError. Nested dicts would silently return None on typos,
which is a footgun.

Usage in a Quarto setup chunk:
    from manuscript_stats import build_stats
    s = build_stats()
    # then `{python} s['screen.n_included']` anywhere in prose

CLI:
    python3 analysis/manuscript_stats.py    # writes analysis/results/manuscript_stats.json

The output file (`manuscript_stats.json`) is for human inspection and
the regression-test's content-integrity check. The manuscript itself
calls `build_stats()` live at render time rather than reading the JSON,
so hand-edits to the JSON have no effect on rendered output — but they
are still forbidden by the `empirical-integrity` skill. Regenerate via
the CLI only.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Pipeline-output paths — edit if your layout differs
# ---------------------------------------------------------------------------

SEARCH_METADATA_PATH = PROJECT_ROOT / "search_metadata.json"
SEARCH_RUN_PATH      = PROJECT_ROOT / "search_run.json"
SEARCH_DEDUP_PATH    = PROJECT_ROOT / "analysis" / "raw" / "search_results.csv"
ABSTRACT_LOG_PATH    = PROJECT_ROOT / "screening" / "abstract_screening.csv"
FULLTEXT_LOG_PATH    = PROJECT_ROOT / "screening" / "fulltext_screening.csv"
CODED_PAPERS_PATH    = PROJECT_ROOT / "analysis" / "results" / "coded_papers.csv"
STATS_OUT_PATH       = PROJECT_ROOT / "analysis" / "results" / "manuscript_stats.json"


# ---------------------------------------------------------------------------
# Optional: rule-based grouping of a free-text coding field into families.
#
# Worked example — for an SLR on entrepreneur motivation, each row in
# `coded_papers.csv` has a free-text `motivational_constructs` column like
# "growth aspirations; self-efficacy". Uncomment the list below to group
# those strings into families. `_coding_stats()` then emits one
# `coding.family.<slug>` key per family, and `tbl_construct_families(s)`
# in `manuscript_tables.py` renders them as a Findings table. Order matters — the
# first regex that matches wins, so put a broad "Other" last.
#
# To adapt to your own SLR: replace the field name in `_coding_stats()`
# below, replace the (label, regex) tuples here, and the pipeline does
# the rest. Leaving the list empty disables the feature entirely (the
# family table in `manuscript.qmd` falls back to a placeholder comment).
# ---------------------------------------------------------------------------

CONSTRUCT_FAMILIES: list[tuple[str, str]] = [
    # ("Display label", "regex (case-insensitive) matched against the field"),
    # ("Growth intentions / aspirations",
    #  r"growth\s+(intent|aspir|ambit|motivat)"),
    # ("Self-efficacy", r"self[- ]?efficacy"),
    # ("Other", r".*"),  # catch-all — keep last
]


def _classify(text: str, rules: list[tuple[str, str]]) -> str:
    for family, pattern in rules:
        if re.search(pattern, text, re.IGNORECASE):
            return family
    return "Unclassified"


# ---------------------------------------------------------------------------
# Builders — one per pipeline stage
# ---------------------------------------------------------------------------


def _last_row_per_key(rows: list[dict], key_col: str = "item_key") -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in rows:
        k = r.get(key_col)
        if k:
            out[k] = r
    return out


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _search_stats() -> dict:
    meta = (json.loads(SEARCH_METADATA_PATH.read_text(encoding="utf-8"))
            if SEARCH_METADATA_PATH.exists() else {})
    run = (json.loads(SEARCH_RUN_PATH.read_text(encoding="utf-8"))
           if SEARCH_RUN_PATH.exists() else {})
    return {
        "search.from_year": meta.get("from_year"),
        "search.to_year": meta.get("to_year"),
        "search.databases": ", ".join(meta.get("databases", [])),
        "search.journal_count": meta.get("journal_count", 0),
        "search.total_raw_rows": meta.get("total_raw_rows", 0),
        "search.unique_records": run.get("unique_records", 0),
        "search.unique_dois": run.get("unique_dois", 0),
        "search.doi_sha256": run.get("doi_sha256", ""),
    }


def _screening_stats() -> dict:
    abs_last = _last_row_per_key(_read_csv(ABSTRACT_LOG_PATH))
    ft_last = _last_row_per_key(_read_csv(FULLTEXT_LOG_PATH))
    abs_counts = Counter(r.get("decision", "") for r in abs_last.values())
    ft_counts = Counter(r.get("decision", "") for r in ft_last.values())
    return {
        "screen.abstract.n_total": len(abs_last),
        "screen.abstract.n_include": abs_counts.get("include", 0),
        "screen.abstract.n_borderline": abs_counts.get("borderline", 0),
        "screen.abstract.n_exclude": abs_counts.get("exclude", 0),
        "screen.fulltext.n_total": len(ft_last),
        "screen.fulltext.n_include": ft_counts.get("include", 0),
        "screen.fulltext.n_exclude": ft_counts.get("exclude", 0),
        "screen.n_included": ft_counts.get("include", 0),
    }


def _coding_stats() -> dict:
    rows = _read_csv(CODED_PAPERS_PATH)
    out: dict = {"coding.n_coded": len(rows)}
    if not rows:
        return out
    # Add counts by whatever field + families you defined above.
    if CONSTRUCT_FAMILIES and "motivational_constructs" in rows[0]:
        families = Counter(
            _classify(r.get("motivational_constructs", "") or "",
                      CONSTRUCT_FAMILIES)
            for r in rows
        )
        for family, n in families.items():
            slug = re.sub(r"\W+", "_", family.lower()).strip("_")
            out[f"coding.family.{slug}"] = n
    # Example: count by method (if coded)
    if rows and "method" in rows[0]:
        methods = Counter(
            (r.get("method", "") or "").split(";")[0].strip().lower()[:30]
            for r in rows
        )
        out["coding.n_unique_methods"] = len(methods)
    return out


def _provenance_stats() -> dict:
    """Model + prompt-version constants from the latest screening row. These
    land in prose via `s['provenance.screen.model']` so the reader can see
    exactly which model rendered which decisions."""
    ft = _read_csv(FULLTEXT_LOG_PATH)
    out: dict = {}
    if ft:
        last = ft[-1]
        out["provenance.fulltext.model"] = last.get("model", "")
        out["provenance.fulltext.prompt_version"] = last.get("prompt_version", "")
    abs_rows = _read_csv(ABSTRACT_LOG_PATH)
    if abs_rows:
        last = abs_rows[-1]
        out["provenance.abstract.model"] = last.get("model", "")
        out["provenance.abstract.prompt_version"] = last.get("prompt_version", "")
    return out


def build_stats() -> dict:
    """Return the flat stats dict. Used by the manuscript setup chunk."""
    stats: dict = {}
    stats.update(_search_stats())
    stats.update(_screening_stats())
    stats.update(_coding_stats())
    stats.update(_provenance_stats())
    return stats


def main() -> int:
    stats = build_stats()
    STATS_OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATS_OUT_PATH.write_text(
        json.dumps(stats, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print(f"Wrote {len(stats)} stats to {STATS_OUT_PATH}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
