"""Semantic Scholar's keyword stream returned zero rows in every scoped run.

`SemanticScholarSearch.run()` post-filters client-side against
`ctx.issns`, because S2 cannot filter by ISSN server-side. The filter's
only source of an ISSN was `externalIds["ISSN"]` / `["ISSNs"]`, and S2
never populates either. Sampled live across 500 papers, the keys
returned are MAG, DOI, CorpusId, PubMed, PubMedCentral, DBLP and ArXiv;
the `journal` object carries name, volume and pages, and no ISSN.

So the filter rejected every paper whenever `JOURNALS` was non-empty.
Measured on one query with only `issns` differing: 0 results from 911
unfiltered with three ISSNs set, 822 with none. `JOURNALS` is a required
key in search_config.py, which makes that every journal-scoped run this
pipeline has ever done — reported as `semantic_scholar: 0` in
`per_database_counts` with nothing saying why. The dead
`if isinstance(journal.get("name"), str): pass` stub left inside the
filter suggests the gap was seen and not closed.

The fix matches on the journal *title*, which `JOURNALS` already carries
and S2 does return. Two properties of the live data shape it:

- **S2 renders the same journal inconsistently.** One query returned
  both "Journal of Applied Psychology" (5 papers) and "The Journal of
  applied psychology" (8). Exact string equality would have found a
  third of that journal's papers, so matching is on a normalised key.
- **HTML entities leak through** ("Strategic Journal of Business &amp;
  Change Management"), so unescaping comes before normalising.

And a filter that rejects a whole non-empty result set now says so.
That is the shape of this bug in general: silence made a scope mismatch
indistinguishable from an empty literature.
"""

from __future__ import annotations

from types import SimpleNamespace

from searchers import SemanticScholarSearch
from searchers.base import SearchContext, normalize_journal_title

JAP = "Journal of Applied Psychology"


def _ctx(**kw) -> SearchContext:
    return SearchContext(
        from_year=kw.get("from_year", 2015),
        to_year=kw.get("to_year", 2018),
        issns=kw.get("issns", ["0021-9010"]),
        journal_titles=kw.get("journal_titles", [JAP]),
    )


def _paper(**kw) -> dict:
    paper = {
        "paperId": kw.get("paper_id", "p1"),
        "title": "A paper",
        "year": 2016,
        "externalIds": kw.get("external_ids", {"DOI": "10.1/x", "MAG": "9"}),
        "authors": [{"name": "A B"}],
        "publicationTypes": ["JournalArticle"],
    }
    if "journal" in kw:
        paper["journal"] = kw["journal"]
    else:
        paper["journal"] = {"name": kw.get("journal_name", JAP)}
    return paper


# ---------------------------------------------------------------------------
# normalize_journal_title
# ---------------------------------------------------------------------------


def test_normalisation_ignores_case() -> None:
    assert normalize_journal_title(JAP) == normalize_journal_title(JAP.lower())


def test_normalisation_ignores_a_leading_the() -> None:
    """The live case: S2 returned both forms of the same journal in one
    query, and exact equality would have matched only the shorter one."""
    assert (normalize_journal_title("The Journal of applied psychology")
            == normalize_journal_title(JAP))


def test_normalisation_unescapes_html_entities() -> None:
    """S2 returns `Business &amp; Change Management` verbatim."""
    assert (normalize_journal_title("Business &amp; Change Management")
            == normalize_journal_title("Business & Change Management"))


def test_normalisation_treats_ampersand_and_and_alike() -> None:
    assert (normalize_journal_title("Business & Society")
            == normalize_journal_title("Business and Society"))


def test_normalisation_ignores_punctuation_and_spacing() -> None:
    assert (normalize_journal_title("Journal of Applied Psychology.")
            == normalize_journal_title("Journal  of applied-psychology"))


def test_normalisation_keeps_distinct_journals_distinct() -> None:
    """Normalising must not collapse different journals into one. The
    whole filter is a scope boundary; a false match widens the corpus
    past what the protocol declared."""
    assert (normalize_journal_title("Journal of Management")
            != normalize_journal_title("Journal of Management Studies"))
    assert (normalize_journal_title("Journal of Applied Psychology")
            != normalize_journal_title("Journal of Applied Psychometrics"))


def test_normalisation_of_empty_input_is_empty() -> None:
    """An empty key must never match an empty journal name — otherwise a
    paper with no journal at all lands inside every scope."""
    assert normalize_journal_title("") == ""
    assert normalize_journal_title(None) == ""


# ---------------------------------------------------------------------------
# SearchContext
# ---------------------------------------------------------------------------


def test_context_exposes_normalised_title_keys() -> None:
    ctx = _ctx(journal_titles=["The Journal of Applied Psychology"])
    assert normalize_journal_title(JAP) in ctx.journal_title_keys()


def test_context_without_titles_has_no_keys() -> None:
    """Backward compatible: every existing SearchContext construction
    omits the new field."""
    ctx = SearchContext(from_year=2015, to_year=2018, issns=["0021-9010"])
    assert ctx.journal_title_keys() == frozenset()


def test_context_ignores_a_blank_title() -> None:
    ctx = _ctx(journal_titles=[JAP, "", "   "])
    assert ctx.journal_title_keys() == frozenset({normalize_journal_title(JAP)})


# ---------------------------------------------------------------------------
# The filter itself
# ---------------------------------------------------------------------------


def test_paper_in_a_listed_journal_is_kept() -> None:
    """The case that returned zero for every scoped run."""
    src = SemanticScholarSearch()
    assert src._paper_in_scope(_paper(), _ctx())


def test_paper_in_a_listed_journal_under_a_variant_name_is_kept() -> None:
    src = SemanticScholarSearch()
    assert src._paper_in_scope(
        _paper(journal_name="The Journal of applied psychology"), _ctx(),
    )


def test_paper_in_an_unlisted_journal_is_dropped() -> None:
    src = SemanticScholarSearch()
    assert not src._paper_in_scope(
        _paper(journal_name="Frontiers in Psychology"), _ctx(),
    )


def test_paper_with_no_journal_name_is_dropped() -> None:
    """22 of 1000 live papers carry no journal name. Unmatchable is not
    the same as in scope."""
    src = SemanticScholarSearch()
    assert not src._paper_in_scope(_paper(journal=None), _ctx())
    assert not src._paper_in_scope(_paper(journal={}), _ctx())


def test_an_issn_still_matches_when_s2_supplies_one() -> None:
    """Kept deliberately: it costs nothing, it is the stronger signal,
    and S2 may start populating the field."""
    src = SemanticScholarSearch()
    assert src._paper_in_scope(
        _paper(journal_name="Some Renamed Journal",
               external_ids={"ISSN": "0021-9010"}),
        _ctx(),
    )


def test_an_unscoped_context_keeps_everything() -> None:
    """A citation search passes no scope, and must not be filtered."""
    src = SemanticScholarSearch()
    ctx = SearchContext(from_year=2015, to_year=2018, issns=[],
                        journal_titles=[])
    assert src._paper_in_scope(_paper(journal_name="Anything At All"), ctx)


def test_a_context_with_issns_but_no_titles_still_filters_on_issn() -> None:
    """An older caller that sets only `issns` keeps its old semantics
    rather than silently becoming unscoped."""
    src = SemanticScholarSearch()
    ctx = SearchContext(from_year=2015, to_year=2018, issns=["0021-9010"])
    assert not src._paper_in_scope(_paper(journal_name=JAP), ctx)
    assert src._paper_in_scope(
        _paper(external_ids={"ISSN": "0021-9010"}), ctx,
    )


# ---------------------------------------------------------------------------
# The silence that hid it
# ---------------------------------------------------------------------------


class _StubS2(SemanticScholarSearch):
    def __init__(self, papers):
        super().__init__()
        self._papers = papers

    def _fetch_all(self, query, ctx, api_key):
        return self._papers


def test_a_filter_that_rejects_everything_says_so(capsys) -> None:
    """The property that would have surfaced this in the first run
    instead of in a downstream review months later."""
    cfg = SimpleNamespace(BLOCK_A_TERMS=["x"])
    rows = _StubS2([_paper(journal_name="Frontiers in Psychology")]).run(
        cfg, _ctx(),
    )
    assert rows == []
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "scope filter rejected all" in out


def test_no_warning_when_the_filter_keeps_something(capsys) -> None:
    cfg = SimpleNamespace(BLOCK_A_TERMS=["x"])
    rows = _StubS2([_paper()]).run(cfg, _ctx())
    assert len(rows) == 1
    assert "scope filter rejected all" not in capsys.readouterr().out


def test_no_warning_when_the_search_itself_found_nothing(capsys) -> None:
    """Nothing to diagnose — an empty query result is not a scope
    mismatch, and crying wolf here would train users to ignore it."""
    cfg = SimpleNamespace(BLOCK_A_TERMS=["x"])
    assert _StubS2([]).run(cfg, _ctx()) == []
    assert "scope filter rejected all" not in capsys.readouterr().out
