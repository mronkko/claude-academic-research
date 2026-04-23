"""Tests for predatory-flag tagging in `import_to_zotero._row_to_zotero_item`.

The search-to-Zotero import does a preflight against Beall's list and
tags flagged journals with `predatory:flag` so the screener sees the
warning in Zotero (see the `systematic-review` skill's tag-conventions
section). Tagging, not excluding — the author decides during full-text."""

from __future__ import annotations

from dataclasses import dataclass

import import_to_zotero
from sources import predatory


@dataclass
class _FakeResult:
    is_predatory: bool
    reason: str = ""
    source: str = ""


def test_non_predatory_row_does_not_add_predatory_flag(monkeypatch) -> None:
    monkeypatch.setattr(
        predatory, "check_predatory",
        lambda journal=None, issn=None: _FakeResult(is_predatory=False),
    )
    row = {
        "title": "A real paper",
        "authors": "Smith, J.",
        "source": "Journal of Management",
        "year": "2020",
        "doi": "10.1/x",
        "issn": "1234-5678",
        "query": "motivation",
        "abstract": "",
    }
    item = import_to_zotero._row_to_zotero_item(row, collection_key="COLL1")

    tag_values = {t["tag"] for t in item.get("tags", [])}
    assert "search:motivation" in tag_values
    assert "predatory:flag" not in tag_values


def test_predatory_row_gets_predatory_flag_tag(monkeypatch) -> None:
    """When check_predatory flags the journal, import adds a
    `predatory:flag` tag alongside the search tag. The item is NOT
    excluded — it still becomes a real journalArticle in Zotero."""
    monkeypatch.setattr(
        predatory, "check_predatory",
        lambda journal=None, issn=None: _FakeResult(
            is_predatory=True,
            reason="ISSN match on Beall's list",
            source="beall_issn",
        ),
    )
    row = {
        "title": "Pay-to-publish paper",
        "authors": "X, Y.",
        "source": "International Journal of Suspicious Research",
        "year": "2020",
        "doi": "10.1/y",
        "issn": "9999-9999",
        "query": "motivation",
        "abstract": "",
    }
    item = import_to_zotero._row_to_zotero_item(row, collection_key="COLL1")

    assert item["itemType"] == "journalArticle"
    tag_values = {t["tag"] for t in item.get("tags", [])}
    assert "predatory:flag" in tag_values
    assert "search:motivation" in tag_values


def test_predatory_check_handles_missing_source_and_issn(monkeypatch) -> None:
    """Some search results land with blank source / issn (data-quality
    gaps). Import shouldn't crash; it should just skip the predatory
    check for that row."""
    called = []

    def fake_check(journal=None, issn=None):
        called.append((journal, issn))
        return _FakeResult(is_predatory=False)

    monkeypatch.setattr(predatory, "check_predatory", fake_check)

    row = {
        "title": "Ghost paper",
        "authors": "",
        "source": "",
        "year": "",
        "doi": "",
        "issn": "",
        "query": "",
        "abstract": "",
    }
    item = import_to_zotero._row_to_zotero_item(row, collection_key=None)

    # check_predatory is called with None for both (per the dict.get(...) or None idiom).
    assert called == [(None, None)]
    # No tags at all (no query, no predatory flag).
    assert "tags" not in item
