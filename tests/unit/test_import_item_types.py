"""What item type an imported search row becomes.

`_row_to_zotero_item` hard-coded `journalArticle` for every row, because
that is what a literature search mostly returns. Mostly is not always:
Scopus and WoS return book chapters, and a mis-typed chapter passes
`journal_articles()`, is routed to article-only PDF handlers and cannot
succeed there. Five of them in one live corpus each burned a browser
slot to produce an unexplained "never reached the viewer".

The type used to be bought from Crossref, one HTTP request per DOI, on
every single row. It now comes from the row's own `type` column, which
the searchers fill from what the database already told them — and only
a row that cannot say goes to the authority. The five real DOIs are
still the fixtures: their Crossref types are exactly the strings the
searchers now emit, so the same table is under test from the other end.
"""

from __future__ import annotations

import import_to_zotero as imp
import pytest

# Real DOIs, with the type Crossref returns for each — which is also
# the value a searcher now writes into the row's `type` column.
LIVE_CASES = [
    ("10.1017/cbo9780511845680.012", "book-chapter", "bookSection"),
    ("10.1163/ej.9789004180192.i-356.48", "book-chapter", "bookSection"),
    ("10.4324/9780203166048-7", "book-chapter", "bookSection"),
    ("10.5149/northcarolina/9781469661032.001.0001", "monograph", "book"),
    ("10.5876/9781607320395.c008", "book-chapter", "bookSection"),
]


@pytest.mark.parametrize(("doi", "crossref_type", "expected"), LIVE_CASES)
def test_live_dois_resolve_to_their_real_types(
    doi, crossref_type, expected,
) -> None:
    row = {"doi": doi, "type": crossref_type}
    assert imp.row_item_type(row) == expected


def test_a_book_is_not_flattened_to_a_chapter() -> None:
    """The case a "chapters vs articles" rule would get wrong."""
    assert imp.row_item_type({"type": "monograph"}) == "book"


def test_type_matching_is_case_and_space_insensitive() -> None:
    assert imp.row_item_type({"type": "  Journal-Article "}) == "journalArticle"


class TestUnknowns:
    """An unknown type must say so, not guess.

    `""` is the routing signal for "ask Crossref". Returning
    `journalArticle` here — the old fallback — would send a book
    chapter to Zotero labelled an article without anything to notice it.
    """

    def test_missing_type_column(self) -> None:
        assert imp.row_item_type({"doi": "10.1/x"}) == ""

    def test_empty_type_column(self) -> None:
        assert imp.row_item_type({"type": ""}) == ""

    def test_unmapped_crossref_type(self) -> None:
        assert imp.row_item_type({"type": "some-new-thing"}) == ""


class TestContainerField:
    """Where the row's `source` column lands, per type.

    Silent failure mode: `_filter_valid_fields` drops fields the type
    does not declare, so leaving a book chapter's container in
    `publicationTitle` loses the book title with only a per-row warning.
    """

    ROW = {
        "title": "A chapter", "authors": "Doe, Jane", "year": "2012",
        "doi": "10.1/x", "issn": "", "abstract": "",
        "source": "Some Edited Volume",
    }

    def test_chapter_source_becomes_book_title(self) -> None:
        item = imp._row_to_zotero_item(self.ROW, None, "bookSection")
        assert item["bookTitle"] == "Some Edited Volume"
        assert "publicationTitle" not in item

    def test_article_source_stays_publication_title(self) -> None:
        item = imp._row_to_zotero_item(self.ROW, None, "journalArticle")
        assert item["publicationTitle"] == "Some Edited Volume"

    def test_conference_source_becomes_proceedings_title(self) -> None:
        item = imp._row_to_zotero_item(self.ROW, None, "conferencePaper")
        assert item["proceedingsTitle"] == "Some Edited Volume"

    def test_a_book_gets_no_container_field(self) -> None:
        """A book's source is the book — already in `title`."""
        item = imp._row_to_zotero_item(self.ROW, None, "book")
        assert not any(f in item for f in imp._CONTAINER_FIELD.values())
        assert item["title"] == "A chapter"

    def test_container_survives_the_valid_field_filter(self) -> None:
        """The end-to-end shape: type set, container kept, no loss."""
        item = imp._row_to_zotero_item(self.ROW, None, "bookSection")
        filtered, rejected = imp._filter_valid_fields(item)
        assert filtered["itemType"] == "bookSection"
        assert filtered.get("bookTitle") == "Some Edited Volume"
        assert "bookTitle" not in rejected

    def test_default_is_still_a_journal_article(self) -> None:
        item = imp._row_to_zotero_item(self.ROW, None)
        assert item["itemType"] == "journalArticle"
