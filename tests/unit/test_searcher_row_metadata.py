"""What each searcher carries out of its API response.

The four search APIs all return volume, issue (three of four), pages and
a document type. The searchers used to drop every one of them, so
`import_to_zotero.py` had to re-fetch from Crossref what the search had
already been told — and the fields it could not re-derive (a chapter's
page range on a DOI Crossref types poorly) simply never reached Zotero.
A live import produced items with a single-field author name and no
volume/issue/pages at all; these tests pin the fix at its source.

Two properties matter downstream and are asserted per source:

1. **`type` speaks Crossref's vocabulary** (`base.CROSSREF_TYPES`), so
   `import_to_zotero._CROSSREF_TYPE_TO_ZOTERO` maps it unchanged; an
   unrecognised native type becomes `""` ("ask the authority"), never a
   guessed `journal-article`.
2. **Scopus and WoS emit comma-format creators**, OpenAlex and Semantic
   Scholar emit display order — because that is what the APIs return.
   Comma-absence is the import's signal to reconstruct the record from
   its DOI, so mislabelling either way costs either wrong creators or
   an unnecessary HTTP request per record.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from searchers import (
    OpenAlexSearch,
    ScopusSearch,
    SemanticScholarSearch,
    WosSearch,
    empty_row,
)
from searchers.base import CROSSREF_TYPES, SEARCH_ROW_FIELDS

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_widened_row_carries_the_bibliographic_columns() -> None:
    for field in ("volume", "issue", "pages", "type"):
        assert field in SEARCH_ROW_FIELDS
        assert empty_row()[field] == ""


# ---------------------------------------------------------------------------
# OpenAlex
# ---------------------------------------------------------------------------

OPENALEX_WORK = {
    "id": "https://openalex.org/W123",
    "doi": "https://doi.org/10.1016/j.techfore.2020.120001",
    "title": "Anticipating the machine",
    "publication_year": 2020,
    "type": "article",
    "cited_by_count": 12,
    "biblio": {
        "volume": "158",
        "issue": "3",
        "first_page": "120001",
        "last_page": "120014",
    },
    "authorships": [
        {"author": {"display_name": "Jane Doe"}},
        {"author": {"display_name": "John Q. Public"}},
    ],
    "primary_location": {
        "source": {
            "display_name": "Technological Forecasting and Social Change",
            "issn_l": "0040-1625",
        },
    },
    "open_access": {"oa_status": "green", "oa_url": "https://example.org/x.pdf"},
}


def test_openalex_carries_biblio_fields() -> None:
    row = OpenAlexSearch()._work_to_row(OPENALEX_WORK, "block_a")
    assert row["volume"] == "158"
    assert row["issue"] == "3"
    assert row["pages"] == "120001-120014"


def test_openalex_article_normalises_to_crossref_journal_article() -> None:
    """OpenAlex renamed `journal-article` to `article` in 2024; the row
    must still speak Crossref, which `_CROSSREF_TYPE_TO_ZOTERO` reads."""
    row = OpenAlexSearch()._work_to_row(OPENALEX_WORK, "block_a")
    assert row["type"] == "journal-article"
    assert row["type"] in CROSSREF_TYPES


def test_openalex_book_chapter_keeps_its_type() -> None:
    work = {**OPENALEX_WORK, "type": "book-chapter"}
    assert OpenAlexSearch()._work_to_row(work, "b")["type"] == "book-chapter"


def test_openalex_preprint_maps_to_posted_content() -> None:
    work = {**OPENALEX_WORK, "type": "preprint"}
    assert OpenAlexSearch()._work_to_row(work, "b")["type"] == "posted-content"


def test_openalex_unknown_type_is_empty_not_guessed() -> None:
    """`""` sends the record down the identifier-fill path. Guessing
    `journal-article` would silently mistype it instead."""
    work = {**OPENALEX_WORK, "type": "peer-review"}
    assert OpenAlexSearch()._work_to_row(work, "b")["type"] == ""


def test_openalex_authors_stay_display_order() -> None:
    """Not a defect to fix here: OpenAlex's response has no given/family
    split to carry. The import reconstructs these from the DOI."""
    row = OpenAlexSearch()._work_to_row(OPENALEX_WORK, "block_a")
    assert row["authors"] == "Jane Doe; John Q. Public"


def test_openalex_lone_first_page_is_not_a_dangling_range() -> None:
    work = {**OPENALEX_WORK,
            "biblio": {"volume": "1", "issue": "", "first_page": "77",
                       "last_page": None}}
    assert OpenAlexSearch()._work_to_row(work, "b")["pages"] == "77"


def test_openalex_single_page_article_is_not_duplicated() -> None:
    work = {**OPENALEX_WORK,
            "biblio": {"first_page": "e12345", "last_page": "e12345"}}
    assert OpenAlexSearch()._work_to_row(work, "b")["pages"] == "e12345"


def test_openalex_missing_biblio_is_empty_strings() -> None:
    work = {k: v for k, v in OPENALEX_WORK.items() if k != "biblio"}
    row = OpenAlexSearch()._work_to_row(work, "b")
    assert (row["volume"], row["issue"], row["pages"]) == ("", "", "")


# ---------------------------------------------------------------------------
# Scopus
# ---------------------------------------------------------------------------


def _scopus_result(**overrides):
    """A pybliometrics `ScopusSearch` result, in the shape it arrives."""
    base = {
        "doi": "10.1016/J.TECHFORE.2020.120001",
        "title": "Anticipating the machine",
        "author_names": "Doe, Jane;Public, John Q.",
        "coverDate": "2020-11-01",
        "publicationName": "Technological Forecasting and Social Change",
        "issn": "00401625",
        "volume": "158",
        "issueIdentifier": "3",
        "pageRange": "120001-120014",
        "subtype": "ar",
        "subtypeDescription": "Article",
        "citedby_count": 12,
        "eid": "2-s2.0-85090000000",
        "description": "An abstract.",
    }
    return SimpleNamespace(**{**base, **overrides})


def test_scopus_carries_biblio_fields() -> None:
    row = ScopusSearch()._result_to_row(_scopus_result(), "q1")
    assert row["volume"] == "158"
    assert row["issue"] == "3"
    assert row["pages"] == "120001-120014"
    assert row["type"] == "journal-article"


def test_scopus_authors_are_comma_format() -> None:
    """The property the import relies on to build creators offline."""
    row = ScopusSearch()._result_to_row(_scopus_result(), "q1")
    assert row["authors"] == "Doe, Jane;Public, John Q."


@pytest.mark.parametrize(("subtype", "expected"), [
    ("ar", "journal-article"),
    ("re", "journal-article"),   # a review article is still an article
    ("cp", "proceedings-article"),
    ("ch", "book-chapter"),
    ("bk", "book"),
])
def test_scopus_subtype_codes_map_to_crossref(subtype, expected) -> None:
    row = ScopusSearch()._result_to_row(_scopus_result(subtype=subtype), "q")
    assert row["type"] == expected


def test_scopus_falls_back_to_the_description_when_no_code() -> None:
    row = ScopusSearch()._result_to_row(
        _scopus_result(subtype="", subtypeDescription="Book Chapter"), "q",
    )
    assert row["type"] == "book-chapter"


def test_scopus_unknown_subtype_is_empty() -> None:
    row = ScopusSearch()._result_to_row(
        _scopus_result(subtype="zz", subtypeDescription="Data Paper"), "q",
    )
    assert row["type"] == ""


def test_scopus_tolerates_missing_biblio_fields() -> None:
    row = ScopusSearch()._result_to_row(
        _scopus_result(volume=None, issueIdentifier=None, pageRange=None), "q",
    )
    assert (row["volume"], row["issue"], row["pages"]) == ("", "", "")


# ---------------------------------------------------------------------------
# Web of Science
# ---------------------------------------------------------------------------


def _wos_record(*, pub_info=None, doctypes="Article", names=None) -> dict:
    return {
        "UID": "WOS:000500000000001",
        "static_data": {
            "summary": {
                "titles": {"title": [
                    {"type": "item", "content": "Anticipating the machine"},
                    {"type": "source",
                     "content": "TECHNOLOGICAL FORECASTING AND SOCIAL CHANGE"},
                ]},
                "names": {"name": names if names is not None else [
                    {"role": "author", "display_name": "Doe, Jane",
                     "full_name": "Doe, Jane"},
                    {"role": "author", "display_name": "Public, John Q.",
                     "full_name": "Public, John Q."},
                ]},
                "pub_info": pub_info if pub_info is not None else {
                    "pubyear": 2020,
                    "vol": "158",
                    "issue": "3",
                    "page": {"begin": "120001", "end": "120014",
                             "page_count": 14, "content": "120001-120014"},
                },
                "doctypes": {"doctype": doctypes},
            },
            "fullrecord_metadata": {"abstracts": {}},
        },
        "dynamic_data": {
            "cluster_related": {"identifiers": {"identifier": [
                {"type": "doi", "value": "10.1016/j.techfore.2020.120001"},
                {"type": "issn", "value": "0040-1625"},
            ]}},
            "citation_related": {"tc_list": {"silo_tc": {"local_count": 12}}},
        },
    }


def test_wos_carries_biblio_fields() -> None:
    row = WosSearch()._extract_record(_wos_record(), "q1")
    assert row["volume"] == "158"
    assert row["issue"] == "3"
    assert row["pages"] == "120001-120014"
    assert row["type"] == "journal-article"


def test_wos_authors_are_comma_format() -> None:
    row = WosSearch()._extract_record(_wos_record(), "q1")
    assert row["authors"] == "Doe, Jane; Public, John Q."


def test_wos_page_dict_without_content_is_joined() -> None:
    rec = _wos_record(pub_info={"pubyear": 2020, "vol": "9", "issue": "1",
                                "page": {"begin": "10", "end": "25"}})
    assert WosSearch()._extract_record(rec, "q")["pages"] == "10-25"


def test_wos_single_page_is_not_a_range() -> None:
    rec = _wos_record(pub_info={"pubyear": 2020, "vol": "9",
                                "page": {"begin": "10", "end": "10"}})
    assert WosSearch()._extract_record(rec, "q")["pages"] == "10"


def test_wos_missing_pub_info_is_empty_strings() -> None:
    rec = _wos_record(pub_info={})
    row = WosSearch()._extract_record(rec, "q")
    assert (row["volume"], row["issue"], row["pages"]) == ("", "", "")


def test_wos_doctype_list_takes_the_first_mappable_entry() -> None:
    """A record is routinely both `Article` and `Early Access`."""
    rec = _wos_record(doctypes=["Early Access", "Article"])
    assert WosSearch()._extract_record(rec, "q")["type"] == "journal-article"


def test_wos_proceedings_paper_maps_to_proceedings_article() -> None:
    rec = _wos_record(doctypes="Proceedings Paper")
    assert WosSearch()._extract_record(rec, "q")["type"] == "proceedings-article"


def test_wos_unknown_doctype_is_empty() -> None:
    rec = _wos_record(doctypes=["Meeting Abstract"])
    assert WosSearch()._extract_record(rec, "q")["type"] == ""


def test_wos_falls_back_to_wos_standard_when_display_name_absent() -> None:
    """`Smith, JA` is a worse rendering than `Smith, John A.` but still
    splits at the comma — better than dropping the creator entirely."""
    rec = _wos_record(names=[{"role": "author", "wos_standard": "Doe, J"}])
    assert WosSearch()._extract_record(rec, "q")["authors"] == "Doe, J"


# ---------------------------------------------------------------------------
# Semantic Scholar
# ---------------------------------------------------------------------------

S2_PAPER = {
    "paperId": "abc123",
    "title": "Anticipating the machine",
    "abstract": "An abstract.",
    "year": 2020,
    "venue": "Technological Forecasting and Social Change",
    "authors": [{"name": "Jane Doe"}, {"name": "John Q. Public"}],
    "externalIds": {"DOI": "10.1016/j.techfore.2020.120001"},
    "citationCount": 12,
    "journal": {"name": "Technological Forecasting and Social Change",
                "volume": "158", "pages": "120001-120014"},
    "publicationTypes": ["JournalArticle", "Review"],
}


def test_semantic_scholar_carries_volume_and_pages() -> None:
    row = SemanticScholarSearch()._paper_to_row(S2_PAPER, "block_a")
    assert row["volume"] == "158"
    assert row["pages"] == "120001-120014"
    assert row["type"] == "journal-article"


def test_semantic_scholar_has_no_issue_number() -> None:
    """Documented gap, not an oversight: the Graph API's `journal`
    object has no issue field. The dedup merge fills it from another
    database when one is in the run."""
    assert SemanticScholarSearch()._paper_to_row(S2_PAPER, "b")["issue"] == ""


def test_semantic_scholar_authors_stay_display_order() -> None:
    row = SemanticScholarSearch()._paper_to_row(S2_PAPER, "b")
    assert row["authors"] == "Jane Doe; John Q. Public"


def test_semantic_scholar_unknown_publication_type_is_empty() -> None:
    paper = {**S2_PAPER, "publicationTypes": ["Dataset"]}
    assert SemanticScholarSearch()._paper_to_row(paper, "b")["type"] == ""


def test_semantic_scholar_missing_publication_types_is_empty() -> None:
    paper = {k: v for k, v in S2_PAPER.items() if k != "publicationTypes"}
    assert SemanticScholarSearch()._paper_to_row(paper, "b")["type"] == ""


def test_semantic_scholar_missing_journal_object_is_empty_strings() -> None:
    paper = {k: v for k, v in S2_PAPER.items() if k != "journal"}
    row = SemanticScholarSearch()._paper_to_row(paper, "b")
    assert (row["volume"], row["pages"]) == ("", "")


# ---------------------------------------------------------------------------
# Cross-source invariant
# ---------------------------------------------------------------------------


def test_every_source_emits_a_full_row() -> None:
    """Whatever a source knows, the row it returns has every column —
    `search.py` writes with a fixed DictWriter fieldnames list and would
    raise on a missing key."""
    rows = [
        OpenAlexSearch()._work_to_row(OPENALEX_WORK, "q"),
        ScopusSearch()._result_to_row(_scopus_result(), "q"),
        WosSearch()._extract_record(_wos_record(), "q"),
        SemanticScholarSearch()._paper_to_row(S2_PAPER, "q"),
    ]
    for row in rows:
        assert set(row) == set(SEARCH_ROW_FIELDS), row["db"]


def test_every_emitted_type_is_crossref_vocabulary() -> None:
    rows = [
        OpenAlexSearch()._work_to_row(OPENALEX_WORK, "q"),
        ScopusSearch()._result_to_row(_scopus_result(), "q"),
        WosSearch()._extract_record(_wos_record(), "q"),
        SemanticScholarSearch()._paper_to_row(S2_PAPER, "q"),
    ]
    for row in rows:
        assert row["type"] in CROSSREF_TYPES, f"{row['db']}: {row['type']!r}"
