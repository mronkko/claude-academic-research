"""Live: Semantic Scholar's keyword stream returns rows under journal scope.

This is the end-to-end half of the fix in
tests/unit/test_s2_journal_scope.py, and it is the half that cannot be
stubbed. The bug was not in our handling of a response — it was that the
response never contains the field the filter keyed on. Only the real API
can show that the replacement key (the journal title) is present and
matches what `JOURNALS` declares.

Measured before the fix, one bulk query with only `issns` differing:
0 results from 911 unfiltered with three ISSNs set, 822 with none. Any
regression that reinstates ISSN-only matching returns this suite to zero.

The bulk endpoint rate-limits per key, and a paginated search burns that
limit on its own — the first version of this file ran a fresh search in
each test and reliably throttled itself into failures that looked like
regressions. Every test now reads one module-scoped fetch, so the whole
file costs a single search.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from searchers import SemanticScholarSearch
from searchers.base import SearchContext, normalize_journal_title

pytestmark = pytest.mark.live

#: A handful of high-volume journals whose titles S2 renders in more than
#: one way — which is why matching is on a normalised key.
JOURNALS = {
    "0021-9010": ("ABS 4*", "Journal of Applied Psychology"),
    "0001-4273": ("ABS 4*", "Academy of Management Journal"),
    "0149-2063": ("ABS 4*", "Journal of Management"),
    "0894-3796": ("ABS 4", "Journal of Organizational Behavior"),
    "0018-7267": ("ABS 4", "Human Relations"),
}


#: A two-year window, deliberately. Wide enough that the scoped result is
#: reliably non-empty — measured at 17 in-scope rows across these
#: journals, six under "Journal of Applied Psychology" and seven under
#: "The Journal of applied psychology" — and narrow enough to stay near a
#: single page, since paginating is what exhausts the per-key rate limit.
_FROM_YEAR, _TO_YEAR = 2016, 2017


def _scoped_ctx() -> SearchContext:
    return SearchContext(
        from_year=_FROM_YEAR, to_year=_TO_YEAR,
        issns=list(JOURNALS),
        journal_titles=[v[1] for v in JOURNALS.values()],
    )


def _is_transient(exc: Exception) -> bool:
    """True for "the server would not serve us", false for anything else.

    Two shapes reach here. `_fetch_all` raises RuntimeError with its own
    wording once the retry policy is exhausted on a 429; a 5xx escapes as
    the underlying HTTPStatusError, which the first version of this
    fixture did not catch — so a throttled run reported an ERROR that
    read exactly like the fix having regressed.
    """
    if isinstance(exc, RuntimeError) and "rate-limited" in str(exc):
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status == 429 or (status is not None and 500 <= status < 600)


def _config() -> SimpleNamespace:
    return SimpleNamespace(BLOCK_A_TERMS=["job satisfaction"], BLOCK_B_TERMS=[])


@pytest.fixture(scope="module")
def scoped_rows() -> list[dict]:
    """One search, shared by every test that needs its rows.

    Module-scoped because the alternative — a search per test — is what
    made this file fail on rate limits rather than on its assertions,
    which is worse than no test: a throttled run reads as a broken fix.
    """
    try:
        return SemanticScholarSearch().run(_config(), _scoped_ctx())
    except Exception as exc:  # noqa: BLE001 — narrowed immediately below
        if _is_transient(exc):
            # Skipping is right only for "could not get data". A skip that
            # swallowed a real defect would be the failure mode this repo
            # keeps hitting, so the predicate below stays narrow and the
            # reason is printed verbatim.
            pytest.skip(f"Semantic Scholar unavailable: {exc}")
        raise


def test_semantic_scholar_returns_rows_under_journal_scope(
    scoped_rows: list[dict],
) -> None:
    """The regression that mattered: this was 0 for every scoped run."""
    assert scoped_rows, (
        "Semantic Scholar returned no rows under journal scope. Before the "
        "title-matching fix this was the case for every run that set "
        "JOURNALS, because S2 populates no ISSN field for the filter to "
        "match on."
    )


def test_every_returned_row_is_actually_in_scope(
    scoped_rows: list[dict],
) -> None:
    """The fix must not widen the corpus to buy back those rows."""
    allowed = {normalize_journal_title(v[1]) for v in JOURNALS.values()}
    out_of_scope = [
        r["source"] for r in scoped_rows
        if normalize_journal_title(r["source"]) not in allowed
    ]
    assert not out_of_scope, (
        f"rows admitted from journals outside JOURNALS: "
        f"{sorted(set(out_of_scope))}"
    )


def test_semantic_scholar_still_supplies_no_issn() -> None:
    """The premise of the fix, asserted rather than assumed.

    If S2 ever starts populating an ISSN, the title-matching path stops
    being load-bearing and this test is the notice that the situation
    changed — the ISSN branch is kept in `_paper_in_scope` for exactly
    that day.
    """
    import http_client
    session = http_client.build_session()
    resp = session.get(
        "https://api.semanticscholar.org/graph/v1/paper/search/bulk",
        params={"query": '"job satisfaction"', "year": "2016-2017",
                "publicationTypes": "JournalArticle",
                "fields": "title,externalIds,journal"},
        timeout=60,
    )
    if resp.status_code == 429:
        pytest.skip("Semantic Scholar throttled; shared free tier")
    resp.raise_for_status()
    papers = resp.json().get("data") or []
    assert papers, "no papers returned; cannot assess the ISSN premise"
    issn_keys = {
        key
        for paper in papers
        for key in (paper.get("externalIds") or {})
        if key.upper().startswith("ISSN")
    }
    assert not issn_keys, (
        f"Semantic Scholar now returns {sorted(issn_keys)} in externalIds. "
        f"ISSN matching is viable again — revisit `_paper_in_scope`."
    )
    named = sum(1 for p in papers if (p.get("journal") or {}).get("name"))
    assert named > len(papers) * 0.5, (
        f"only {named}/{len(papers)} papers carry a journal name; title "
        f"matching depends on that field being populated."
    )
