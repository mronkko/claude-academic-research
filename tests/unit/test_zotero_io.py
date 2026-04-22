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
