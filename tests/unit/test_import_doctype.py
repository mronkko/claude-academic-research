"""import_to_zotero keeps supplements / front matter / notices and tags them.

The tag is global (`doctype:*`), like `predatory:flag`: it states a fact
about the record, the same for every review sharing the library. A
supplement whose article is in the batch or library is related to it in
both directions.
"""

from __future__ import annotations

import doctype
import import_to_zotero as imp
import pytest
import zotero_io
from sources import predatory
from zotero_io import ZoteroClient

NS = "test-review/"


@pytest.fixture(autouse=True)
def _no_predatory(monkeypatch):
    class _R:
        is_predatory = False
    monkeypatch.setattr(predatory, "check_predatory",
                        lambda journal=None, issn=None: _R())


def _row(**kw):
    row = {"title": "", "authors": "", "source": "Journal of Applied "
           "Psychology", "year": "2022", "doi": "", "issn": "0021-9010",
           "query": "q1", "abstract": "", "type": "journal-article"}
    row.update(kw)
    return row


def _tags(item):
    return {t["tag"] for t in item.get("tags", [])}


def test_supplement_row_is_built_and_tagged_globally():
    item = imp._row_to_zotero_item(_row(
        title="Supplemental Material for Reconciling Female Agentic Advantage",
        doi="10.1037/apl0000550.supp",
    ), "COLL1", ns=NS)
    assert item["itemType"] == "journalArticle"
    assert "doctype:supplement" in _tags(item)
    assert f"{NS}doctype:supplement" not in _tags(item)
    assert f"{NS}search:q1" in _tags(item)


@pytest.mark.parametrize(("title", "doi", "expected"), [
    ("Editorial Board", "10.1016/s1048-9843(22)00006-6", "doctype:front-matter"),
    ("Issue Information", "10.1002/job.2525", "doctype:front-matter"),
    ("Publication Notice", "10.1177/10422587221120291", "doctype:notice"),
])
def test_front_matter_and_notices_are_tagged(title, doi, expected):
    item = imp._row_to_zotero_item(_row(title=title, doi=doi), None, ns=NS)
    assert expected in _tags(item)


def test_ordinary_article_gets_no_doctype_tag():
    item = imp._row_to_zotero_item(_row(
        title="Reconciling female agentic advantage and disadvantage",
        authors="Doe, Jane", doi="10.1037/apl0000550",
    ), None, ns=NS)
    assert not doctype.from_tags(item["tags"])


def test_row_doctype_reads_authors_from_the_built_item():
    row = _row(title="Call for papers on entrepreneurship")
    assert imp.row_doctype(row) == "front-matter"
    item = {"creators": [{"lastName": "Doe", "firstName": "J",
                          "creatorType": "author"}]}
    assert imp.row_doctype(row, item) == ""


# --- existing items get the tag on patch --------------------------------

class _PatchZot:
    def __init__(self, items):
        self._items = items
        self.patches: list[dict] = []

    def journal_articles(self):
        return self._items

    def update_fields(self, key, fields):
        self.patches.append({"key": key, **fields})


def test_existing_item_is_patched_with_a_missing_doctype_tag(monkeypatch):
    monkeypatch.setattr(imp.time, "sleep", lambda _s: None)
    zot = _PatchZot([
        {"key": "SUPP0001", "version": 7, "data": {
            "abstractNote": "x", "tags": [{"tag": "other"}]}},
        {"key": "SUPP0002", "version": 3, "data": {
            "abstractNote": "x", "tags": [{"tag": "doctype:supplement"}]}},
    ])
    failed = imp._patch_existing_items(
        [("SUPP0001", "", "supplement"), ("SUPP0002", "", "supplement"),
         ("SUPP0001", "", "")],  # second row for the same item: one patch
        zot, None,
    )
    assert failed == 0
    assert zot.patches == [{
        "key": "SUPP0001",
        "tags": [{"tag": "other"}, {"tag": "doctype:supplement", "type": 1}],
    }]


# --- created keys are recorded by position -----------------------------

def test_create_records_key_by_position_even_when_one_fails(monkeypatch):
    monkeypatch.setattr(imp.time, "sleep", lambda _s: None)
    monkeypatch.setattr(imp, "BATCH_SIZE", 2)
    bodies = [
        {"success": {"0": "AAAA0001"}, "failed": {"1": {"message": "bad"}}},
        {"success": {"0": "AAAA0003"}},
    ]
    monkeypatch.setattr(zotero_io, "_post_items",
                        lambda _client, _payloads: bodies.pop(0))
    zot = ZoteroClient.__new__(ZoteroClient)
    zot.library_type, zot.group_id = "group", "1"
    zot._local = object()  # never reached: `_post_items` is stubbed
    items = [{"itemType": "journalArticle", "title": t} for t in "abc"]
    key_by_index: dict[int, str] = {}
    created, failed, keys = imp._create_new_items(
        items, zot, key_by_index=key_by_index,
    )
    assert (created, failed) == (2, 1)
    assert key_by_index == {0: "AAAA0001", 2: "AAAA0003"}
