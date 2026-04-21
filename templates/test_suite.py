#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyzotero>=1.6",
# ]
# ///
"""Project-level test suite template for a systematic review.

Copy this file into your SLR project's `scripts/test_suite.py` and
customise the **project-specific** sections at the bottom (coding
fields, forbidden methodology literals, manuscript paths). The
**universal** tests at the top verify invariants every SR pipeline
should satisfy:

- All expected stage artefacts exist and are non-empty.
- The search-integrity gatekeeper (`search_run.json`) matches the
  deduplicated CSV.
- No duplicate DOIs in the deduplicated search output.
- Abstract and full-text logs use only allowed decision states.
- PRISMA arithmetic: fulltext-screened items all come from the
  abstract-include+borderline set.
- Temperature=0 pinned in every Claude API call.
- Model / prompt-version constants in scripts match what was logged.
- Better BibTeX keys are non-empty and unique in the includes view.
- No "ghost" items (items in logs but absent from Zotero).
- Every coded paper's PDF passes the `%PDF-` magic-byte check.

Run:
    uv run scripts/test_suite.py        # concise output
    uv run scripts/test_suite.py -v     # list every test

Exit code 0 if all pass, 1 if any fail. Designed to run before any
manuscript render, and inside the `critic-loop` skill's test gate.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter
from collections.abc import Callable

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------


class TestRunner:
    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def run(self, name: str, fn: Callable[[], None]) -> None:
        try:
            fn()
            self.passed.append(name)
            if self.verbose:
                print(f"  ✓ {name}", flush=True)
        except AssertionError as e:
            self.failed.append((name, str(e)))
            print(f"  ✗ {name}\n      {e}", flush=True)
        except Exception as e:
            self.failed.append((name, f"unhandled {type(e).__name__}: {e}"))
            print(f"  ✗ {name}\n      unhandled {type(e).__name__}: {e}",
                  flush=True)

    def report(self) -> int:
        total = len(self.passed) + len(self.failed)
        print(f"\n{'=' * 60}")
        print(f"Tests passed: {len(self.passed)}/{total}")
        if self.failed:
            print(f"Failures ({len(self.failed)}):")
            for name, err in self.failed:
                print(f"  - {name}: {err}")
            return 1
        print("ALL PASS.")
        return 0


def must_exist(path: str) -> None:
    assert os.path.exists(path), f"missing file: {path}"
    assert os.path.getsize(path) > 0, f"empty file: {path}"


def read_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def last_row_per_key(rows: list[dict], key_col: str = "item_key") -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in rows:
        k = r.get(key_col)
        if k:
            out[k] = r
    return out


# ---------------------------------------------------------------------------
# Paths — edit for your project
# ---------------------------------------------------------------------------

SEARCH_RAW      = os.path.join(PROJECT_ROOT, "analysis/raw/search_results_raw.csv")
SEARCH_DEDUP    = os.path.join(PROJECT_ROOT, "analysis/raw/search_results.csv")
SEARCH_METADATA = os.path.join(PROJECT_ROOT, "search_metadata.json")
SEARCH_RUN      = os.path.join(PROJECT_ROOT, "search_run.json")
ABSTRACT_LOG    = os.path.join(PROJECT_ROOT, "screening/abstract_screening.csv")
FULLTEXT_LOG    = os.path.join(PROJECT_ROOT, "screening/fulltext_screening.csv")
CODED_PAPERS    = os.path.join(PROJECT_ROOT, "analysis/results/coded_papers.csv")
# Screening config — the project's per-review `screening_config.py`
# is the canonical source of MODEL and PROMPT_VERSION constants (the
# screening scripts read them via `getattr(mod, "ABSTRACT_SCREENING_MODEL",
# ...)`). The scripts themselves live inside the plugin and are invoked
# by path, so they typically aren't copied into the project.
SCREENING_CONFIG = os.path.join(PROJECT_ROOT, "screening_config.py")
# Legacy paths kept for projects that did copy the screening scripts
# alongside the plugin invocation. Tests below skip gracefully when
# absent.
ABSTRACT_SCRIPT = os.path.join(PROJECT_ROOT, "scripts/abstract_screen.py")
FULLTEXT_SCRIPT = os.path.join(PROJECT_ROOT, "scripts/fulltext_code.py")

# Project-specific: edit these three blocks for your SR.
STATS_JSON     = os.path.join(PROJECT_ROOT, "analysis/results/stats.json")
MANUSCRIPT_QMD = os.path.join(PROJECT_ROOT, "manuscript/review.qmd")
REFERENCES_BIB = os.path.join(PROJECT_ROOT, "manuscript/references.bib")


# ---------------------------------------------------------------------------
# Universal tests — should pass in every SR project
# ---------------------------------------------------------------------------


def test_pipeline_artefacts_exist() -> None:
    for p in [SEARCH_DEDUP, SEARCH_METADATA, SEARCH_RUN,
              ABSTRACT_LOG, FULLTEXT_LOG, CODED_PAPERS]:
        must_exist(p)


def test_search_run_marker_matches_dedup_csv() -> None:
    """`search_run.json`'s DOI count must equal the deduped CSV's DOI count."""
    with open(SEARCH_RUN) as f:
        marker = json.load(f)
    rows = read_csv(SEARCH_DEDUP)
    csv_dois = sum(1 for r in rows if r.get("doi", "").strip())
    assert marker["unique_dois"] == csv_dois, (
        f"search_run.json says {marker['unique_dois']} unique DOIs, "
        f"CSV has {csv_dois}"
    )


def test_search_metadata_has_required_fields() -> None:
    with open(SEARCH_METADATA) as f:
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
        f"non-final decisions in fulltext log: {decisions - {'include','exclude'}}"
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

    Looks at any local copy of `abstract_screen.py` / `fulltext_code.py`
    if present (legacy layout). Projects that invoke the plugin scripts
    by path get this invariant enforced by the plugin's own test suite,
    so the test silently passes when no local copies exist.
    """
    for script in (ABSTRACT_SCRIPT, FULLTEXT_SCRIPT):
        if not os.path.exists(script):
            continue
        with open(script) as f:
            src = f.read()
        ok = (re.search(r'temperature\s*=\s*0\b', src) is not None
              or re.search(r'TEMPERATURE\s*=\s*0\b', src) is not None
              or re.search(r'"temperature"\s*:\s*0\b', src) is not None)
        assert ok, f"{script}: no temperature=0 setting found"


def test_screening_config_constants_in_log() -> None:
    """`screening_config.py` is the canonical source of MODEL and
    PROMPT_VERSION (the pipeline scripts read them via getattr). Each
    fulltext-log row's logged model/prompt_version must match what's
    declared in the config, so we can tell when the config changed but
    a re-run didn't happen."""
    if not os.path.exists(SCREENING_CONFIG):
        return
    with open(SCREENING_CONFIG) as f:
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


def test_bbt_keys_unique_in_coded_papers() -> None:
    rows = read_csv(CODED_PAPERS)
    keys = [r["bibtex_key"] for r in rows if r.get("bibtex_key")]
    missing = [r["item_key"] for r in rows if not (r.get("bibtex_key") or "").strip()]
    assert not missing, (
        f"{len(missing)} included papers missing bibtex_key: {missing[:5]}"
    )
    dups = [k for k, c in Counter(keys).items() if c > 1]
    assert not dups, f"duplicate bibtex_keys: {dups[:5]}"


def test_no_remaining_errors_in_fulltext_log() -> None:
    """After --rerun, no live decision should be 'error'."""
    rows = read_csv(FULLTEXT_LOG)
    last = last_row_per_key(rows)
    errors = [k for k, r in last.items() if r.get("decision") == "error"]
    assert not errors, f"{len(errors)} items still in error state: {errors[:5]}"


def test_ghosts_handled_consistently() -> None:
    """Items in the fulltext log must exist in Zotero. Skip if pyzotero unavailable
    or local Zotero not running."""
    try:
        from pyzotero import zotero
    except ImportError:
        return
    group = os.environ.get("ZOTERO_GROUP")
    api_key = os.environ.get("ZOTERO_API_KEY")
    if not group or not api_key:
        return
    try:
        z = zotero.Zotero(group, "group", api_key, local=True)
        live_keys = {it["key"] for it in z.everything(z.items(itemType="journalArticle"))}
    except Exception:
        return
    ft_last = last_row_per_key(read_csv(FULLTEXT_LOG))
    ghosts = [k for k in ft_last if k not in live_keys]
    assert not ghosts, f"fulltext log references non-Zotero keys: {ghosts[:5]}"


# ---------------------------------------------------------------------------
# Project-specific tests — uncomment and edit for your SR
# ---------------------------------------------------------------------------


# Coding-field completeness. List the field names your coding schema uses.
# REQUIRED_CODING_FIELDS: tuple[str, ...] = (
#     "motivational_constructs",
#     "level_of_analysis",
#     "sample",
#     "method",
#     "key_findings",
# )
#
# def test_coded_papers_have_all_required_fields() -> None:
#     rows = read_csv(CODED_PAPERS)
#     bad: list[tuple[str, str]] = []
#     for r in rows:
#         for fld in REQUIRED_CODING_FIELDS:
#             if not (r.get(fld) or "").strip():
#                 bad.append((r["item_key"], fld))
#     assert not bad, (
#         f"{len(bad)} empty field(s) across {len({k for k,_ in bad})} papers"
#     )


# Forbidden methodology literals in manuscript prose. Add model names,
# version strings, search dates, screening counts — anything that must
# come from inline expressions (per empirical-integrity skill) rather than
# hand-typed in prose.
# FORBIDDEN_LITERALS: tuple[str, ...] = (
#     "claude-haiku", "claude-sonnet", "temperature=0",
#     # Specific counts from your SR — replace with yours:
#     # " 81 papers", "1,708 unique",
# )
#
# def test_no_forbidden_methodology_literals_in_manuscript() -> None:
#     if not os.path.exists(MANUSCRIPT_QMD):
#         return
#     with open(MANUSCRIPT_QMD) as f:
#         src = f.read()
#     # Strip YAML frontmatter and fenced code chunks
#     body = re.sub(r"^---\n.*?\n---\n", "", src, count=1, flags=re.S)
#     body = re.sub(r"```\{[^}]*\}.*?```", "", body, flags=re.S)
#     body = re.sub(r"```.*?```", "", body, flags=re.S)
#     hits = [lit for lit in FORBIDDEN_LITERALS if lit in body]
#     assert not hits, f"forbidden literals in manuscript prose: {hits}"


# Every @citekey in the manuscript must resolve in references.bib.
# def test_manuscript_citekeys_resolve() -> None:
#     if not (os.path.exists(MANUSCRIPT_QMD) and os.path.exists(REFERENCES_BIB)):
#         return
#     with open(MANUSCRIPT_QMD) as f:
#         src = f.read()
#     keys_in_prose = set(re.findall(r"@([\w:-]+)", src))
#     with open(REFERENCES_BIB) as f:
#         bib = f.read()
#     known = set(re.findall(r"@\w+\{([^,]+),", bib))
#     missing = sorted(keys_in_prose - known)
#     assert not missing, f"{len(missing)} manuscript citekeys not in bib: {missing[:5]}"


# stats.json freshness check — refuse to render manuscript with stale stats.
# def test_stats_json_fresh_vs_coded_csv() -> None:
#     if not (os.path.exists(STATS_JSON) and os.path.exists(CODED_PAPERS)):
#         return
#     assert os.path.getmtime(STATS_JSON) >= os.path.getmtime(CODED_PAPERS), (
#         "stats.json is older than coded_papers.csv — re-run analysis/stats.py"
#     )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    r = TestRunner(verbose=args.verbose)

    # Universal
    r.run("pipeline artefacts exist", test_pipeline_artefacts_exist)
    r.run("search_run marker matches dedup CSV", test_search_run_marker_matches_dedup_csv)
    r.run("search_metadata has required fields", test_search_metadata_has_required_fields)
    r.run("no duplicate DOIs in dedup CSV", test_no_duplicate_dois_in_dedup_csv)
    r.run("abstract log uses allowed decision states", test_abstract_log_decision_states)
    r.run("fulltext log final decisions", test_fulltext_log_decision_states_final)
    r.run("PRISMA arithmetic", test_prisma_arithmetic)
    r.run("coded count matches fulltext includes", test_coded_count_matches_fulltext_includes)
    r.run("temperature=0 pinned in Claude calls", test_temperature_zero_pinned)
    r.run("screening_config constants match log", test_screening_config_constants_in_log)
    r.run("BBT keys unique in coded papers", test_bbt_keys_unique_in_coded_papers)
    r.run("no errors remaining in fulltext log", test_no_remaining_errors_in_fulltext_log)
    r.run("no ghost keys", test_ghosts_handled_consistently)

    # Project-specific — uncomment the corresponding test above and the
    # r.run() line below when ready.
    # r.run("required coding fields populated", test_coded_papers_have_all_required_fields)
    # r.run("no forbidden literals in manuscript", test_no_forbidden_methodology_literals_in_manuscript)
    # r.run("manuscript citekeys resolve", test_manuscript_citekeys_resolve)
    # r.run("stats.json fresh", test_stats_json_fresh_vs_coded_csv)

    return r.report()


if __name__ == "__main__":
    sys.exit(main())
