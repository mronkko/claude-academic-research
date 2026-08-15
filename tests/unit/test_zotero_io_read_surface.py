"""Which items a read sees, and where it reads them from.

Two defaults in `ZoteroClient` are right most of the time and quietly
wrong in one situation each. Both were found by a live run that had just
written ~1,700 items through the Web API.

1. **Reads prefer the local Zotero client**, because it is far faster and
   unmetered. But items written through the Web API do not exist locally
   until Zotero Desktop next syncs. A script that adds items and reads
   them back — or one handed a `--filter-keys-file` of freshly created
   keys — sees nothing and reports zero items to process, which reads as
   "already done" rather than "cannot see them yet". `--remote` is the
   escape hatch, and it belongs on `add_library_args` so every pipeline
   entry point has it rather than one script at a time.

2. **`journal_articles()` returns only `journalArticle`.** A systematic
   review's included set routinely holds book chapters, reports and
   preprints, and those records need abstracts for the same reason
   articles do. Screening them out silently shrinks the frame instead of
   reporting a gap: on the live library 55 such items were never examined,
   and nothing said so.
"""

from __future__ import annotations

import argparse

from zotero_io import ZoteroClient, add_library_args


def _parse(argv):
    parser = argparse.ArgumentParser()
    add_library_args(parser)
    return parser.parse_args(argv)


def test_remote_flag_is_available_to_every_pipeline_script():
    assert _parse(["--user"]).remote is False
    assert _parse(["--user", "--remote"]).remote is True


def test_remote_is_not_mutually_exclusive_with_library_choice():
    """--group/--user say WHICH library; --remote says HOW to read it."""
    args = _parse(["--group", "12345", "--remote"])
    assert args.group == "12345"
    assert args.remote is True


def test_remote_overrides_a_callers_prefer_local_default(monkeypatch):
    """The flag beats the keyword, not the other way round.

    Whoever runs the script knows the desktop client is behind; a
    caller's compiled-in default cannot.
    """
    captured = {}

    def _fake_for_user_library(user_id, *, api_key, prefer_local, **kwargs):
        captured["prefer_local"] = prefer_local
        return "client"

    monkeypatch.setattr(
        ZoteroClient, "for_user_library",
        classmethod(lambda cls, *a, **kw: _fake_for_user_library(*a, **kw)),
    )
    monkeypatch.setattr(
        "core.config_loader.require", lambda *a, **kw: "stub",
    )

    ZoteroClient.from_args(_parse(["--user", "--remote"]), prefer_local=True)
    assert captured["prefer_local"] is False, (
        "--remote must win over a prefer_local=True default"
    )

    ZoteroClient.from_args(_parse(["--user"]), prefer_local=True)
    assert captured["prefer_local"] is True


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
        ZoteroClient, "_read_client", lambda self: _FakeZot(),
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
    monkeypatch.setattr(ZoteroClient, "_read_client", lambda self: _FakeZot())

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
    monkeypatch.setattr(ZoteroClient, "_read_client", lambda self: _FakeZot())

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
    monkeypatch.setattr(ZoteroClient, "_read_client", lambda self: _FakeZot())
    assert client.items_by_keys([]) == []
    assert client.items_by_keys(["", "  "]) == []
