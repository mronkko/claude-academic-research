"""Which collection an import writes to, and which backend it reads.

Two failures from one live run, both of which the agent driving it had
to work around by hand:

1. The skill documents `--collection <name>` and promises the collection
   is created if absent. The script accepted only an 8-character key, so
   the name went to the API as one and **every item in the batch** came
   back 400. The agent then wrote inline Python against `zotero_io` to
   list and create the collection, and re-ran the import.

2. That re-run read the library through the *local* Zotero client while
   writing through the *cloud* API. Zotero Desktop had not synced yet,
   so the dedup pre-check saw an empty library and created all 31 items
   a second time. PDF enrichment then downloaded 62 files and the PRISMA
   count guard failed downstream.
"""

from __future__ import annotations

import argparse

import import_to_zotero as imp
import pytest
from zotero_io import ZoteroClient

# ---------------------------------------------------------------------------
# find_collection
# ---------------------------------------------------------------------------


class _FakeCloud:
    def __init__(self, collections, *, created_key="NEWKEY01"):
        self._collections = collections
        self.created: list[str] = []
        self._created_key = created_key

    def collections(self):
        return self._collections

    def everything(self, result):
        return result

    def create_collections(self, payload):
        self.created.append(payload[0]["name"])
        return {"success": {"0": self._created_key}}


def _client(collections, **kw) -> tuple[ZoteroClient, _FakeCloud]:
    client = ZoteroClient.__new__(ZoteroClient)
    cloud = _FakeCloud(collections, **kw)
    client._cloud = cloud
    # `describe_library()` names the library in the not-found error.
    client.library_type = "user"
    client.group_id = "5591"
    return client, cloud


def _collection(key: str, name: str) -> dict:
    return {"key": key, "data": {"key": key, "name": name}}


def test_an_existing_key_is_used_as_is() -> None:
    client, cloud = _client([_collection("BSEJHPJN", "AI SLR")])
    assert client.find_collection("BSEJHPJN") == ("BSEJHPJN", "key")
    assert cloud.created == []


def test_a_name_resolves_to_its_key() -> None:
    client, cloud = _client([_collection("BSEJHPJN", "AI SLR")])
    assert client.find_collection("AI SLR") == ("BSEJHPJN", "name")
    assert cloud.created == []


def test_a_missing_name_is_created() -> None:
    """What the skill has promised all along."""
    client, cloud = _client([], created_key="CREATED1")
    assert client.find_collection("AI_ETP_SR", create=True) == (
        "CREATED1", "created",
    )
    assert cloud.created == ["AI_ETP_SR"]


def test_a_missing_name_without_create_is_an_error() -> None:
    client, _ = _client([_collection("BSEJHPJN", "Other")])
    with pytest.raises(ValueError, match="No collection named"):
        client.find_collection("AI SLR", create=False)


def test_an_ambiguous_name_is_never_guessed() -> None:
    client, _ = _client([
        _collection("AAAAAAAA", "Screening"),
        _collection("BBBBBBBB", "Screening"),
    ])
    with pytest.raises(ValueError, match="Ambiguous"):
        client.find_collection("Screening", create=True)


def test_a_key_shaped_string_that_is_not_a_key_falls_through_to_name() -> None:
    """`AI_ETP_SR` is 9 characters so it never looked like a key, but an
    8-character all-caps *name* must still resolve as a name rather than
    404 as a key."""
    client, _ = _client([_collection("BSEJHPJN", "SLR_2024")])
    assert client.find_collection("SLR_2024") == ("BSEJHPJN", "name")


def test_empty_collection_argument_is_no_collection() -> None:
    client, cloud = _client([])
    assert client.find_collection("") == ("", "")
    assert cloud.created == []


# ---------------------------------------------------------------------------
# _resolve_collection — the CLI layer, and what it says out loud
# ---------------------------------------------------------------------------


def _args(**kw):
    return argparse.Namespace(
        collection=kw.get("collection", ""),
        dry_run=kw.get("dry_run", False),
        no_create_collection=kw.get("no_create_collection", False),
    )


class _StubClient:
    def __init__(self, result):
        self._result = result

    def find_collection(self, name_or_key, *, create=True):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result

    def describe_library(self):
        return "group 6015547"


def test_resolve_collection_announces_a_creation(capsys) -> None:
    key = imp._resolve_collection(
        _StubClient(("CREATED1", "created")), _args(collection="AI SLR"),
    )
    assert key == "CREATED1"
    out = capsys.readouterr().out
    assert "CREATED" in out and "AI SLR" in out


def test_resolve_collection_announces_a_name_match(capsys) -> None:
    key = imp._resolve_collection(
        _StubClient(("BSEJHPJN", "name")), _args(collection="AI SLR"),
    )
    assert key == "BSEJHPJN"
    assert "matched by name" in capsys.readouterr().out


def test_resolve_collection_exits_on_an_ambiguous_name() -> None:
    stub = _StubClient(ValueError("Ambiguous collection name 'X'"))
    with pytest.raises(SystemExit) as exc:
        imp._resolve_collection(stub, _args(collection="X"))
    assert "Ambiguous" in str(exc.value)


def test_resolve_collection_does_not_create_under_dry_run(capsys) -> None:
    """A dry run has no API key and must write nothing at all."""

    class _Exploding:
        def find_collection(self, *a, **kw):
            raise AssertionError("--dry-run must not touch the API")

    key = imp._resolve_collection(
        _Exploding(), _args(collection="AI SLR", dry_run=True),
    )
    assert key == "AI SLR"
    assert "dry-run" in capsys.readouterr().out


def test_no_collection_argument_resolves_to_nothing() -> None:
    class _Exploding:
        def find_collection(self, *a, **kw):
            raise AssertionError("nothing to resolve")

    assert imp._resolve_collection(_Exploding(), _args()) == ""


# ---------------------------------------------------------------------------
# Read-your-writes
# ---------------------------------------------------------------------------


class _FakeZot:
    def __init__(self, items, label):
        self.items_returned = items
        self.label = label

    def items(self, **kwargs):
        return self.items_returned

    def everything(self, result):
        return result


def _client_with_backends(local_items, cloud_items):
    client = ZoteroClient.__new__(ZoteroClient)
    client._local = _FakeZot(local_items, "local")
    client._cloud = _FakeZot(cloud_items, "cloud")
    client.prefer_local = True
    return client


def test_cloud_journal_articles_ignores_the_local_preference() -> None:
    client = _client_with_backends(local_items=[], cloud_items=[{"key": "K1"}])
    assert client.cloud_journal_articles() == [{"key": "K1"}]
    assert client.journal_articles() == [], "journal_articles() stays local"


def test_dedup_precheck_reads_the_backend_the_import_writes() -> None:
    """The regression: the local client had not synced, reported zero
    items, and the import duplicated the whole corpus."""
    existing = [{
        "key": "EXISTING1",
        "data": {"key": "EXISTING1", "DOI": "10.1016/j.jbusvent.2019.105970",
                 "title": "A paper", "creators": [{"lastName": "Doe"}]},
    }]
    client = _client_with_backends(local_items=[], cloud_items=existing)

    doi_map, title_map = imp._fetch_existing_items(client, dry_run=False)

    assert doi_map == {"10.1016/j.jbusvent.2019.105970": "EXISTING1"}
    assert title_map


def test_dry_run_reads_nothing_at_all() -> None:
    class _Exploding:
        def cloud_journal_articles(self):
            raise AssertionError("--dry-run must not read the library")

    assert imp._fetch_existing_items(_Exploding(), dry_run=True) == ({}, {})
