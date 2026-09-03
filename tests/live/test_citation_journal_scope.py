"""Live: OpenAlex ANDs a venue filter into a `cites:` query.

The efficiency claim behind journal-scoped citation search is entirely
about where the filter runs. If OpenAlex applies it server-side, the
out-of-scope citing works are never transferred; if it silently ignored
the extra filter, the scoped run would fetch exactly as much as the open
one and quietly return everything, and no unit test could tell the
difference because both paths only assert on a filter string we built
ourselves.

Measured when this landed, on Dawson & Richter (2006) across five
journals: 1670 citing works open-scope, 68 scoped. The assertions below
are deliberately loose about the exact counts — citation counts grow —
and strict about the two properties that matter: scoping reduces the
result substantially, and every record it returns is in a listed venue.
"""

from __future__ import annotations

import pytest
from searchers import OpenAlexSearch
from searchers.base import SearchContext, normalize_journal_title

pytestmark = pytest.mark.live

SEED = "10.1037/0021-9010.91.4.917"

JOURNALS = {
    "0021-9010": "Journal of Applied Psychology",
    "0001-4273": "Academy of Management Journal",
    "0149-2063": "Journal of Management",
    "0894-3796": "Journal of Organizational Behavior",
    "0018-7267": "Human Relations",
}
FROM_YEAR, TO_YEAR = 2007, 2012


def _ctx(*, scope: bool) -> SearchContext:
    return SearchContext(
        from_year=FROM_YEAR, to_year=TO_YEAR,
        issns=list(JOURNALS), journal_titles=list(JOURNALS.values()),
        citation_journal_scope=scope,
    )


def test_scoping_reduces_the_pull_substantially() -> None:
    """The whole point: fewer records fetched, not merely fewer kept."""
    open_rows = OpenAlexSearch().run_citations([SEED], _ctx(scope=False))
    scoped_rows = OpenAlexSearch().run_citations([SEED], _ctx(scope=True))
    assert open_rows, "no citing works at all — the seed lookup failed"
    assert len(scoped_rows) < len(open_rows) / 2, (
        f"scoped={len(scoped_rows)} vs open={len(open_rows)}: the venue "
        f"filter does not appear to be restricting anything. If OpenAlex "
        f"ignored `primary_location.source.issn:` inside a `cites:` query, "
        f"a scoped run would fetch everything and report it as in scope."
    )


def test_every_scoped_record_is_in_a_listed_journal() -> None:
    """Server-side filtering has to mean what it says — nothing
    downstream re-checks the venue of a citation-stream record."""
    rows = OpenAlexSearch().run_citations([SEED], _ctx(scope=True))
    assert rows, "scoped citation search returned nothing"
    allowed = {normalize_journal_title(t) for t in JOURNALS.values()}
    stray = sorted({
        r["source"] for r in rows
        if normalize_journal_title(r["source"]) not in allowed
    })
    assert not stray, f"records from unlisted venues: {stray}"


def test_the_open_stream_still_reaches_beyond_the_journal_list() -> None:
    """The other half of the contract. `--citation-journal-scope off` has
    to remain genuinely open, since that is the behaviour the stream was
    built for and the reason it finds what a keyword search cannot."""
    rows = OpenAlexSearch().run_citations([SEED], _ctx(scope=False))
    allowed = {normalize_journal_title(t) for t in JOURNALS.values()}
    outside = {
        r["source"] for r in rows
        if r["source"] and normalize_journal_title(r["source"]) not in allowed
    }
    assert len(outside) > 20, (
        f"open-scope citation search reached only {len(outside)} venue(s) "
        f"outside the journal list; it should reach many."
    )
