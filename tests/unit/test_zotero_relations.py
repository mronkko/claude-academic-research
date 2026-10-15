"""ZoteroClient.add_related: merge `dc:relation` links, never clobber."""

from __future__ import annotations

from zotero_io import ZoteroClient


class _Client:
    def __init__(self, data):
        self.data = data
        self.updates: list[dict] = []

    def item(self, key):
        return {"key": key, "version": 5, "data": self.data}

    def update_item(self, payload):
        self.updates.append(payload)
        return True


def _zot(data, *, library_type="group", group_id="6015547"):
    zot = ZoteroClient.__new__(ZoteroClient)
    zot.library_type, zot.group_id = library_type, group_id
    zot.prefer_local, zot.local_api_key = False, ""
    zot._cloud = _Client(data)
    return zot


def test_item_uri_matches_zotero_form():
    assert _zot({}).item_uri("ABCD1234") == \
        "http://zotero.org/groups/6015547/items/ABCD1234"
    assert _zot({}, library_type="user", group_id="5591").item_uri("K") == \
        "http://zotero.org/users/5591/items/K"


def test_add_related_keeps_existing_relations_and_predicates():
    other = "http://zotero.org/groups/6015547/items/OLD00001"
    zot = _zot({"relations": {"dc:relation": other,
                              "owl:sameAs": "http://zotero.org/x"}})
    assert zot.add_related("SUPP0001", ["ART00001"], cloud=True) == 1
    (payload,) = zot._cloud.updates
    assert payload["version"] == 5
    assert payload["relations"] == {
        "dc:relation": [other,
                        "http://zotero.org/groups/6015547/items/ART00001"],
        "owl:sameAs": "http://zotero.org/x",
    }


def test_add_related_is_idempotent_and_ignores_self():
    uri = "http://zotero.org/groups/6015547/items/ART00001"
    zot = _zot({"relations": {"dc:relation": [uri]}})
    assert zot.add_related("SUPP0001", ["ART00001", "SUPP0001"],
                           cloud=True) == 0
    assert zot._cloud.updates == []


class _Conflict(Exception):
    class response:  # noqa: N801 — mimics httpx's attribute shape
        status_code = 412


def test_add_related_rereads_the_version_on_412(monkeypatch):
    """Same version/412 pattern as `update_fields`: re-read, then retry."""
    monkeypatch.setattr(ZoteroClient.add_related.retry, "sleep",
                        lambda _s: None)
    zot = _zot({})
    reads: list[str] = []
    real_item = zot._cloud.item

    def item(key):
        reads.append(key)
        return real_item(key)

    calls = {"n": 0}

    def update_item(payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _Conflict()
        zot._cloud.updates.append(payload)

    zot._cloud.item, zot._cloud.update_item = item, update_item
    assert zot.add_related("SUPP0001", ["ART00001"], cloud=True) == 1
    assert len(reads) == 2 and len(zot._cloud.updates) == 1
