#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyzotero>=1.6",
# ]
# ///
"""Systematic-review pipeline tests — the recurring-test backstop for the
`systematic-review` skill.

Copy this file into your project's `scripts/` directory alongside
`test_common.py`. Edit paths at the top if your pipeline layout differs.

Tests (every one is an invariant every SR pipeline should satisfy):

1. All expected stage artefacts exist and are non-empty.
2. `search_run.json`'s DOI count matches the deduplicated CSV.
3. `search_metadata.json` has the required fields.
4. No duplicate DOIs in the deduplicated search output.
5. Abstract and full-text logs use only allowed decision states.
6. PRISMA arithmetic — fulltext-screened items all come from the
   abstract-include+borderline set.
7. Coded-paper count equals the fulltext-includes count.
8. Temperature=0 pinned in every Claude API call (legacy-layout check).
9. Model / prompt-version constants in `screening_config.py` match what
   was logged by the screening scripts.
10. No items in `decision=error` state after adjudication.
11. No "ghost" items — every fulltext-log key exists in Zotero.

BBT-key uniqueness and `coded_papers.csv` → `references.bib` resolution
live in `test_citations.py` instead, since they're citation concerns.
Manuscript-prose tests (forbidden literals, inline-key resolution, label
uniqueness, figure files) live in `test_empirical_integrity.py`.

Run:
    uv run scripts/test_systematic_review.py
    uv run scripts/test_systematic_review.py -v

Exit code 0 if all pass, 1 if any fail.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter

from test_common import (
    PROJECT_ROOT,
    TestRunner,
    last_row_per_key,
    must_exist,
    read_csv,
)

# ---------------------------------------------------------------------------
# Paths — edit for your project
# ---------------------------------------------------------------------------

SEARCH_DEDUP     = os.path.join(PROJECT_ROOT, "analysis/raw/search_results.csv")
SEARCH_METADATA  = os.path.join(PROJECT_ROOT, "search_metadata.json")
SEARCH_RUN       = os.path.join(PROJECT_ROOT, "search_run.json")
ABSTRACT_LOG     = os.path.join(PROJECT_ROOT, "screening/abstract_screening.csv")
FULLTEXT_LOG     = os.path.join(PROJECT_ROOT, "screening/fulltext_screening.csv")
CODED_PAPERS     = os.path.join(PROJECT_ROOT, "analysis/results/coded_papers.csv")

# `screening_config.py` is the canonical source of `MODEL` and
# `PROMPT_VERSION` constants (the pipeline scripts read them via
# `getattr(...)`). Test 9 verifies the screening logs match the config.
SCREENING_CONFIG = os.path.join(PROJECT_ROOT, "screening_config.py")

# Legacy-layout scripts kept for projects that copied the screening
# scripts into `scripts/` alongside the plugin invocation. Test 8
# silently passes when neither copy exists.
ABSTRACT_SCRIPT = os.path.join(PROJECT_ROOT, "scripts/abstract_screen.py")
FULLTEXT_SCRIPT = os.path.join(PROJECT_ROOT, "scripts/fulltext_code.py")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_pipeline_artefacts_exist() -> None:
    for p in (SEARCH_DEDUP, SEARCH_METADATA, SEARCH_RUN,
              ABSTRACT_LOG, FULLTEXT_LOG, CODED_PAPERS):
        must_exist(p)


def test_search_run_marker_matches_dedup_csv() -> None:
    """`search_run.json`'s DOI count must equal the deduped CSV's DOI count."""
    with open(SEARCH_RUN, encoding="utf-8") as f:
        marker = json.load(f)
    rows = read_csv(SEARCH_DEDUP)
    csv_dois = sum(1 for r in rows if r.get("doi", "").strip())
    assert marker["unique_dois"] == csv_dois, (
        f"search_run.json says {marker['unique_dois']} unique DOIs, "
        f"CSV has {csv_dois}"
    )


def test_search_metadata_has_required_fields() -> None:
    with open(SEARCH_METADATA, encoding="utf-8") as f:
        meta = json.load(f)
    for key in ("search_date_start", "search_date_end", "databases",
                "from_year", "to_year", "queries", "total_unique_records"):
        assert key in meta, f"search_metadata.json missing '{key}'"
    assert meta["from_year"] < meta["to_year"]


def test_no_duplicate_dois_in_dedup_csv() -> None:
    rows = read_csv(SEARCH_DEDUP)
    dois = [r["doi"].strip().lower() for r in rows if r.get("doi", "").strip()]
    dups = [d for d, c in Counter(dois).items() if c > 1]
    assert not dups, f"duplicate DOIs in deduped CSV: {dups[:5]}"


def test_abstract_log_decision_states() -> None:
    """Abstract stage uses include / exclude / borderline (or error during runs)."""
    rows = read_csv(ABSTRACT_LOG)
    decisions = {r["decision"] for r in rows if r.get("decision")}
    allowed = {"include", "exclude", "borderline", "error"}
    assert decisions <= allowed, f"unexpected abstract decisions: {decisions - allowed}"


def test_fulltext_log_decision_states_final() -> None:
    """After adjudication the last row per key must be include or exclude."""
    rows = read_csv(FULLTEXT_LOG)
    last = last_row_per_key(rows)
    decisions = {r.get("decision", "") for r in last.values()}
    assert decisions <= {"include", "exclude"}, (
        f"non-final decisions in fulltext log: {decisions - {'include', 'exclude'}}"
    )


def test_prisma_arithmetic() -> None:
    """Every fulltext-screened key comes from the abstract-include+borderline set."""
    abs_last = last_row_per_key(read_csv(ABSTRACT_LOG))
    abs_screened = {k for k, r in abs_last.items()
                    if r.get("decision") in ("include", "borderline")}

    ft_last = last_row_per_key(read_csv(FULLTEXT_LOG))
    extras = set(ft_last) - abs_screened
    assert not extras, (
        f"fulltext log contains keys not in abstract-include+borderline set: "
        f"{sorted(extras)[:5]}"
    )


def test_coded_count_matches_fulltext_includes() -> None:
    ft_last = last_row_per_key(read_csv(FULLTEXT_LOG))
    n_includes = sum(1 for r in ft_last.values() if r.get("decision") == "include")
    coded_rows = read_csv(CODED_PAPERS)
    assert len(coded_rows) == n_includes, (
        f"coded_papers.csv has {len(coded_rows)} rows; fulltext includes={n_includes}"
    )


def test_temperature_zero_pinned() -> None:
    """Every Claude API call must have temperature=0 — reproducibility invariant.

    Checks any local copy of `abstract_screen.py` / `fulltext_code.py`
    (legacy layout). Projects that invoke the plugin scripts by path get
    this invariant enforced by the plugin's own test suite, so this test
    silently passes when no local copies exist.
    """
    for script in (ABSTRACT_SCRIPT, FULLTEXT_SCRIPT):
        if not os.path.exists(script):
            continue
        with open(script, encoding="utf-8") as f:
            src = f.read()
        ok = (re.search(r'temperature\s*=\s*0\b', src) is not None
              or re.search(r'TEMPERATURE\s*=\s*0\b', src) is not None
              or re.search(r'"temperature"\s*:\s*0\b', src) is not None)
        assert ok, f"{script}: no temperature=0 setting found"


def test_screening_config_constants_match_logs() -> None:
    """`screening_config.py` is the canonical source of MODEL and
    PROMPT_VERSION (pipeline scripts read them via getattr). Each
    screening-log row's logged `model` / `prompt_version` must match
    what the config declares, so drift between a changed config and an
    un-re-run pipeline surfaces here."""
    if not os.path.exists(SCREENING_CONFIG):
        return
    with open(SCREENING_CONFIG, encoding="utf-8") as f:
        src = f.read()
    m_ft = re.search(r'^FULLTEXT_CODING_MODEL\s*=\s*"([^"]+)"', src, re.M)
    p_ft = re.search(r'^FULLTEXT_CODING_PROMPT_VERSION\s*=\s*"([^"]+)"', src, re.M)
    m_ab = re.search(r'^ABSTRACT_SCREENING_MODEL\s*=\s*"([^"]+)"', src, re.M)
    p_ab = re.search(r'^ABSTRACT_SCREENING_PROMPT_VERSION\s*=\s*"([^"]+)"', src, re.M)

    if m_ft and p_ft and os.path.exists(FULLTEXT_LOG):
        ft_last = last_row_per_key(read_csv(FULLTEXT_LOG))
        models = {r.get("model", "") for r in ft_last.values() if r.get("model")}
        pvers = {r.get("prompt_version", "") for r in ft_last.values()
                 if r.get("prompt_version")}
        assert models <= {m_ft.group(1)}, (
            f"fulltext model drift vs screening_config.py: {models}"
        )
        assert pvers <= {p_ft.group(1)}, (
            f"fulltext prompt_version drift: {pvers}"
        )

    if m_ab and p_ab and os.path.exists(ABSTRACT_LOG):
        ab_last = last_row_per_key(read_csv(ABSTRACT_LOG))
        models = {r.get("model", "") for r in ab_last.values() if r.get("model")}
        pvers = {r.get("prompt_version", "") for r in ab_last.values()
                 if r.get("prompt_version")}
        assert models <= {m_ab.group(1)}, (
            f"abstract model drift vs screening_config.py: {models}"
        )
        assert pvers <= {p_ab.group(1)}, (
            f"abstract prompt_version drift: {pvers}"
        )


def test_no_remaining_errors_in_fulltext_log() -> None:
    """After `--rerun`, no live decision should be `error`."""
    rows = read_csv(FULLTEXT_LOG)
    last = last_row_per_key(rows)
    errors = [k for k, r in last.items() if r.get("decision") == "error"]
    assert not errors, f"{len(errors)} items still in error state: {errors[:5]}"


def _live_zotero_items() -> list[dict] | None:
    """Fetch all journalArticle items from Zotero for cross-check tests.
    Returns None when pyzotero isn't available, credentials aren't set,
    or Zotero can't be reached — callers skip gracefully in that case."""
    try:
        from pyzotero import zotero  # type: ignore[import-not-found]
    except ImportError:
        return None
    group = os.environ.get("ZOTERO_GROUP")
    api_key = os.environ.get("ZOTERO_API_KEY")
    if not group or not api_key:
        return None
    try:
        z = zotero.Zotero(group, "group", api_key, local=True)
        return list(z.everything(z.items(itemType="journalArticle")))
    except Exception:
        return None


def _tags_of(item: dict) -> set[str]:
    return {
        t.get("tag", "")
        for t in item.get("data", {}).get("tags", [])
        if t.get("tag")
    }


def test_ghosts_handled_consistently() -> None:
    """Items in the fulltext log must exist in Zotero. Skip if pyzotero
    unavailable or local Zotero not running."""
    items = _live_zotero_items()
    if items is None:
        return
    live_keys = {it["key"] for it in items}
    ft_last = last_row_per_key(read_csv(FULLTEXT_LOG))
    ghosts = [k for k in ft_last if k not in live_keys]
    assert not ghosts, f"fulltext log references non-Zotero keys: {ghosts[:5]}"


def test_fulltext_tags_consistent_with_csv_log() -> None:
    """Zotero is the ground truth for screening decisions (per the
    `systematic-review` skill). Every item in Zotero with
    `fulltext:include` or `fulltext:exclude` must have a matching
    last-row decision in the CSV log, AND every CSV include/exclude
    decision must have a matching tag. Drift in either direction
    signals a tag-write failure or an out-of-band CSV edit.

    Skipped when Zotero is unreachable."""
    items = _live_zotero_items()
    if items is None:
        return
    tag_decision: dict[str, str] = {}
    for it in items:
        tags = _tags_of(it)
        if "fulltext:include" in tags:
            tag_decision[it["key"]] = "include"
        elif "fulltext:exclude" in tags:
            tag_decision[it["key"]] = "exclude"

    if not os.path.exists(FULLTEXT_LOG):
        # A tagged-only project is acceptable; skip the cross-check.
        return

    ft_last = last_row_per_key(read_csv(FULLTEXT_LOG))
    csv_decisions = {
        k: r.get("decision", "")
        for k, r in ft_last.items()
        if r.get("decision") in ("include", "exclude")
    }

    tag_only = set(tag_decision) - set(csv_decisions)
    csv_only = set(csv_decisions) - set(tag_decision)
    mismatches = [
        k for k, d in csv_decisions.items()
        if k in tag_decision and tag_decision[k] != d
    ]

    problems = []
    if tag_only:
        problems.append(
            f"{len(tag_only)} item(s) tagged but not in CSV: "
            f"{sorted(tag_only)[:5]}"
        )
    if csv_only:
        problems.append(
            f"{len(csv_only)} CSV decision(s) without matching Zotero tag "
            f"(run `fulltext_code.py --csv-backfill`): "
            f"{sorted(csv_only)[:5]}"
        )
    if mismatches:
        problems.append(
            f"{len(mismatches)} item(s) where CSV decision differs from "
            f"Zotero tag: {sorted(mismatches)[:5]}"
        )
    assert not problems, "\n  ".join(problems)


def test_fulltext_include_items_have_slr_coding_note() -> None:
    """Every item tagged `fulltext:include` must carry a parseable
    `SLR Coding` child note. Without it, `export_coded_includes.py`
    cannot extract the coded fields and the manuscript has nothing
    to read. Skipped when Zotero is unreachable."""
    items = _live_zotero_items()
    if items is None:
        return
    try:
        from pyzotero import zotero  # type: ignore[import-not-found]
    except ImportError:
        return
    group = os.environ.get("ZOTERO_GROUP")
    api_key = os.environ.get("ZOTERO_API_KEY")
    if not group or not api_key:
        return
    z = zotero.Zotero(group, "group", api_key, local=True)

    missing: list[str] = []
    for it in items:
        if "fulltext:include" not in _tags_of(it):
            continue
        key = it["key"]
        try:
            children = z.children(key)
        except Exception:
            continue
        has_note = any(
            (c.get("data", {}).get("itemType") == "note"
             and "SLR_CODING_DATA" in (c.get("data", {}).get("note") or ""))
            for c in children
        )
        if not has_note:
            missing.append(key)

    assert not missing, (
        f"{len(missing)} item(s) tagged fulltext:include lack an SLR "
        f"Coding child note: {missing[:5]}. Re-run `fulltext_code.py "
        f"--full-recode --only-keys <...>` for the affected items."
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    r = TestRunner(verbose=args.verbose)
    r.run("pipeline artefacts exist", test_pipeline_artefacts_exist)
    r.run("search_run marker matches dedup CSV",
          test_search_run_marker_matches_dedup_csv)
    r.run("search_metadata has required fields",
          test_search_metadata_has_required_fields)
    r.run("no duplicate DOIs in dedup CSV",
          test_no_duplicate_dois_in_dedup_csv)
    r.run("abstract log uses allowed decision states",
          test_abstract_log_decision_states)
    r.run("fulltext log final decisions", test_fulltext_log_decision_states_final)
    r.run("PRISMA arithmetic", test_prisma_arithmetic)
    r.run("coded count matches fulltext includes",
          test_coded_count_matches_fulltext_includes)
    r.run("temperature=0 pinned in Claude calls",
          test_temperature_zero_pinned)
    r.run("screening_config constants match logs",
          test_screening_config_constants_match_logs)
    r.run("no errors remaining in fulltext log",
          test_no_remaining_errors_in_fulltext_log)
    r.run("no ghost keys", test_ghosts_handled_consistently)
    r.run("fulltext tags consistent with CSV log",
          test_fulltext_tags_consistent_with_csv_log)
    r.run("every fulltext:include item has SLR Coding note",
          test_fulltext_include_items_have_slr_coding_note)
    return r.report()


if __name__ == "__main__":
    sys.exit(main())
