"""Forward citation search — the "Stream B" of a two-stream protocol.

A keyword search scoped to a journal list cannot find a paper that
applies a method without using the review's topic vocabulary, and no
amount of term tuning fixes that: the paper simply does not contain the
words. What it does contain is a citation to the work that introduced
the method. Listing everything citing that work is a different retrieval
operation, and a standard one — forward snowballing.

Two properties separate it from the keyword stream and both are pinned
here:

- **No journal or ISSN restriction.** Escaping venue scope is the point;
  a method travels outside the journals a protocol lists.
- **Reported separately.** PRISMA counts a citation search under "other
  sources", never inside the database totals, so `discovery_source`
  rides on every row and survives dedup — with an overlap attributed to
  the database search, since the citation stream is credited for what it
  *adds*.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import search as search_mod
from searchers import (
    DISCOVERY_CITATION,
    DISCOVERY_KEYWORD,
    SEARCH_ROW_FIELDS,
    OpenAlexSearch,
    ScopusSearch,
    SemanticScholarSearch,
    WosSearch,
    empty_row,
    searchers_by_name,
)
from searchers.base import SearchContext, SearchSource

SEED = "10.1037/0021-9010.91.4.917"


def _ctx(**kw) -> SearchContext:
    return SearchContext(
        from_year=kw.get("from_year", 2000),
        to_year=kw.get("to_year", 2026),
        issns=kw.get("issns", ["0021-9010"]),
    )


# ---------------------------------------------------------------------------
# Schema and contract
# ---------------------------------------------------------------------------


def test_discovery_source_is_part_of_the_row_schema() -> None:
    assert "discovery_source" in SEARCH_ROW_FIELDS


def test_a_plain_row_is_a_keyword_hit() -> None:
    """Every source that predates citation search keeps its meaning
    without being touched."""
    assert empty_row()["discovery_source"] == DISCOVERY_KEYWORD


def test_discovery_source_is_appended_not_inserted() -> None:
    """Readers use DictReader, but a column added mid-schema still
    shifts every CSV a human has already eyeballed."""
    assert SEARCH_ROW_FIELDS[:4] == ("db", "query", "doi", "title")
    assert SEARCH_ROW_FIELDS[-1] == "discovery_source"


def test_sources_declare_whether_they_can_do_citation_search() -> None:
    assert OpenAlexSearch.supports_citation_search
    assert SemanticScholarSearch.supports_citation_search
    # Scopus needs an EID rather than a DOI for REFEID(), and the WoS
    # Starter tier exposes no cited-reference endpoint. Declared false so
    # the orchestrator skips them with a message instead of failing.
    assert not ScopusSearch.supports_citation_search
    assert not WosSearch.supports_citation_search


def test_a_source_that_does_not_support_it_refuses_loudly() -> None:
    class Bare(SearchSource):
        name = "bare"

        def run(self, config, ctx):
            return []

    with pytest.raises(NotImplementedError):
        Bare().run_citations([SEED], _ctx())


# ---------------------------------------------------------------------------
# OpenAlex
# ---------------------------------------------------------------------------

OPENALEX_CITING_WORK = {
    "id": "https://openalex.org/W999",
    "doi": "https://doi.org/10.1016/j.example.2015.01.001",
    "title": "Applying polynomial regression to fit indices",
    "publication_year": 2015,
    "type": "article",
    "cited_by_count": 4,
    "biblio": {"volume": "12", "issue": "2", "first_page": "1"},
    "authorships": [{"author": {"display_name": "Ada Lovelace"}}],
    "primary_location": {"source": {"display_name": "Some Other Journal",
                                    "issn_l": "9999-9999"}},
}


class _FakeOpenAlex(OpenAlexSearch):
    """Records the filters it was asked for; serves canned pages."""

    def __init__(self, work_id="W123", pages=None):
        super().__init__()
        self._work_id = work_id
        self._pages = pages if pages is not None else [
            {"results": [OPENALEX_CITING_WORK], "meta": {"next_cursor": None}},
        ]
        self.filters: list[str] = []
        self.cursors: list[str] = []

    def _resolve_work_id(self, doi, ctx):
        self.resolved = doi
        return self._work_id

    def _fetch_page_cursor(self, filter_str, cursor, ctx):
        self.filters.append(filter_str)
        self.cursors.append(cursor)
        return self._pages[len(self.filters) - 1]


def test_openalex_citation_rows_are_marked_as_citation_hits() -> None:
    rows = _FakeOpenAlex().run_citations([SEED], _ctx())
    assert len(rows) == 1
    assert rows[0]["discovery_source"] == DISCOVERY_CITATION
    assert rows[0]["doi"] == "10.1016/j.example.2015.01.001"


def test_openalex_citation_query_names_the_seed_in_the_query_column() -> None:
    """The provenance a PRISMA flow diagram needs: which seed found it."""
    rows = _FakeOpenAlex().run_citations([SEED], _ctx())
    assert rows[0]["query"] == f"cites:{SEED}"


def test_openalex_citation_filter_has_no_issn_restriction() -> None:
    """The whole point of the stream. A citing paper in a journal the
    protocol never listed is exactly what it exists to find."""
    src = _FakeOpenAlex()
    src.run_citations([SEED], _ctx(issns=["0021-9010", "0001-4273"]))
    assert "issn" not in src.filters[0]
    assert "primary_location.source" not in src.filters[0]


def test_openalex_citation_filter_keeps_the_year_window() -> None:
    src = _FakeOpenAlex()
    src.run_citations([SEED], _ctx(from_year=2007, to_year=2020))
    assert "publication_year:2007-2020" in src.filters[0]


def test_openalex_citation_filter_cites_the_resolved_work_id() -> None:
    src = _FakeOpenAlex(work_id="W2075867231")
    src.run_citations([SEED], _ctx())
    assert "cites:W2075867231" in src.filters[0]


def test_openalex_pages_with_a_cursor_past_the_ten_thousand_ceiling() -> None:
    """Page-number paging stops at 10,000 results. A seminal method paper
    — the kind named as a seed precisely because everything applying the
    method cites it — can have more citing works than that, and returning
    the first 10,000 would understate the search while looking complete."""
    second = {**OPENALEX_CITING_WORK, "id": "https://openalex.org/W1000",
              "doi": "https://doi.org/10.1016/j.example.2016.01.001"}
    src = _FakeOpenAlex(pages=[
        {"results": [OPENALEX_CITING_WORK], "meta": {"next_cursor": "c2"}},
        {"results": [second], "meta": {"next_cursor": None}},
    ])
    rows = src.run_citations([SEED], _ctx())
    assert len(rows) == 2
    assert src.cursors == ["*", "c2"]


def test_openalex_reports_an_unresolvable_seed_without_failing(capsys) -> None:
    """One seed OpenAlex has never indexed should not sink a run that has
    other seeds and a whole keyword stream behind it."""
    src = _FakeOpenAlex(work_id="")
    assert src.run_citations([SEED], _ctx()) == []
    assert "seed not found" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Semantic Scholar
# ---------------------------------------------------------------------------

S2_CITING_PAPER = {
    "paperId": "abc123",
    "title": "A study that applies the method",
    "year": 2015,
    "abstract": "We use polynomial regression.",
    "externalIds": {"DOI": "10.1016/J.EXAMPLE.2015.01.001"},
    "authors": [{"name": "Grace Hopper"}],
    "journal": {"name": "Another Journal", "volume": "7", "pages": "1-20"},
    "publicationTypes": ["JournalArticle"],
    "citationCount": 3,
}


class _FakeS2(SemanticScholarSearch):
    def __init__(self, papers=None):
        super().__init__()
        self._papers = [S2_CITING_PAPER] if papers is None else papers

    def _fetch_citations(self, doi, ctx, api_key):
        self.requested = doi
        return self._papers


def test_s2_citation_rows_are_marked_and_labelled() -> None:
    rows = _FakeS2().run_citations([SEED], _ctx())
    assert len(rows) == 1
    assert rows[0]["discovery_source"] == DISCOVERY_CITATION
    assert rows[0]["query"] == f"cites:{SEED}"


def test_s2_citation_rows_are_not_issn_filtered() -> None:
    """The keyword stream post-filters S2 against `ctx.issns` client-side
    because S2 cannot do it server-side. The citation stream must not:
    the citing paper is in a journal nobody listed, by construction."""
    rows = _FakeS2().run_citations([SEED], _ctx(issns=["0021-9010"]))
    assert len(rows) == 1
    assert rows[0]["source"] == "Another Journal"


def test_s2_citation_applies_the_year_window_client_side() -> None:
    """`/citations` takes no year parameter, so the bound is ours to
    enforce or silently lose."""
    old = {**S2_CITING_PAPER, "year": 1999, "paperId": "old"}
    rows = _FakeS2(papers=[S2_CITING_PAPER, old]).run_citations(
        [SEED], _ctx(from_year=2000, to_year=2026),
    )
    assert [r["s2_paper_id"] for r in rows] == ["abc123"]


def test_s2_keeps_a_citing_paper_with_no_year() -> None:
    """S2 leaves `year` null on records it has not fully resolved.
    Dropping those would narrow the stream on a metadata gap rather than
    on the protocol's dates."""
    undated = {**S2_CITING_PAPER, "year": None, "paperId": "undated"}
    rows = _FakeS2(papers=[undated]).run_citations([SEED], _ctx())
    assert [r["s2_paper_id"] for r in rows] == ["undated"]


# ---------------------------------------------------------------------------
# Orchestration and PRISMA accounting
# ---------------------------------------------------------------------------


def _row(doi: str, discovery: str, **kw) -> dict:
    row = empty_row()
    row.update({"doi": doi, "title": kw.get("title", doi), "db": "openalex"})
    row["discovery_source"] = discovery
    row.update(kw)
    return row


def test_dedup_attributes_an_overlap_to_the_database_search() -> None:
    """A record both streams found is a database hit. Crediting it to the
    citation stream would inflate "other sources" and understate the
    databases — the two numbers a PRISMA flow diagram reports."""
    deduped, _ = search_mod._dedup([
        _row("10.1/x", DISCOVERY_KEYWORD),
        _row("10.1/x", DISCOVERY_CITATION),
    ])
    assert len(deduped) == 1
    assert deduped[0]["discovery_source"] == DISCOVERY_KEYWORD


def test_dedup_attribution_does_not_depend_on_arrival_order() -> None:
    """`--databases` and `--streams` both change which row lands first."""
    deduped, _ = search_mod._dedup([
        _row("10.1/x", DISCOVERY_CITATION),
        _row("10.1/x", DISCOVERY_KEYWORD),
    ])
    assert deduped[0]["discovery_source"] == DISCOVERY_KEYWORD


def test_dedup_leaves_a_citation_only_record_credited_to_the_stream() -> None:
    """The records the stream exists to add."""
    deduped, _ = search_mod._dedup([
        _row("10.1/x", DISCOVERY_KEYWORD),
        _row("10.1/y", DISCOVERY_CITATION),
    ])
    by_doi = {r["doi"]: r["discovery_source"] for r in deduped}
    assert by_doi == {"10.1/x": DISCOVERY_KEYWORD, "10.1/y": DISCOVERY_CITATION}


def test_citation_stream_is_skipped_for_a_source_that_cannot_do_it(
    capsys,
) -> None:
    rows = search_mod._run_citation_stream(
        searchers_by_name()["scopus"], [SEED], _ctx(),
    )
    assert rows == []
    assert "cannot list citing works" in capsys.readouterr().out


def test_keyword_stream_still_runs_when_the_config_has_only_blocks() -> None:
    """A config with block terms and no QUERY_DEFS is a valid
    OpenAlex/S2 run; adding a second stream must not change that."""
    cfg = SimpleNamespace(BLOCK_A_TERMS=["x"])
    called: list[str] = []

    class Src(SearchSource):
        name = "openalex"

        def run(self, config, ctx):
            called.append("run")
            return []

    assert search_mod._run_keyword_stream(Src(), cfg, _ctx()) == []
    assert called == ["run"]
