"""Live: forward citation search against OpenAlex and Semantic Scholar.

The unit tests stub both APIs, so they pin our handling of a response
shape rather than the shape itself. Three things about that shape can
only be established against the real services, and each one silently
returns zero rows if we have it wrong:

- OpenAlex's `cites:` filter takes a short work id (`W2015257908`), not
  a DOI and not the full `https://openalex.org/...` IRI, so a citation
  search is always resolve-then-page.
- Semantic Scholar's `/citations` wraps each entry as
  `{"citingPaper": {...}}` rather than returning papers directly.
- Neither endpoint filters by year, and OpenAlex's page-number paging
  caps at 10,000 — the cursor path exists for seeds above it.

The seed is Dawson & Richter (2006), a methods paper in JAP with a few
thousand citing works spread across journals that share none of its
topic vocabulary — which is the case forward snowballing exists for.
"""

from __future__ import annotations

import pytest
from searchers import OpenAlexSearch, SemanticScholarSearch
from searchers.base import DISCOVERY_CITATION, SearchContext

pytestmark = pytest.mark.live

#: Dawson & Richter (2006), "Probing three-way interactions in moderated
#: multiple regression", J. Applied Psychology 91(4), 917-926.
SEED_DOI = "10.1037/0021-9010.91.4.917"

#: A narrow window keeps the assertion cheap and the result stable —
#: past years do not gain new citing papers.
FROM_YEAR, TO_YEAR = 2007, 2010


def _ctx() -> SearchContext:
    # No ISSNs: a citation search must not be venue-scoped, and passing
    # a list here is how we would find out if one leaked into the query.
    return SearchContext(from_year=FROM_YEAR, to_year=TO_YEAR, issns=[])


def _assert_citation_rows(rows: list[dict], source_name: str) -> None:
    assert rows, (
        f"{source_name} returned no citing works for {SEED_DOI} in "
        f"{FROM_YEAR}-{TO_YEAR}. This seed has thousands of citations; "
        f"an empty result means the request shape is wrong, not that the "
        f"literature is."
    )
    for row in rows:
        assert row["discovery_source"] == DISCOVERY_CITATION
        assert row["query"] == f"cites:{SEED_DOI}"
        assert row["db"] == source_name
    years = [int(r["year"]) for r in rows if r["year"]]
    assert years, f"{source_name} returned no publication years"
    assert min(years) >= FROM_YEAR and max(years) <= TO_YEAR, (
        f"{source_name} returned works outside {FROM_YEAR}-{TO_YEAR}: "
        f"{sorted(set(years))}"
    )
    # The property the whole stream exists for: it reaches beyond any one
    # journal. A single-venue result would mean a scope filter leaked in.
    venues = {r["source"] for r in rows if r["source"]}
    assert len(venues) > 5, (
        f"{source_name} returned works from only {len(venues)} venue(s) "
        f"({sorted(venues)}). A citation search must not be journal-scoped."
    )


def test_openalex_lists_works_citing_the_seed() -> None:
    rows = OpenAlexSearch().run_citations([SEED_DOI], _ctx())
    _assert_citation_rows(rows, "openalex")


def test_openalex_resolves_the_seed_doi_to_a_work_id() -> None:
    """The step `cites:` depends on. A DOI passed straight to the filter
    matches nothing and returns an empty page, not an error."""
    work_id = OpenAlexSearch()._resolve_work_id(SEED_DOI, _ctx())
    assert work_id.startswith("W"), (
        f"expected a short OpenAlex work id, got {work_id!r}"
    )


def test_openalex_reports_an_unknown_seed_rather_than_failing() -> None:
    """A DOI OpenAlex has never indexed. One bad seed must not sink a run."""
    src = OpenAlexSearch()
    assert src._resolve_work_id("10.9999/nonexistent.doi.xyz", _ctx()) == ""
    assert src.run_citations(["10.9999/nonexistent.doi.xyz"], _ctx()) == []


def test_semantic_scholar_lists_works_citing_the_seed() -> None:
    rows = SemanticScholarSearch().run_citations([SEED_DOI], _ctx())
    _assert_citation_rows(rows, "semantic_scholar")


def test_the_two_sources_agree_on_the_seed_being_widely_cited() -> None:
    """Not an equality check — coverage genuinely differs between the two,
    which is why a protocol runs both. This catches the case where one of
    them quietly returns a near-empty result while the other does not."""
    oa = len(OpenAlexSearch().run_citations([SEED_DOI], _ctx()))
    s2 = len(SemanticScholarSearch().run_citations([SEED_DOI], _ctx()))
    assert oa > 20 and s2 > 20, (
        f"openalex={oa}, semantic_scholar={s2} citing works in "
        f"{FROM_YEAR}-{TO_YEAR}; both should be well above 20."
    )
