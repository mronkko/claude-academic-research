"""Live round trip for writes on Zotero Desktop's local API.

Opt in with `pytest -m live`. Needs a running Zotero 10 with the local
API enabled, `[zotero] local_api_key` granted through `/setup`, and the
hand-created `academic-research-e2e` group. Skips cleanly on any of
those, because none can be provisioned from a test.

Why this exists rather than a unit test with a mock: the whole point of
the feature is a claim about *another program's* behaviour — that
Zotero 10.0.1 accepts a write on `localhost:23119` with a key granted
through its consent dialog, preserves item versions across it, and
serves the result back to a local read. A mock asserts our idea of
that. This asserts Zotero.

The version assertion is the load-bearing one. `update_item` sends
`If-Unmodified-Since-Version`, so a surface that did not increment
versions would make every optimistic-concurrency retry in `zotero_io`
silently useless — and the 412 retry on `update_abstract` would mask it
rather than report it.

Everything runs in the e2e group and cleans up after itself.
"""

from __future__ import annotations

import uuid

import pytest
import zotero_io

from tests.live.conftest import e2e_group, e2e_zotero_client, require_zotero_local

pytestmark = pytest.mark.live


def _scratch_payload(label: str) -> dict:
    return {
        "itemType": "journalArticle",
        "title": f"[live-test] {label} {uuid.uuid4().hex[:8]}",
        "DOI": f"10.9999/{label.replace(' ', '-')}-{uuid.uuid4().hex[:8]}",
    }


def _created_key(result: dict) -> str:
    """The key of the one item a `create_items` call made."""
    success = result.get("success") or {}
    assert len(success) == 1, f"expected one created item, got {result!r}"
    return next(iter(success.values()))


@pytest.fixture(scope="module")
def local_client():
    return e2e_zotero_client()


@pytest.fixture
def scratch_item(local_client):
    """A throwaway item created locally, removed after the test."""
    key = _created_key(local_client.create_items([_scratch_payload("local write")]))
    yield key
    try:
        local_client.delete_item(key)
    except Exception:
        pass


def test_a_local_write_is_visible_to_a_local_read_immediately(
    local_client, scratch_item,
):
    """The whole point: no sync wait between writing and reading back.

    When writes went to the Web API this needed Zotero Desktop to sync
    first, which is the gap that made a script write an item, read it
    back locally, see nothing, and report "already done".
    """
    abstract = f"local-write probe {uuid.uuid4().hex}"
    assert local_client.update_abstract(scratch_item, abstract) is True

    read_back = local_client.local.item(scratch_item)
    assert read_back["data"]["abstractNote"] == abstract


def test_a_local_write_increments_the_item_version(local_client, scratch_item):
    """Optimistic concurrency survives the move.

    `update_item` sends `If-Unmodified-Since-Version`; if the local
    surface did not version writes, every 412 retry in `zotero_io`
    would be dead code and concurrent writers would overwrite silently.
    """
    before = local_client.get_item(scratch_item)["version"]
    local_client.update_abstract(scratch_item, f"v-probe {uuid.uuid4().hex}")
    after = local_client.get_item(scratch_item)["version"]
    assert after > before


def test_a_stale_version_is_rejected_locally(local_client, scratch_item):
    """A local write with an outdated version must fail, not clobber.

    Asserted through `_http_status_of` rather than a concrete exception
    class: pyzotero raised `httpx.HTTPStatusError` through 1.14 and its
    own `PreConditionFailedError` from 1.15, and this test naming one of
    them is exactly how the dead 412 retry was found.
    """
    from zotero_io import _http_status_of

    stale = local_client.get_item(scratch_item)["version"]
    local_client.update_abstract(scratch_item, f"first {uuid.uuid4().hex}")

    with pytest.raises(Exception) as exc:
        local_client.local.update_item({
            "key": scratch_item,
            "version": stale,
            "abstractNote": "should be refused",
        })
    assert _http_status_of(exc.value) == 412


def test_a_version_conflict_still_reaches_the_retry(local_client, scratch_item):
    """`update_abstract` must translate a 412 into `VersionConflictError`.

    That translation is what tenacity retries on. When pyzotero stopped
    raising httpx errors it stopped happening, and a routine conflict —
    two writers on one library — would have escaped as an uncaught
    pyzotero exception instead of being retried.

    Driven through `__wrapped__` so tenacity does not swallow the first
    conflict by retrying it into a success.
    """
    import zotero_io

    stale = local_client.get_item(scratch_item)
    local_client.update_abstract(scratch_item, f"bump {uuid.uuid4().hex}")

    original = local_client.get_item
    local_client.get_item = lambda _key: stale
    try:
        with pytest.raises(zotero_io.VersionConflictError):
            zotero_io.ZoteroClient.update_abstract.__wrapped__(
                local_client, scratch_item, "refused",
            )
    finally:
        local_client.get_item = original


def test_tags_written_locally_read_back_locally(local_client, scratch_item):
    """`update_tags` is the other write the screening stages lean on."""
    tag = f"live-test:{uuid.uuid4().hex[:8]}"
    local_client.update_tags(scratch_item, add=[tag])
    assert tag in local_client.get_tags(scratch_item)


# ---------------------------------------------------------------------------
# A revoked key is re-granted, not reported as a failed write.
# ---------------------------------------------------------------------------


def test_a_rejected_key_is_regranted_and_the_write_retried():
    """An invalid key must reach the re-grant path, then the write lands.

    Zotero answers a revoked or unknown key with 401 "Invalid or
    expired API key". `_LocalZotero._write` is meant to ask Zotero for a
    new key, hand it to `on_new_key` (config, in production) and resend
    once. Proven against real Zotero by starting from a key it has never
    issued. `_grant_key` is stubbed to return the configured real key so
    no consent dialog opens, and `on_new_key` is captured so the test
    does not rewrite config.toml.
    """
    real_key = require_zotero_local()
    group_id = str(e2e_group()["id"])
    client = zotero_io.ZoteroClient(group_id, local_api_key="invalid")

    granted: list[str] = []
    saved: list[str] = []

    def grant() -> str:
        granted.append("asked")
        return real_key

    client.local._grant_key = grant
    client.local._on_new_key = saved.append

    key = _created_key(client.create_items([_scratch_payload("regrant")]))
    try:
        assert granted == ["asked"], "the 401 never reached the re-grant path"
        assert saved == [real_key], "the new key was not handed on to be saved"
        assert client.local.local_api_key == real_key
        assert client.get_item(key)["data"]["title"].startswith("[live-test] regrant")
    finally:
        assert client.delete_item(key), f"cleanup: could not delete {key}"
