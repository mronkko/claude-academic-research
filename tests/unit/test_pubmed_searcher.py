"""The PubMed searcher: query construction, XML parsing, config gating.

No network. `parse_articles` is fed a trimmed efetch response so the
row mapping — structured abstracts, `MedlineDate` years, DOI location,
collective authors — is pinned without calling NCBI.
"""

from __future__ import annotations

from types import SimpleNamespace

from searchers import ALL_SOURCE_CLASSES, PubMedSearch, searchers_by_name
from searchers.base import CROSSREF_TYPES, SEARCH_ROW_FIELDS, SearchContext

EFETCH_XML = """<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>111</PMID>
      <Article>
        <Journal>
          <ISSN>0022-3913</ISSN>
          <JournalIssue>
            <Volume>134</Volume><Issue>2</Issue>
            <PubDate><Year>2025</Year></PubDate>
          </JournalIssue>
          <Title>The Journal of prosthetic dentistry</Title>
        </Journal>
        <ArticleTitle>Intraoral scanners for <i>auricular</i> prostheses.</ArticleTitle>
        <Pagination><MedlinePgn>513-519</MedlinePgn></Pagination>
        <ELocationID EIdType="doi" ValidYN="Y">10.1016/J.PROSDENT.2023.09.028</ELocationID>
        <Abstract>
          <AbstractText Label="PURPOSE">Compare scanners.</AbstractText>
          <AbstractText Label="RESULTS">Trueness differed.</AbstractText>
        </Abstract>
        <AuthorList>
          <Author><LastName>Gadallah</LastName><ForeName>Maged Ahmed</ForeName></Author>
          <Author><CollectiveName>Scan Study Group</CollectiveName></Author>
        </AuthorList>
        <PublicationTypeList>
          <PublicationType>Journal Article</PublicationType>
        </PublicationTypeList>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>222</PMID>
      <Article>
        <Journal>
          <JournalIssue>
            <PubDate><MedlineDate>2019 Jul-Aug</MedlineDate></PubDate>
          </JournalIssue>
          <Title>Some Journal</Title>
        </Journal>
        <ArticleTitle>No DOI here</ArticleTitle>
        <AuthorList>
          <Author><LastName>Roe</LastName><Initials>J</Initials></Author>
        </AuthorList>
      </Article>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList><ArticleId IdType="pubmed">222</ArticleId></ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""


def test_pubmed_is_registered() -> None:
    assert PubMedSearch in ALL_SOURCE_CLASSES
    assert "pubmed" in searchers_by_name()


def test_full_query_wraps_core_before_adding_the_year_window() -> None:
    ctx = SearchContext(from_year=2016, to_year=2026, issns=[])
    q = PubMedSearch.full_query("a OR b", ctx)
    assert q.startswith("(a OR b) AND ")
    assert '"2016/01/01"[Date - Publication]' in q
    assert '"2026/12/31"[Date - Publication]' in q


def test_row_mapping() -> None:
    rows = PubMedSearch().parse_articles(EFETCH_XML)
    assert len(rows) == 2
    first = rows[0]
    assert set(first) == set(SEARCH_ROW_FIELDS)
    assert first["db"] == "pubmed"
    assert first["doi"] == "10.1016/j.prosdent.2023.09.028"
    assert first["title"] == "Intraoral scanners for auricular prostheses."
    assert first["authors"] == "Gadallah, Maged Ahmed; Scan Study Group"
    assert first["year"] == "2025"
    assert first["source"] == "The Journal of prosthetic dentistry"
    assert first["issn"] == "0022-3913"
    assert (first["volume"], first["issue"], first["pages"]) == ("134", "2", "513-519")
    assert first["abstract"] == "PURPOSE: Compare scanners. RESULTS: Trueness differed."
    assert first["type"] in CROSSREF_TYPES


def test_missing_doi_and_medline_date_year() -> None:
    second = PubMedSearch().parse_articles(EFETCH_XML)[1]
    assert second["doi"] == ""
    assert second["year"] == "2019"
    assert second["authors"] == "Roe, J"
    assert second["type"] == "journal-article"


def test_search_needs_pubmed_query_defs() -> None:
    import search

    assert "PUBMED_QUERY_DEFS" in search._keyword_skip_reason(
        "pubmed", SimpleNamespace())
    assert search._keyword_skip_reason(
        "pubmed", SimpleNamespace(PUBMED_QUERY_DEFS=[("x", "y")])) == ""
