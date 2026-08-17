"""Coverage parsing, coverage-aware ranking, and the Case 2 guard.

Every string in `REAL_STRINGS` was captured from a live Alma tenant
(2026-08-17, six DOIs, 23 distinct statements). They are the spec: a
parser built on the two examples I first saw would have missed
multi-range holdings and year-only dates entirely.

Two failures motivate all of this, and they pull in opposite directions:

- A 1988 article was handed to the Springer handler because Alma lists
  SpringerLink for the journal. The holding starts 1997. Result: paywall,
  30-second download timeout, three times in one 97-item run.
- EBSCOhost is ranked *first* by `PLATFORM_PRIORITY` and commonly carries
  a one-year moving wall, so for a very recent article the preferred
  platform is the one that cannot serve it. That risk only became live
  when Alma target matching started working at all.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fetchers.resolvers import (
    PLATFORM_PRIORITY,
    AlmaResolver,
    CoverageWindow,
    FulltextTarget,
    SfxResolver,
    covers_year,
    parse_coverage,
)

ALMA = AlmaResolver(
    "https://eu03.alma.exlibrisgroup.com/view/uresolver/358AALTO_INST/openurl"
)
SFX = SfxResolver("https://sfx.example.org/inst01")
ALMA_URL = "https://aalto.alma.exlibrisgroup.com/view/action/uresolver.do?x=1"

SPRINGER_COV = "Available from 01.01.1997 volume: 16 issue: 1.<br>"
EBSCO_COV = (
    "Available from 01.02.1982.<br>Most recent 1 year(s) not available.<br>"
)

# Captured live; the parser must handle each without special-casing.
REAL_STRINGS = [
    "Available from 01.11.1987 volume: 6 issue: 8.<br>Most recent 1 year(s) not available.<br>",
    "Available from 01.05.1982 volume: 1 issue: 2 until 31.12.1985 volume: 4 issue: 6.<br>",
    "Available from 01.02.1986 volume: 5 issue: 1 until 31.12.1987 volume: 6.<br>",
    "Available from 01.02.1982.<br>Most recent 1 year(s) not available.<br>",
    "Available from 01.02.1982 volume: 1 issue: 1.<br>Most recent 4 year(s) not available.<br>",
    "Available from 01.01.1997 volume: 16 issue: 1.<br>",
    "Available from 1996 until 2007.<br>Available from 2007.<br>Most recent 1 year(s) not available.<br>",
    "Available from 01.09.1988.<br>",
    "Available from 1999 volume: 23 issue: 3.<br>",
    "Available from 2002 volume: 27 issue: 1 until 2017 volume: 41 issue: 6.<br>",
    "Available from 01.01.1990 volume: 1 issue: 1.<br>Most recent 6 year(s) not available.<br>",
    "Available from 01.01.1999 volume: 10 issue: 1 until 30.11.2009 volume: 20 issue: 6.<br>",
    "Available from 01.01.2010.<br>",
]


def _t(coverage: str, package="", interface="") -> FulltextTarget:
    return FulltextTarget(
        url=ALMA_URL, package_name=package, interface_name=interface,
        coverage=coverage,
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", REAL_STRINGS)
def test_every_real_string_parses(text: str) -> None:
    """None means "unknown" downstream, so a real statement returning None
    would silently disable the guard for that platform."""
    assert parse_coverage(text) is not None


def test_open_ended_range() -> None:
    w = parse_coverage(SPRINGER_COV)
    assert w.ranges == ((1997, None),)
    assert w.embargo_years == 0


def test_closed_range_ignores_trailing_volume_and_issue() -> None:
    w = parse_coverage(
        "Available from 01.05.1982 volume: 1 issue: 2 until 31.12.1985 volume: 4 issue: 6.<br>"
    )
    assert w.ranges == ((1982, 1985),)


def test_year_only_dates() -> None:
    w = parse_coverage("Available from 2002 volume: 27 issue: 1 until 2017 volume: 41 issue: 6.<br>")
    assert w.ranges == ((2002, 2017),)


def test_multiple_ranges_in_one_statement() -> None:
    """The `<br>` split matters: without it the second "Available from"
    would be read as the first range's `until`."""
    w = parse_coverage(
        "Available from 1996 until 2007.<br>Available from 2007.<br>"
        "Most recent 1 year(s) not available.<br>"
    )
    assert w.ranges == ((1996, 2007), (2007, None))
    assert w.embargo_years == 1


def test_embargo_is_read() -> None:
    assert parse_coverage(EBSCO_COV).embargo_years == 1
    assert parse_coverage(
        "Available from 01.01.1990 volume: 1 issue: 1.<br>Most recent 6 year(s) not available.<br>"
    ).embargo_years == 6


@pytest.mark.parametrize("text", ["", "   ", "Unknown holdings", "<br>", None])
def test_unparseable_is_none_not_false(text) -> None:
    """The load-bearing distinction. False would stop retrieval from any
    platform whose wording we failed to anticipate."""
    assert parse_coverage(text) is None


# ---------------------------------------------------------------------------
# covers_year
# ---------------------------------------------------------------------------


def test_the_springer_1988_case() -> None:
    """The exact failure: Alma lists SpringerLink, holding starts 1997."""
    assert covers_year(SPRINGER_COV, 1988) is False
    assert covers_year(SPRINGER_COV, 2018) is True


def test_the_ebscohost_1988_case() -> None:
    """Same article, different platform — and this one holds it."""
    assert covers_year(EBSCO_COV, 1988, today_year=2026) is True


def test_moving_wall_excludes_recent_years() -> None:
    """The embargo direction: EBSCOhost is first choice yet cannot serve
    the newest year.

    The boundary year is None, not False. A one-year wall is measured in
    months, so with today in 2026 some of 2025 is released and some is
    not — and None means "attempt it", which costs a wasted try rather
    than a wrongly skipped article.
    """
    assert covers_year(EBSCO_COV, 2026, today_year=2026) is False
    assert covers_year(EBSCO_COV, 2025, today_year=2026) is None
    assert covers_year(EBSCO_COV, 2024, today_year=2026) is True


def test_a_longer_moving_wall_shifts_the_boundary() -> None:
    cov = (
        "Available from 01.02.1982 volume: 1 issue: 1.<br>"
        "Most recent 4 year(s) not available.<br>"
    )
    assert covers_year(cov, 2023, today_year=2026) is False
    assert covers_year(cov, 2022, today_year=2026) is None
    assert covers_year(cov, 2021, today_year=2026) is True


def test_closed_range_excludes_after_the_end() -> None:
    cov = "Available from 1996 volume: 17 issue: 1 until 2007.<br>"
    assert covers_year(cov, 2000) is True
    assert covers_year(cov, 2010) is False


def test_year_before_start_is_excluded() -> None:
    assert covers_year("Available from 01.01.2010.<br>", 2009) is False


def test_missing_or_unparseable_year_is_unknown() -> None:
    assert covers_year(SPRINGER_COV, None) is None
    assert covers_year(SPRINGER_COV, "n.d.") is None
    assert covers_year("", 1988) is None


def test_year_accepts_a_string_as_the_pipeline_supplies_it() -> None:
    """`_year_from_zotero_date` yields a string or None."""
    assert covers_year(SPRINGER_COV, "2018") is True
    assert covers_year(SPRINGER_COV, "1988") is False


def test_window_uses_todays_year_by_default() -> None:
    """Sanity check that the default is wired; the assertion is kept
    calendar-independent on purpose."""
    w = CoverageWindow(ranges=((1900, None),), embargo_years=1)
    assert w.covers_year(dt.date.today().year) is False
    assert w.covers_year(dt.date.today().year - 1) is None
    assert w.covers_year(dt.date.today().year - 2) is True


# ---------------------------------------------------------------------------
# Coverage-aware ranking
# ---------------------------------------------------------------------------


def test_covering_platform_beats_higher_ranked_embargoed_one() -> None:
    """The risk that ranking introduced: EBSCOhost ranks first but is
    embargoed for 2026, so the Springer route must win."""
    ebsco = _t(EBSCO_COV, "EBSCOhost Business Source Ultimate", "EBSCOhost")
    springer = _t(SPRINGER_COV, "FinELib SpringerLink", "Springer Link")
    best = min(
        [ebsco, springer],
        key=lambda t: ALMA.sort_key(t, PLATFORM_PRIORITY, pub_date=2026, today_year=2026),
    )
    assert best is springer


def test_platform_priority_still_decides_among_covering_targets() -> None:
    """Coverage is a filter, not a replacement for preference."""
    ebsco = _t(EBSCO_COV, "EBSCOhost Business Source Ultimate", "EBSCOhost")
    proquest = _t("Available from 01.11.1987.<br>", "ABI/INFORM", "ProQuest")
    best = min(
        [proquest, ebsco],
        key=lambda t: ALMA.sort_key(t, PLATFORM_PRIORITY, pub_date=2000, today_year=2026),
    )
    assert best is ebsco


def test_unknown_coverage_sorts_between_covering_and_not() -> None:
    covering = _t("Available from 1980.<br>", "X", "ProQuest")
    unknown = _t("", "Y", "JSTOR")
    excluded = _t(SPRINGER_COV, "Z", "Springer Link")
    keyed = sorted(
        [excluded, unknown, covering],
        key=lambda t: ALMA.sort_key(t, PLATFORM_PRIORITY, pub_date=1988),
    )
    assert keyed[0] is covering
    assert keyed[-1] is excluded


def test_sfx_ordering_is_untouched_by_coverage() -> None:
    """SFX sends no coverage, so every target is unknown and `rank_key`
    alone decides — exactly as before this existed."""
    ebsco = FulltextTarget(url="https://search.ebscohost.com/x")
    jstor = FulltextTarget(url="https://www.jstor.org/stable/1")
    with_year = sorted(
        [jstor, ebsco], key=lambda t: SFX.sort_key(t, pub_date=2026),
    )
    without = sorted([jstor, ebsco], key=lambda t: SFX.sort_key(t))
    assert with_year == without == [ebsco, jstor]


def test_no_pub_date_means_no_coverage_influence() -> None:
    ebsco = _t(EBSCO_COV, "EBSCOhost", "EBSCOhost")
    springer = _t(SPRINGER_COV, "SpringerLink", "Springer Link")
    best = min([ebsco, springer], key=lambda t: ALMA.sort_key(t, PLATFORM_PRIORITY))
    assert best is ebsco


# ---------------------------------------------------------------------------
# Case 2 via targets_match_domains
# ---------------------------------------------------------------------------


def _cfg():
    from unittest.mock import MagicMock

    from fetchers.library_resolver import LibraryResolverConfig
    return LibraryResolverConfig(resolver=ALMA, session=MagicMock())


def test_case_2_springer_present_but_out_of_coverage() -> None:
    """in_any True, in_range False → Case 2 → divert to the Connector
    instead of timing out on a Springer paywall."""
    from fetchers.library_resolver import targets_match_domains

    targets = [_t(SPRINGER_COV, "FinELib SpringerLink", "Springer Link")]
    cfg = _cfg()
    in_any = targets_match_domains(targets, ("springer.com",), cfg)
    in_range = targets_match_domains(
        targets, ("springer.com",), cfg, pub_date="1988",
    )
    assert in_any is True
    assert in_range is False


def test_case_3_springer_present_and_in_coverage() -> None:
    from fetchers.library_resolver import targets_match_domains

    targets = [_t(SPRINGER_COV, "FinELib SpringerLink", "Springer Link")]
    cfg = _cfg()
    assert targets_match_domains(
        targets, ("springer.com",), cfg, pub_date="2018",
    ) is True


def test_unknown_coverage_does_not_gate() -> None:
    """An SFX-shaped target has no coverage; passing pub_date must not
    turn that into a skip."""
    from fetchers.library_resolver import targets_match_domains

    targets = [FulltextTarget(url="https://link.springer.com/x")]
    cfg = _cfg()
    assert targets_match_domains(
        targets, ("springer.com",), cfg, pub_date="1988",
    ) is True


def test_a_second_covering_target_on_the_same_platform_still_matches() -> None:
    """Only one route needs to hold the year."""
    from fetchers.library_resolver import targets_match_domains

    targets = [
        _t(SPRINGER_COV, "FinELib SpringerLink", "Springer Link"),
        _t("Available from 1980.<br>", "Springer Legacy", "Springer Link"),
    ]
    assert targets_match_domains(
        targets, ("springer.com",), _cfg(), pub_date="1988",
    ) is True
