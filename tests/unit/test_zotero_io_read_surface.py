"""Which items a read sees, and how many requests it costs.

Found by a live run that had just written ~1,700 items.

1. **`journal_articles()` returns only `journalArticle`.** A systematic
   review's included set routinely holds book chapters, reports and
   preprints, and those records need abstracts for the same reason
   articles do. Screening them out silently shrinks the frame instead of
   reporting a gap: on the live library 55 such items were never examined,
   and nothing said so.

2. **`items_by_keys` must not walk the library.** Cost has to track the
   request, not the library size.

(This file used to pin the read *surface* too — local vs Web API and the
`--remote` flag. The plugin now reads only Zotero Desktop's local API, so
those tests went with the Web API.)
"""

from __future__ import annotations

from zotero_io import ZoteroClient


def test_abstractable_types_cover_more_than_journal_articles():
    types = ZoteroClient.ABSTRACTABLE_ITEM_TYPES
    assert "journalArticle" in types
    for screened_type in ("bookSection", "book", "report", "preprint"):
        assert screened_type in types, (
            f"{screened_type} is screened by real reviews and can carry an "
            f"abstract; excluding it shrinks the frame silently"
        )


def test_abstractable_items_asks_for_a_type_union_in_one_sweep(monkeypatch):
    """One paginated request, not one per type."""
    calls = []

    class _FakeZot:
        def items(self, **kwargs):
            calls.append(kwargs)
            return []

        def everything(self, result):
            return result

    client = ZoteroClient.__new__(ZoteroClient)
    monkeypatch.setattr(
        ZoteroClient, "_client", lambda self: _FakeZot(),
    )

    client.abstractable_items()
    assert len(calls) == 1, "should be a single union query, not one per type"
    assert " || " in calls[0]["itemType"]
    assert "bookSection" in calls[0]["itemType"]


def test_abstractable_items_honours_an_explicit_type_list(monkeypatch):
    calls = []

    class _FakeZot:
        def items(self, **kwargs):
            calls.append(kwargs)
            return []

        def everything(self, result):
            return result

    client = ZoteroClient.__new__(ZoteroClient)
    monkeypatch.setattr(ZoteroClient, "_client", lambda self: _FakeZot())

    client.abstractable_items(["journalArticle"])
    assert calls[0]["itemType"] == "journalArticle", (
        "passing journalArticle must restore the old narrow behaviour"
    )


def test_items_by_keys_batches_and_does_not_walk_the_library(monkeypatch):
    """The whole point: cost tracks the request, not the library size.

    Walking ~10,000 items to arrive at a few hundred is what tripped
    Zotero's rate limiter on a live run, and because the walk happens
    before any retrieval, no amount of backoff got past it.
    """
    calls = []

    class _FakeZot:
        def items(self, **kwargs):
            calls.append(kwargs)
            return [{"key": k} for k in kwargs["itemKey"].split(",")]

        def everything(self, result):
            return result

        def top(self):
            raise AssertionError("must not enumerate the library")

    client = ZoteroClient.__new__(ZoteroClient)
    monkeypatch.setattr(ZoteroClient, "_client", lambda self: _FakeZot())

    keys = [f"KEY{i:05d}" for i in range(120)]
    got = client.items_by_keys(keys)

    assert len(got) == 120
    assert len(calls) == 3, "120 keys at a 50-key ceiling is three requests"
    assert all("itemType" not in c for c in calls)
    assert sum(len(c["itemKey"].split(",")) for c in calls) == 120


def test_items_by_keys_is_a_no_op_for_no_keys(monkeypatch):
    class _FakeZot:
        def items(self, **kwargs):
            raise AssertionError("should not issue a request for zero keys")

    client = ZoteroClient.__new__(ZoteroClient)
    monkeypatch.setattr(ZoteroClient, "_client", lambda self: _FakeZot())
    assert client.items_by_keys([]) == []
    assert client.items_by_keys(["", "  "]) == []
