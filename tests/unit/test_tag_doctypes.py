"""tag_doctypes.py: backfill doctype:* tags on an existing library.

Dry-run by default; tags only items with no doctype tag yet; relates
supplements to their article wherever it sits in the library.
"""

from __future__ import annotations

import sys

import pytest
import tag_doctypes
import zotero_io


def _item(key, title, *, doi="", tags=(), creators=()):
    return {"key": key, "data": {"key": key, "title": title, "DOI": doi,
                                 "creators": list(creators),
                                 "tags": [{"tag": t} for t in tags]}}


AUTHOR = [{"lastName": "Doe", "firstName": "J", "creatorType": "author"}]

LIBRARY = [
    _item("ART00001", "Reconciling female agentic advantage",
          doi="https://doi.org/10.1037/APL0000550", creators=AUTHOR),
    _item("SUPP0001", "Supplemental Material for Reconciling Female Agentic "
          "Advantage", doi="10.1037/apl0000550.supp"),
    _item("SUPP0002", "Supplemental Material for Something Else",
          doi="10.1037/apl0000999.supp"),  # parent not in library
    _item("EDB00001", "Editorial Board", doi="10.1016/s1048-9843(22)00006-6"),
    _item("ISS00001", "Issue Information", doi="10.1002/job.2525",
          tags=["doctype:front-matter"]),  # already tagged
    _item("PUB00001", "Publication Notice", doi="10.1177/10422587221120291"),
    _item("MAN00001", "Editorial Board", tags=["doctype:erratum"]),  # hand-set
]


def test_plan_tags_untagged_and_links_supplements_library_wide():
    to_tag, links = tag_doctypes.plan(LIBRARY, LIBRARY)
    assert to_tag == [("SUPP0001", "supplement"), ("SUPP0002", "supplement"),
                      ("EDB00001", "front-matter"), ("PUB00001", "notice")]
    assert links == [("SUPP0001", "ART00001")]


def test_plan_for_a_collection_still_finds_parents_outside_it():
    to_tag, links = tag_doctypes.plan([LIBRARY[1]], LIBRARY)
    assert to_tag == [("SUPP0001", "supplement")]
    assert links == [("SUPP0001", "ART00001")]


def test_an_already_tagged_supplement_is_still_linked():
    supp = _item("SUPP0001", "x", doi="10.1037/apl0000550.supp",
                 tags=["doctype:supplement"])
    to_tag, links = tag_doctypes.plan([supp], [LIBRARY[0], supp])
    assert to_tag == []
    assert links == [("SUPP0001", "ART00001")]


class _FakeZot:
    def __init__(self):
        self.tag_updates = None
        self.related: list[tuple] = []

    def describe_library(self):
        return "group 1 ('test')"

    def journal_articles(self):
        return LIBRARY

    def collection_items(self, _c, item_type="journalArticle"):
        return LIBRARY[:2]

    def batch_update_tags(self, updates):
        self.tag_updates = updates
        return {"applied": len(updates), "unchanged": 0, "failed": 0}

    def add_related(self, key, related, *, cloud=False):
        self.related.append((key, tuple(related)))
        return 1


@pytest.fixture
def fake(monkeypatch):
    zot = _FakeZot()
    monkeypatch.setattr(zotero_io.ZoteroClient, "from_args",
                        classmethod(lambda cls, args: zot))
    monkeypatch.setattr(sys, "argv", ["tag_doctypes.py"])
    return zot


def test_dry_run_writes_nothing(fake, capsys):
    assert tag_doctypes.main(["--group", "1"]) == 0
    assert fake.tag_updates is None and fake.related == []
    out = capsys.readouterr().out
    assert "Would tag 4 item(s): supplement 2, front-matter 1, notice 1" in out
    assert "Would relate 1 supplement(s)" in out
    assert "Dry run" in out


def test_apply_tags_globally_and_links_both_ways(fake):
    assert tag_doctypes.main(["--group", "1", "--apply"]) == 0
    assert ("EDB00001", {"add": ["doctype:front-matter"]}) in fake.tag_updates
    assert all(not op["add"][0].startswith(("/", "slr"))
               for _k, op in fake.tag_updates)
    assert fake.related == [("SUPP0001", ("ART00001",)),
                            ("ART00001", ("SUPP0001",))]


def test_no_link_tags_only(fake):
    assert tag_doctypes.main(["--group", "1", "--apply", "--no-link"]) == 0
    assert fake.tag_updates and fake.related == []
