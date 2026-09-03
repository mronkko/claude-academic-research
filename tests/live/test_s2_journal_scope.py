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

These tests are throttle-prone: the bulk endpoint rate-limits per key and
the free tier is shared. A 429 here is not a failure of the fix.
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


def _scoped_ctx() -> SearchContext:
    return SearchContext(
        from_year=2015, to_year=2018,
        issns=list(JOURNALS),
        journal_titles=[v[1] for v in JOURNALS.values()],
    )


def _config() -> SimpleNamespace:
    return SimpleNamespace(BLOCK_A_TERMS=["job satisfaction"], BLOCK_B_TERMS=[])


def test_semantic_scholar_returns_rows_under_journal_scope() -> None:
    """The regression that mattered: this was 0 for every scoped run."""
    rows = SemanticScholarSearch().run(_config(), _scoped_ctx())
    assert rows, (
        "Semantic Scholar returned no rows under journal scope. Before the "
        "title-matching fix this was the case for every run that set "
        "JOURNALS, because S2 populates no ISSN field for the filter to "
        "match on."
    )


def test_every_returned_row_is_actually_in_scope() -> None:
    """The fix must not widen the corpus to buy back those rows."""
    rows = SemanticScholarSearch().run(_config(), _scoped_ctx())
    allowed = {normalize_journal_title(v[1]) for v in JOURNALS.values()}
    out_of_scope = [
        r["source"] for r in rows
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
