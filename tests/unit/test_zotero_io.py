"""Tests for scripts/pipelines/zotero_io.ZoteroClient."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import zotero_io


def _client() -> zotero_io.ZoteroClient:
    """ZoteroClient with fake credentials; real pyzotero clients are
    injected via the _local/_cloud properties in tests."""
    return zotero_io.ZoteroClient(api_key="fake-key", group_id="12345")


def test_from_config_reads_api_key_from_config(monkeypatch) -> None:
    """from_config() reads api_key from config/env but takes group_id as
    an argument — group is per-project and must not be in the global
    config (see test_setup_wizard.py:40-42 for the convention)."""
    from core import config_loader

    monkeypatch.setattr(config_loader, "load_config", lambda: {
        "zotero": {"api_key": "from-config"}
    })
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    monkeypatch.delenv("ZOTERO_GROUP", raising=False)

    zc = zotero_io.ZoteroClient.from_config(group_id="99")
    assert zc.api_key == "from-config"
    assert zc.group_id == "99"


def test_from_config_uses_env_group_id_when_no_arg(monkeypatch) -> None:
    from core import config_loader

    monkeypatch.setattr(config_loader, "load_config", lambda: {
        "zotero": {"api_key": "from-config"}
    })
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    monkeypatch.setenv("ZOTERO_GROUP", "77")

    zc = zotero_io.ZoteroClient.from_config()
    assert zc.group_id == "77"


def test_from_config_raises_group_selection_required_when_multiple_groups(monkeypatch) -> None:
    """With multiple accessible groups and no --group / $ZOTERO_GROUP,
    from_config() raises GroupSelectionRequired carrying the group list."""
    from core import config_loader

    monkeypatch.setattr(config_loader, "load_config", lambda: {
        "zotero": {"api_key": "from-config", "user_id": "5591"}
    })
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    monkeypatch.delenv("ZOTERO_GROUP", raising=False)
    monkeypatch.setattr(
        zotero_io, "_list_accessible_groups",
        lambda _key, _uid: [
            {"id": 111, "name": "Group A"},
            {"id": 222, "name": "Group B"},
        ],
    )

    import pytest
    with pytest.raises(zotero_io.GroupSelectionRequired) as exc_info:
        zotero_io.ZoteroClient.from_config()
    assert len(exc_info.value.groups) == 2
    assert exc_info.value.groups[0]["id"] == 111


def test_from_config_auto_selects_sole_group(monkeypatch, capsys) -> None:
    """With exactly one accessible group, from_config() uses it without
    error and prints a notice to stderr."""
    from core import config_loader

    monkeypatch.setattr(config_loader, "load_config", lambda: {
        "zotero": {"api_key": "from-config", "user_id": "5591"}
    })
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    monkeypatch.delenv("ZOTERO_GROUP", raising=False)
    monkeypatch.setattr(
        zotero_io, "_list_accessible_groups",
        lambda _key, _uid: [{"id": 333, "name": "Sole Group"}],
    )

    zc = zotero_io.ZoteroClient.from_config()
    assert zc.group_id == "333"
    out = capsys.readouterr().err
    assert "auto-selected" in out
    assert "333" in out


def test_from_config_raises_when_no_user_id_and_no_group(monkeypatch) -> None:
    """Without user_id, we can't query groups — raise GroupSelectionRequired
    with an empty list so the orchestrator shows the fallback message."""
    from core import config_loader

    monkeypatch.setattr(config_loader, "load_config", lambda: {
        "zotero": {"api_key": "from-config"}     # no user_id
    })
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    monkeypatch.delenv("ZOTERO_GROUP", raising=False)

    import pytest
    with pytest.raises(zotero_io.GroupSelectionRequired) as exc_info:
        zotero_io.ZoteroClient.from_config()
    assert exc_info.value.groups == []


def test_format_group_selection_error_lists_groups() -> None:
    groups = [{"id": 111, "name": "Lab A"}, {"id": 222, "name": "Lab B"}]
    msg = zotero_io.format_group_selection_error(groups)
    assert "Lab A" in msg and "111" in msg
    assert "Lab B" in msg and "222" in msg
    assert "--group" in msg
    assert "ZOTERO_GROUP" in msg


def test_format_group_selection_error_handles_empty_list() -> None:
    """When Zotero returned no groups (e.g. API error), show a different
    message pointing at the wizard."""
    msg = zotero_io.format_group_selection_error([])
    assert "wizard" in msg.lower() or "user_id" in msg


def test_journal_articles_uses_local_client() -> None:
    zc = _client()
    fake_local = MagicMock()
    fake_local.everything.return_value = [{"key": "ABC"}]
    fake_local.items.return_value = "items-iterator-placeholder"
    zc._local = fake_local

    result = zc.journal_articles()

    fake_local.items.assert_called_once_with(itemType="journalArticle")
    fake_local.everything.assert_called_once_with("items-iterator-placeholder")
    assert result == [{"key": "ABC"}]


def test_collection_items_filters_by_type() -> None:
    zc = _client()
    fake = MagicMock()
    fake.everything.return_value = []
    zc._local = fake

    zc.collection_items("COLLKEY", item_type="book")
    fake.collection_items.assert_called_once_with("COLLKEY", itemType="book")


def test_pdf_map_groups_real_vs_stub_attachments() -> None:
    zc = _client()
    fake = MagicMock()
    fake.everything.return_value = [
        {"key": "ATT1", "data": {"contentType": "application/pdf",
                                  "parentItem": "PARENT_A", "md5": "deadbeef"}},
        {"key": "ATT2", "data": {"contentType": "application/pdf",
                                  "parentItem": "PARENT_A", "md5": None}},
        {"key": "ATT3", "data": {"contentType": "application/pdf",
                                  "parentItem": "PARENT_B", "md5": ""}},
        {"key": "ATT4", "data": {"contentType": "text/html",
                                  "parentItem": "PARENT_B", "md5": None}},
    ]
    zc._local = fake

    result = zc.pdf_map()

    assert result["PARENT_A"][0] is True              # has real PDF
    assert result["PARENT_A"][1] == ["ATT2"]          # one stub
    assert result["PARENT_B"][0] is False             # only stubs
    assert result["PARENT_B"][1] == ["ATT3"]          # one stub
    # text/html attachments are not indexed
    assert "PARENT_C" not in result


def test_attach_pdf_delegates_to_pyzotero_attachment_simple() -> None:
    """pyzotero's Zupload returns lists of item dicts under success /
    failure / unchanged (see _upload.py:218-239)."""
    zc = _client()
    fake_cloud = MagicMock()
    fake_cloud.attachment_simple.return_value = {
        "success": [{"key": "NEWATT1", "title": "paper.pdf"}],
        "failure": [],
        "unchanged": [],
    }
    zc._cloud = fake_cloud

    result = zc.attach_pdf("PARENT1", "/tmp/paper.pdf")

    fake_cloud.attachment_simple.assert_called_once_with(
        ["/tmp/paper.pdf"], parentid="PARENT1",
    )
    assert result == "NEWATT1"


def test_attach_pdf_returns_none_on_unchanged() -> None:
    """pyzotero returns the file under 'unchanged' if the same hash
    is already attached — not an error, just a no-op."""
    zc = _client()
    fake_cloud = MagicMock()
    fake_cloud.attachment_simple.return_value = {
        "success": [],
        "failure": [],
        "unchanged": [{"key": "ALREADY_THERE"}],
    }
    zc._cloud = fake_cloud

    assert zc.attach_pdf("PARENT1", "/tmp/paper.pdf") is None


def test_attach_pdf_raises_on_failure() -> None:
    zc = _client()
    fake_cloud = MagicMock()
    fake_cloud.attachment_simple.return_value = {
        "success": [],
        "failure": [{"title": "Bad File"}],
        "unchanged": [],
    }
    zc._cloud = fake_cloud

    with pytest.raises(RuntimeError):
        zc.attach_pdf("PARENT1", "/tmp/bad.pdf")


def test_attach_pdf_reads_nested_data_key_shape() -> None:
    """Some pyzotero responses nest the key under `data` instead of at
    the top level. The wrapper should handle both."""
    zc = _client()
    fake_cloud = MagicMock()
    fake_cloud.attachment_simple.return_value = {
        "success": [{"data": {"key": "NESTED1"}}],
        "failure": [],
        "unchanged": [],
    }
    zc._cloud = fake_cloud
    assert zc.attach_pdf("PARENT1", "/tmp/x.pdf") == "NESTED1"


def test_update_abstract_patches_item_with_current_version() -> None:
    zc = _client()
    fake_cloud = MagicMock()
    fake_cloud.item.return_value = {"key": "X1", "version": 7}
    response = MagicMock(status_code=204)
    fake_cloud.update_item.return_value = response
    zc._cloud = fake_cloud

    ok = zc.update_abstract("X1", "new abstract text")

    fake_cloud.item.assert_called_once_with("X1")
    fake_cloud.update_item.assert_called_once_with({
        "key": "X1", "version": 7, "abstractNote": "new abstract text",
    })
    assert ok is True


def test_update_abstract_retries_on_412_version_conflict(monkeypatch) -> None:
    """The @retry decorator must catch VersionConflictError, which the
    method raises on HTTP 412, and re-run update_abstract (which then
    re-fetches the item's latest version)."""
    import httpx
    zc = _client()
    fake_cloud = MagicMock()

    # First call: returns stale version, pyzotero raises 412.
    # Second call: returns fresh version, pyzotero returns 204.
    version_seq = iter([{"key": "X1", "version": 7},
                        {"key": "X1", "version": 8}])
    fake_cloud.item.side_effect = lambda _k: next(version_seq)

    conflict = httpx.HTTPStatusError(
        "Precondition Failed",
        request=httpx.Request("PATCH", "http://test"),
        response=httpx.Response(412),
    )
    response_ok = MagicMock(status_code=204)
    fake_cloud.update_item.side_effect = [conflict, response_ok]
    zc._cloud = fake_cloud

    # Collapse tenacity waits for test speed.
    import tenacity
    monkeypatch.setattr(zc.update_abstract.retry, "wait", tenacity.wait_none())

    ok = zc.update_abstract("X1", "new abstract text")

    assert ok is True
    assert fake_cloud.item.call_count == 2
    assert fake_cloud.update_item.call_count == 2


def test_update_abstract_raises_on_non_412_http_error() -> None:
    """A 500 / 403 / etc. must propagate — not be caught as a version
    conflict."""
    import httpx
    zc = _client()
    fake_cloud = MagicMock()
    fake_cloud.item.return_value = {"key": "X1", "version": 7}

    non_conflict = httpx.HTTPStatusError(
        "Server Error",
        request=httpx.Request("PATCH", "http://test"),
        response=httpx.Response(500),
    )
    fake_cloud.update_item.side_effect = non_conflict
    zc._cloud = fake_cloud

    with pytest.raises(httpx.HTTPStatusError):
        zc.update_abstract("X1", "abstract")


def test_delete_item_calls_pyzotero_with_fresh_version() -> None:
    zc = _client()
    fake_cloud = MagicMock()
    fake_cloud.item.return_value = {"key": "STUB1", "version": 99}
    fake_cloud.delete_item.return_value = MagicMock(status_code=204)
    zc._cloud = fake_cloud

    assert zc.delete_item("STUB1") is True
    fake_cloud.delete_item.assert_called_once()
    args, kwargs = fake_cloud.delete_item.call_args
    assert args[0]["version"] == 99
    assert kwargs.get("last_modified") == 99


def test_read_client_prefers_local_by_default() -> None:
    zc = _client()
    assert zc.prefer_local is True
    assert zc._read_client() is zc.local


def test_read_client_uses_cloud_when_prefer_local_false() -> None:
    zc = zotero_io.ZoteroClient(
        api_key="k", group_id="1", prefer_local=False,
    )
    assert zc._read_client() is zc.cloud


# ---------------------------------------------------------------------------
# merge_duplicate_item — the Connector dedup path. Ported from
# zotero-mcp's merge_duplicates; tests assert the parts that matter
# for v0.4.0 routing (no permanent delete; attachment sig dedup;
# DOI mismatch guard).
# ---------------------------------------------------------------------------


class _PatchResponse:
    def __init__(self, status_code: int = 204) -> None:
        self.status_code = status_code


def _mock_cloud() -> MagicMock:
    """Fake cloud pyzotero client with minimal surface for merge."""
    m = MagicMock()
    m.endpoint = "https://api.zotero.org"
    m.library_type = "groups"
    m.library_id = "12345"
    patch_mock = MagicMock()
    patch_mock.patch.return_value = _PatchResponse(status_code=204)
    m.client = patch_mock
    return m


def _fake_item(key: str, *, doi: str = "", tags=None, collections=None,
               version: int = 1, extra: dict | None = None) -> dict:
    data: dict = {
        "DOI": doi,
        "tags": [{"tag": t} for t in (tags or [])],
        "collections": list(collections or []),
    }
    if extra:
        data.update(extra)
    return {"key": key, "version": version, "data": data}


def _fake_attachment(key: str, *, parent: str,
                     filename: str = "", md5: str = "",
                     content_type: str = "application/pdf",
                     url: str = "") -> dict:
    return {
        "key": key,
        "version": 1,
        "data": {
            "itemType": "attachment",
            "parentItem": parent,
            "contentType": content_type,
            "filename": filename,
            "md5": md5,
            "url": url,
        },
    }


def test_merge_duplicate_item_refuses_mismatched_dois(monkeypatch) -> None:
    """Safety guard: two non-empty DOIs that don't match → ValueError.
    Prevents merging two genuinely different papers by accident."""
    zc = _client()
    monkeypatch.setattr(type(zc), "cloud", _mock_cloud(),
                        raising=False)
    zc.cloud.item.side_effect = [
        _fake_item("KEEPER", doi="10.1/x"),
        _fake_item("DUPE",   doi="10.1/y"),    # different!
    ]

    with pytest.raises(ValueError, match="Refusing to merge"):
        zc.merge_duplicate_item("KEEPER", "DUPE")


def test_merge_duplicate_item_allows_when_one_doi_missing(monkeypatch) -> None:
    """Connector-created duplicates often arrive without a DOI in
    their data field; an empty DOI on one side is not a mismatch."""
    zc = _client()
    cloud = _mock_cloud()
    cloud.item.side_effect = [
        _fake_item("KEEPER", doi="10.1/x"),
        _fake_item("DUPE",   doi=""),
        # after mutation, merge re-fetches the duplicate for the trash
        # PATCH — return the same keyed shape.
        _fake_item("DUPE",   doi=""),
    ]
    cloud.children.side_effect = [[], []]      # no children either side
    monkeypatch.setattr(type(zc), "cloud", cloud, raising=False)

    stats = zc.merge_duplicate_item("KEEPER", "DUPE")
    assert stats["trashed"] == ["DUPE"]


def test_merge_duplicate_item_moves_children_and_unions_tags(monkeypatch) -> None:
    zc = _client()
    cloud = _mock_cloud()
    dup_pdf = _fake_attachment(
        "PDF-NEW", parent="DUPE",
        filename="fresh.pdf", md5="aaaa",
    )
    cloud.item.side_effect = [
        _fake_item("KEEPER", doi="10.1/x",
                   tags=["framing"], collections=["C1"]),
        _fake_item("DUPE", doi="10.1/x",
                   tags=["framing", "institutional"],
                   collections=["C1", "C2"]),
        # re-fetch after tag update
        _fake_item("KEEPER", doi="10.1/x",
                   tags=["framing", "institutional"],
                   collections=["C1"], version=2),
        # re-fetch after addto_collection
        _fake_item("KEEPER", doi="10.1/x",
                   tags=["framing", "institutional"],
                   collections=["C1", "C2"], version=3),
        # fresh lookup of the child before re-parent
        dup_pdf,
        # latest duplicate before the trash PATCH
        _fake_item("DUPE", doi="10.1/x",
                   tags=["framing", "institutional"],
                   collections=["C1", "C2"]),
    ]
    cloud.children.side_effect = [[], [dup_pdf]]
    monkeypatch.setattr(type(zc), "cloud", cloud, raising=False)

    stats = zc.merge_duplicate_item("KEEPER", "DUPE")

    assert stats["moved"] == 1
    assert stats["tags_added"] == 1       # "institutional" was new
    assert stats["collections_added"] == 1
    assert stats["trashed"] == ["DUPE"]
    # Child was re-parented to the keeper.
    assert dup_pdf["data"]["parentItem"] == "KEEPER"
    cloud.addto_collection.assert_called_once()


def test_merge_duplicate_item_skips_duplicate_attachments(monkeypatch) -> None:
    """A duplicate attachment with the same (contentType, filename,
    md5, url) signature is NOT re-parented — prevents twin PDFs."""
    zc = _client()
    cloud = _mock_cloud()
    keeper_pdf = _fake_attachment(
        "PDF-KEEPER", parent="KEEPER",
        filename="paper.pdf", md5="deadbeef",
    )
    dup_pdf = _fake_attachment(
        "PDF-DUPE", parent="DUPE",
        filename="paper.pdf", md5="deadbeef",
    )
    cloud.item.side_effect = [
        _fake_item("KEEPER", doi="10.1/x"),
        _fake_item("DUPE",   doi="10.1/x"),
        dup_pdf,                         # fresh child before re-parent
        _fake_item("DUPE",   doi="10.1/x"),   # latest before trash
    ]
    cloud.children.side_effect = [[keeper_pdf], [dup_pdf]]
    monkeypatch.setattr(type(zc), "cloud", cloud, raising=False)

    stats = zc.merge_duplicate_item("KEEPER", "DUPE")

    assert stats["moved"] == 0
    assert stats["skipped_dupe_attachments"] == 1


def test_merge_duplicate_item_trashes_via_patch_not_delete(monkeypatch) -> None:
    """Confirms the duplicate parent is TRASHED (PATCH {"deleted": 1}),
    not permanently deleted. Matters: Zotero's Trash lets the user
    recover, pyzotero's delete_item() does not."""
    zc = _client()
    cloud = _mock_cloud()
    cloud.item.side_effect = [
        _fake_item("KEEPER", doi="10.1/x"),
        _fake_item("DUPE",   doi="10.1/x"),
        _fake_item("DUPE",   doi="10.1/x"),     # latest before trash
    ]
    cloud.children.side_effect = [[], []]
    monkeypatch.setattr(type(zc), "cloud", cloud, raising=False)

    zc.merge_duplicate_item("KEEPER", "DUPE")

    # pyzotero.delete_item MUST NOT be used.
    assert not cloud.delete_item.called
    # The PATCH call body must include {"deleted": 1}.
    cloud.client.patch.assert_called_once()
    _args, kwargs = cloud.client.patch.call_args
    import json as _json
    body = _json.loads(kwargs["content"])
    assert body == {"deleted": 1}
    assert kwargs["headers"]["If-Unmodified-Since-Version"] == "1"


def test_merge_duplicate_item_reports_trash_failure(monkeypatch, caplog) -> None:
    """A non-204 PATCH response produces an empty `trashed` list — the
    caller can detect the failure without a raised exception (the
    merge itself succeeded, only the cleanup step didn't)."""
    zc = _client()
    cloud = _mock_cloud()
    cloud.client.patch.return_value = _PatchResponse(status_code=412)
    cloud.item.side_effect = [
        _fake_item("KEEPER", doi="10.1/x"),
        _fake_item("DUPE",   doi="10.1/x"),
        _fake_item("DUPE",   doi="10.1/x"),
    ]
    cloud.children.side_effect = [[], []]
    monkeypatch.setattr(type(zc), "cloud", cloud, raising=False)

    caplog.set_level("WARNING")
    stats = zc.merge_duplicate_item("KEEPER", "DUPE")

    assert stats["trashed"] == []
    assert any("trash PATCH returned HTTP 412" in r.message
               for r in caplog.records)
