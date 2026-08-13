"""Trim-stage journal diversity for the live mini-SLR (BACKLOG.md "L1").

The first live run's `verify` stage failed because all 3 items that
passed abstract screening ended `error`/`no_pdf` — no PDF was ever
attached. Root cause was upstream of the PDF code: `stage_trim` called
`filter_search_results.py --top-n 8`, which sorts by year descending.
The corpus is a single closed year (2019), so that sort is one giant tie
that just preserves `search.py`'s dedup order, and all 8 rows came back
Small Business Economics. Zero JBV (Elsevier), zero SEJ (Wiley) — so
`ELSEVIER_API_KEY` and `WILEY_TDM_TOKEN` were configured and never
exercised, in a corpus whose whole purpose is to exercise all three.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

JOURNALS = {
    "0883-9026": ("n/a", "Journal of Business Venturing"),
    "1932-4391": ("n/a", "Strategic Entrepreneurship Journal"),
    "0921-898X": ("n/a", "Small Business Economics"),
}


@pytest.fixture(scope="module")
def mini_slr():
    """Load `scripts/dev/mini_slr.py` without executing its CLI.

    It lives outside the package tree and carries PEP 723 deps, so it is
    loaded by path rather than imported.
    """
    path = REPO_ROOT / "scripts" / "dev" / "mini_slr.py"
    name = "mini_slr_under_test"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # Register before exec: the module defines a @dataclass, and
    # dataclasses resolves annotations via `sys.modules[cls.__module__]`.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        del sys.modules[name]
        raise
    return module


def _row(issn: str, doi: str, *, source: str = "", year: str = "2019") -> dict:
    return {"issn": issn, "doi": doi, "source": source, "year": year}


# --- the regression ---------------------------------------------------

def test_all_journals_represented_when_one_dominates(mini_slr) -> None:
    """The live failure: 40 Small Business Economics rows first, a
    handful of the others after. Top-N-by-year kept only the first 8."""
    rows = (
        [_row("0921-898X", f"10.1007/sbe{i}") for i in range(40)]
        + [_row("0883-9026", f"10.1016/jbv{i}") for i in range(5)]
        + [_row("1932-4391", f"10.1002/sej{i}") for i in range(5)]
    )
    picked = mini_slr._balanced_sample(rows, JOURNALS, 8)

    assert len(picked) == 8
    covered = {mini_slr._journal_of(r, JOURNALS) for r in picked}
    assert covered == set(JOURNALS), (
        "a three-publisher corpus must exercise three publishers"
    )


def test_sample_is_evenly_spread(mini_slr) -> None:
    rows = [
        _row(issn, f"10.{i}/{issn}")
        for issn in JOURNALS for i in range(10)
    ]
    picked = mini_slr._balanced_sample(rows, JOURNALS, 9)
    counts = {}
    for r in picked:
        key = mini_slr._journal_of(r, JOURNALS)
        counts[key] = counts.get(key, 0) + 1
    assert counts == {k: 3 for k in JOURNALS}


def test_thin_journal_does_not_shrink_the_sample(mini_slr) -> None:
    """One journal returning a single row must not cost us the quota —
    the others absorb the shortfall."""
    rows = (
        [_row("0921-898X", f"10.1007/sbe{i}") for i in range(20)]
        + [_row("0883-9026", f"10.1016/jbv{i}") for i in range(20)]
        + [_row("1932-4391", "10.1002/sej-only-one")]
    )
    picked = mini_slr._balanced_sample(rows, JOURNALS, 8)
    assert len(picked) == 8
    assert sum(
        1 for r in picked if mini_slr._journal_of(r, JOURNALS) == "1932-4391"
    ) == 1


def test_returns_everything_when_corpus_is_smaller_than_target(mini_slr) -> None:
    rows = [_row("0921-898X", "10.1007/a"), _row("0883-9026", "10.1016/b")]
    assert len(mini_slr._balanced_sample(rows, JOURNALS, 8)) == 2


def test_sample_is_deterministic(mini_slr) -> None:
    """Reruns of a frozen corpus must stay comparable."""
    rows = [
        _row(issn, f"10.{i}/{issn}", year=str(2015 + i))
        for issn in JOURNALS for i in range(6)
    ]
    first = mini_slr._balanced_sample(rows, JOURNALS, 8)
    second = mini_slr._balanced_sample(list(reversed(rows)), JOURNALS, 8)
    assert [r["doi"] for r in first] == [r["doi"] for r in second]


def test_newest_first_within_a_journal(mini_slr) -> None:
    rows = [
        _row("0883-9026", "10.1016/old", year="2015"),
        _row("0883-9026", "10.1016/new", year="2019"),
    ]
    picked = mini_slr._balanced_sample(rows, JOURNALS, 1)
    assert picked[0]["doi"] == "10.1016/new"


# --- journal identification -------------------------------------------

@pytest.mark.parametrize("issn", ["0883-9026", "08839026", " 0883-9026 "])
def test_issn_matching_tolerates_formatting(mini_slr, issn) -> None:
    assert mini_slr._journal_of(_row(issn, "10.1/x"), JOURNALS) == "0883-9026"


def test_multi_valued_issn_field_matches(mini_slr) -> None:
    """Databases return print + electronic ISSNs in one field."""
    row = _row("1540-6520, 0883-9026", "10.1/x")
    assert mini_slr._journal_of(row, JOURNALS) == "0883-9026"


def test_falls_back_to_journal_name_without_an_issn(mini_slr) -> None:
    """Not every database returns an ISSN on every row."""
    row = _row("", "10.1/x", source="Journal of Business Venturing")
    assert mini_slr._journal_of(row, JOURNALS) == "0883-9026"


def test_unrelated_row_matches_nothing(mini_slr) -> None:
    row = _row("9999-9999", "10.1/x", source="Some Other Journal")
    assert mini_slr._journal_of(row, JOURNALS) is None


def test_unmatched_rows_only_top_up_a_short_sample(mini_slr) -> None:
    """A stray row must never displace the coverage this exists for."""
    rows = (
        [_row("9999-9999", f"10.9/stray{i}") for i in range(20)]
        + [_row("0883-9026", "10.1016/jbv1")]
    )
    picked = mini_slr._balanced_sample(rows, JOURNALS, 4)
    assert any(mini_slr._journal_of(r, JOURNALS) == "0883-9026" for r in picked)
    assert len(picked) == 4
