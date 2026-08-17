"""What item type an imported search row becomes.

`_row_to_zotero_item` hard-coded `journalArticle` for every row, because
that is what a literature search mostly returns. Mostly is not always:
Scopus and WoS return book chapters, and a mis-typed chapter passes
`journal_articles()`, is routed to article-only PDF handlers and cannot
succeed there. Five of them in one live corpus each burned a browser
slot to produce an unexplained "never reached the viewer".

The five real DOIs are used here as fixtures. Their correct types were
confirmed against Crossref in a live Zotero group: four chapters and —
the one worth keeping in the suite — a whole *book* among them, so a
rule of "not an article means chapter" would be wrong too.
"""

from __future__ import annotations

import import_to_zotero as imp
import pytest

# Real DOIs, with the type Crossref actually returns for each.
LIVE_CASES = [
    ("10.1017/cbo9780511845680.012", "book-chapter", "bookSection"),
    ("10.1163/ej.9789004180192.i-356.48", "book-chapter", "bookSection"),
    ("10.4324/9780203166048-7", "book-chapter", "bookSection"),
    ("10.5149/northcarolina/9781469661032.001.0001", "monograph", "book"),
    ("10.5876/9781607320395.c008", "book-chapter", "bookSection"),
]


class _Session:
    """Stand-in for the shared requests session."""


def _crossref(monkeypatch, type_by_doi: dict, *, calls: list | None = None):
    def fake_get_json(session, url, **kw):
        if calls is not None:
            calls.append(url)
        doi = url.rsplit("/works/", 1)[-1]
        if doi not in type_by_doi:
            return None
        return {"message": {"type": type_by_doi[doi]}}

    monkeypatch.setattr(imp.http_client, "get_json", fake_get_json)


@pytest.mark.parametrize(("doi", "crossref_type", "expected"), LIVE_CASES)
def test_live_dois_resolve_to_their_real_types(
    monkeypatch, doi, crossref_type, expected,
) -> None:
    _crossref(monkeypatch, {doi: crossref_type})
    assert imp.resolve_item_type(doi, session=_Session()) == expected


def test_a_book_is_not_flattened_to_a_chapter(monkeypatch) -> None:
    """The case a "chapters vs articles" rule would get wrong."""
    doi = "10.5149/northcarolina/9781469661032.001.0001"
    _crossref(monkeypatch, {doi: "monograph"})
    assert imp.resolve_item_type(doi, session=_Session()) == "book"


class TestFallbacks:
    """Every unknown must land on the old behaviour, never on an error."""

    def test_missing_doi(self) -> None:
        assert imp.resolve_item_type("", session=_Session()) == "journalArticle"

    def test_unknown_crossref_type(self, monkeypatch) -> None:
        _crossref(monkeypatch, {"10.1/x": "some-new-thing"})
        assert imp.resolve_item_type("10.1/x", session=_Session()) == "journalArticle"

    def test_doi_not_in_crossref(self, monkeypatch) -> None:
        _crossref(monkeypatch, {})
        assert imp.resolve_item_type("10.1/x", session=_Session()) == "journalArticle"

    def test_network_failure_does_not_fail_the_import(self, monkeypatch) -> None:
        def boom(*a, **kw):
            raise RuntimeError("crossref down")

        monkeypatch.setattr(imp.http_client, "get_json", boom)
        assert imp.resolve_item_type("10.1/x", session=_Session()) == "journalArticle"


def test_one_lookup_per_distinct_doi(monkeypatch) -> None:
    """A merged Scopus+WoS batch sees most DOIs twice."""
    calls: list = []
    _crossref(monkeypatch, {"10.1/x": "journal-article"}, calls=calls)
    cache: dict = {}
    for _ in range(4):
        imp.resolve_item_type("10.1/X", session=_Session(), cache=cache)
    assert len(calls) == 1, "Crossref was asked more than once for one DOI"


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
