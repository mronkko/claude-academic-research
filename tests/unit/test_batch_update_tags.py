"""`ZoteroClient.batch_update_tags` against a real pyzotero client.

These drive a genuine `pyzotero.zotero.Zotero` whose HTTP client is an
`httpx2.MockTransport` speaking Zotero's write protocol, rather than a
`MagicMock` standing in for pyzotero. The earlier tests mocked
`update_items` returning a `{success, unchanged, failed}` dict — a shape
pyzotero 1.16 never returns: its `_batch_update` throws the response body
away and returns `True` for any 2xx. Zotero answers a multi-object write
with 200 even when it rejected objects in it (the rejections are in the
body's `failed` map), so every rejected tag write was counted as applied,
and the mocked tests could not see it.

The fake server keeps per-item versions and applies Zotero's rules: an
object whose `version` is not the stored one fails with code 412, an
identical tag set is `unchanged`, anything else succeeds and bumps the
version.
"""

from __future__ import annotations

import copy
import json

import httpx2
import pytest
import zotero_io
from pyzotero import errors as pz_errors

LIB = "/api/groups/12345"


class FakeZotero:
    """Just enough of Zotero Desktop's local API for the tag-batch path."""

    def __init__(self, items: dict[str, list[str]]):
        self.items = {
            key: {"key": key, "version": 10,
                  "data": {"key": key, "version": 10,
                           "tags": [{"tag": t} for t in tags]}}
            for key, tags in items.items()
        }
        self.posts: list[list[dict]] = []
        self.gets = 0
        #: Statuses to answer the next POSTs with instead of processing.
        self.post_failures: list[httpx2.Response] = []
        #: Objects to reject with this code (per-object, inside a 200).
        self.reject: dict[str, tuple[int, str]] = {}
        #: Keys someone else edits just before our next POST lands.
        self.concurrent_edit: set[str] = set()

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if request.method == "GET" and path == f"{LIB}/items":
            self.gets += 1
            wanted = request.url.params["itemKey"].split(",")
            body = [copy.deepcopy(self.items[k]) for k in wanted if k in self.items]
            return httpx2.Response(200, json=body)
        if request.method == "GET" and path == "/api/itemFields":
            return httpx2.Response(200, json=[{"field": "title"}])
        if request.method == "POST" and path.rstrip("/") == f"{LIB}/items":
            payload = json.loads(request.content)
            self.posts.append(payload)
            if self.post_failures:
                return self.post_failures.pop(0)
            return httpx2.Response(200, json=self._write(payload))
        return httpx2.Response(404, text="not in fake")

    def _write(self, payload: list[dict]) -> dict:
        for key in self.concurrent_edit:
            self._bump(key)
        self.concurrent_edit = set()
        out: dict = {"successful": {}, "success": {}, "unchanged": {}, "failed": {}}
        for idx, obj in enumerate(payload):
            slot, key = str(idx), obj["key"]
            if key in self.reject:
                code, message = self.reject[key]
                out["failed"][slot] = {"key": key, "code": code, "message": message}
                continue
            stored = self.items[key]
            if obj["version"] != stored["version"]:
                out["failed"][slot] = {
                    "key": key, "code": 412,
                    "message": f"Item {key} has been modified since "
                               f"specified version",
                }
                continue
            if obj["tags"] == stored["data"]["tags"]:
                out["unchanged"][slot] = key
                continue
            stored["data"]["tags"] = obj["tags"]
            self._bump(key)
            out["success"][slot] = key
            out["successful"][slot] = copy.deepcopy(stored)
        return out

    def _bump(self, key: str) -> None:
        item = self.items[key]
        item["version"] += 1
        item["data"]["version"] = item["version"]

    def tags(self, key: str) -> set[str]:
        return {t["tag"] for t in self.items[key]["data"]["tags"]}


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(zotero_io, "_write_sleep", lambda s: None, raising=False)
    monkeypatch.setattr(zotero_io, "_page_sleep", lambda s: None)


def _client(server: FakeZotero) -> zotero_io.ZoteroClient:
    zc = zotero_io.ZoteroClient(group_id="12345", local_api_key="fake-key")
    zc._local = zotero_io._LocalZotero(
        "12345", "group", None, local=True, local_api_key="fake-key",
        server_id="fake-server",
        client=httpx2.Client(transport=httpx2.MockTransport(server.handler)),
    )
    return zc


def test_pyzotero_update_items_still_hides_per_object_failures() -> None:
    """Pins the reason `batch_update_tags` cannot use `update_items`.

    If this starts failing, pyzotero has begun returning Zotero's result
    map, and `_post_items` may be replaceable by the library call."""
    server = FakeZotero({"A": []})
    server.reject["A"] = (400, "Invalid tag")
    z = _client(server)._local

    assert z.update_items([{"key": "A", "version": 10, "tags": [{"tag": "x"}]}]) is True


def test_applies_tags_in_one_write() -> None:
    server = FakeZotero({"A": ["r/abstract:borderline"], "B": []})
    zc = _client(server)

    stats = zc.batch_update_tags([
        ("A", {"add": ["r/abstract:include"], "remove_prefixed": ["r/abstract:"]}),
        ("B", {"add": ["r/abstract:exclude"]}),
    ])

    assert stats == {"applied": 2, "unchanged": 0, "failed": 0}
    assert len(server.posts) == 1
    assert server.tags("A") == {"r/abstract:include"}
    assert server.tags("B") == {"r/abstract:exclude"}


def test_an_object_zotero_rejected_is_counted_failed() -> None:
    """A1. Zotero answers 200 with the object in `failed`; through 1.16
    pyzotero reported that as success and the old code counted it
    applied — a screening decision silently lost, then never re-screened
    because the caller thought it had landed."""
    server = FakeZotero({"A": [], "B": []})
    server.reject["B"] = (400, "Tag must be a string")
    zc = _client(server)

    stats = zc.batch_update_tags([
        ("A", {"add": ["t"]}),
        ("B", {"add": ["t"]}),
    ])

    assert stats == {"applied": 1, "unchanged": 0, "failed": 1}
    assert server.tags("B") == set()


def test_a_per_object_version_conflict_is_reread_and_resent() -> None:
    """Someone edits B between our read and our write: Zotero fails it
    with 412 inside a 200. B is re-read and written at its new version."""
    server = FakeZotero({"A": [], "B": ["keep"]})
    server.concurrent_edit = {"B"}
    zc = _client(server)

    stats = zc.batch_update_tags([
        ("A", {"add": ["t"]}),
        ("B", {"add": ["t"]}),
    ])

    assert stats == {"applied": 2, "unchanged": 0, "failed": 0}
    assert len(server.posts) == 2
    assert [o["key"] for o in server.posts[1]] == ["B"]
    assert server.tags("B") == {"keep", "t"}


def test_a_conflict_that_never_clears_is_failed_not_looped() -> None:
    server = FakeZotero({"A": []})
    zc = _client(server)
    original = server._write

    def edited_every_time(payload):
        server.concurrent_edit = {"A"}
        return original(payload)

    server._write = edited_every_time
    stats = zc.batch_update_tags([("A", {"add": ["t"]})])

    assert stats == {"applied": 0, "unchanged": 0, "failed": 1}
    assert len(server.posts) == zotero_io._TAG_CONFLICT_ROUNDS


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_a_transient_server_error_is_retried(status) -> None:
    """A2. One 5xx used to raise straight out of abstract screening."""
    server = FakeZotero({"A": []})
    server.post_failures = [httpx2.Response(status, text="Service unavailable")]
    zc = _client(server)

    stats = zc.batch_update_tags([("A", {"add": ["t"]})])

    assert stats == {"applied": 1, "unchanged": 0, "failed": 0}
    assert server.tags("A") == {"t"}


def test_a_retry_after_a_write_that_landed_counts_it_unchanged() -> None:
    """The ambiguous case: the write landed but the answer was a 502.
    The retry re-reads, finds the tags already there, and says so."""
    server = FakeZotero({"A": []})
    zc = _client(server)
    original = server.handler
    calls = {"n": 0}

    def landed_then_502(request):
        resp = original(request)
        if request.method == "POST" and calls["n"] == 0:
            calls["n"] += 1
            return httpx2.Response(502, text="Bad gateway")
        return resp

    zc._local.client = httpx2.Client(transport=httpx2.MockTransport(landed_then_502))

    stats = zc.batch_update_tags([("A", {"add": ["t"]})])

    assert stats == {"applied": 0, "unchanged": 1, "failed": 0}
    assert len(server.posts) == 1


def test_a_429_with_backoff_is_not_reported_as_applied(monkeypatch) -> None:
    """pyzotero's error handler returns normally on a 429 that carries a
    Backoff header, so `update_items` returned True having written
    nothing."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    server = FakeZotero({"A": []})
    server.post_failures = [httpx2.Response(429, headers={"Backoff": "1"})]
    zc = _client(server)

    stats = zc.batch_update_tags([("A", {"add": ["t"]})])

    assert stats == {"applied": 1, "unchanged": 0, "failed": 0}
    assert len(server.posts) == 2
    assert server.tags("A") == {"t"}


def test_persistent_server_errors_count_the_batch_failed_without_raising() -> None:
    server = FakeZotero({"A": [], "B": []})
    server.post_failures = [
        httpx2.Response(503, text="down") for _ in range(zotero_io._TAG_BATCH_ATTEMPTS)
    ]
    zc = _client(server)

    stats = zc.batch_update_tags([("A", {"add": ["t"]}), ("B", {"add": ["t"]})])

    assert stats == {"applied": 0, "unchanged": 0, "failed": 2}
    assert len(server.posts) == zotero_io._TAG_BATCH_ATTEMPTS


def test_a_permission_error_raises() -> None:
    """403 would fail every batch alike: say so instead of counting."""
    server = FakeZotero({"A": []})
    server.post_failures = [httpx2.Response(403, text="Forbidden")]
    zc = _client(server)

    with pytest.raises(pz_errors.UserNotAuthorisedError):
        zc.batch_update_tags([("A", {"add": ["t"]})])
    assert len(server.posts) == 1


def test_a_missing_item_is_counted_failed() -> None:
    server = FakeZotero({"A": []})
    zc = _client(server)

    stats = zc.batch_update_tags([("A", {"add": ["t"]}), ("GONE", {"add": ["t"]})])

    assert stats == {"applied": 1, "unchanged": 0, "failed": 1}


def test_noops_are_not_sent() -> None:
    server = FakeZotero({"A": ["t"]})
    zc = _client(server)

    stats = zc.batch_update_tags([("A", {"add": ["t"]})])

    assert stats == {"applied": 0, "unchanged": 1, "failed": 0}
    assert server.posts == []


def test_large_runs_are_chunked_at_fifty() -> None:
    server = FakeZotero({f"K{i:03d}": [] for i in range(120)})
    zc = _client(server)

    stats = zc.batch_update_tags(
        [(f"K{i:03d}", {"add": ["x"]}) for i in range(120)], batch_size=500,
    )

    assert stats == {"applied": 120, "unchanged": 0, "failed": 0}
    assert [len(p) for p in server.posts] == [50, 50, 20]


def test_empty_input_touches_nothing() -> None:
    server = FakeZotero({})
    assert _client(server).batch_update_tags([]) == {
        "applied": 0, "unchanged": 0, "failed": 0,
    }
    assert server.gets == 0
