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
    """Old stubs (dateAdded past the grace window) are classified
    as stubs. Attachments without md5 but recently added are treated
    as 'real' to protect in-flight Connector uploads."""
    zc = _client()
    fake = MagicMock()
    fake.everything.return_value = [
        {"key": "ATT1", "data": {"contentType": "application/pdf",
                                  "parentItem": "PARENT_A",
                                  "md5": "deadbeef",
                                  "dateAdded": "2020-01-01T00:00:00Z"}},
        {"key": "ATT2", "data": {"contentType": "application/pdf",
                                  "parentItem": "PARENT_A",
                                  "md5": None,
                                  "dateAdded": "2020-01-01T00:00:00Z"}},
        {"key": "ATT3", "data": {"contentType": "application/pdf",
                                  "parentItem": "PARENT_B",
                                  "md5": "",
                                  "dateAdded": "2020-01-01T00:00:00Z"}},
        {"key": "ATT4", "data": {"contentType": "text/html",
                                  "parentItem": "PARENT_B",
                                  "md5": None,
                                  "dateAdded": "2020-01-01T00:00:00Z"}},
    ]
    zc._local = fake

    result = zc.pdf_map()

    assert result["PARENT_A"][0] is True              # has real PDF
    assert result["PARENT_A"][1] == ["ATT2"]          # one old stub
    assert result["PARENT_B"][0] is False             # only stubs
    assert result["PARENT_B"][1] == ["ATT3"]          # one old stub
    # text/html attachments are not indexed
    assert "PARENT_C" not in result


def test_pdf_map_grace_window_protects_recent_attachments() -> None:
    """An attachment added within the grace window but with empty md5
    is NOT classified as a stub — it's likely an in-flight Connector
    upload where Zotero Desktop hasn't finished computing / syncing
    md5 yet. Deleting it would destroy the upload."""
    import datetime
    zc = _client()
    fake = MagicMock()
    # "Right now" — well within the 1-hour grace window.
    now_iso = datetime.datetime.now(datetime.UTC).isoformat()
    fake.everything.return_value = [
        {"key": "RECENT_STUB",
         "data": {"contentType": "application/pdf",
                  "parentItem": "PARENT_X",
                  "md5": "",
                  "dateAdded": now_iso}},
    ]
    zc._local = fake

    result = zc.pdf_map()

    # Treated as real (in-flight upload) — has_real=True, no stubs.
    assert result["PARENT_X"][0] is True
    assert result["PARENT_X"][1] == []


def test_pdf_map_unparseable_dateadded_protects_attachment() -> None:
    """Malformed / missing dateAdded → err on the side of keeping the
    attachment. Deleting a 'stub' that we can't verify as old is the
    worse outcome."""
    zc = _client()
    fake = MagicMock()
    fake.everything.return_value = [
        {"key": "WEIRD",
         "data": {"contentType": "application/pdf",
                  "parentItem": "PARENT_Y",
                  "md5": None,
                  "dateAdded": "not-a-timestamp"}},
    ]
    zc._local = fake

    result = zc.pdf_map()

    assert result["PARENT_Y"][0] is True
    assert result["PARENT_Y"][1] == []


def test_attach_pdf_delegates_to_pyzotero_attachment_simple(tmp_path) -> None:
    """pyzotero's Zupload returns lists of item dicts under success /
    failure / unchanged (see _upload.py:218-239)."""
    from pathlib import Path as _Path
    zc = _client()
    fake_cloud = MagicMock()
    fake_cloud.attachment_simple.return_value = {
        "success": [{"key": "NEWATT1", "title": "paper.pdf"}],
        "failure": [],
        "unchanged": [],
    }
    zc._cloud = fake_cloud

    pdf_path = tmp_path / "paper.pdf"
    result = zc.attach_pdf("PARENT1", str(pdf_path))

    # attach_pdf normalises via str(Path(...)), which uses backslashes on
    # Windows. Build the expected list through the same transformation so
    # the assertion matches on every OS.
    fake_cloud.attachment_simple.assert_called_once_with(
        [str(_Path(str(pdf_path)))], parentid="PARENT1",
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


# ---------------------------------------------------------------------------
# update_tags / get_tags — the tag write-back backbone of Zotero-as-truth.
# ---------------------------------------------------------------------------


def _tagged_item(key: str, version: int, tags: list[str]) -> dict:
    """Fake Zotero item payload with the given tags."""
    return {
        "key": key,
        "version": version,
        "data": {
            "key": key,
            "version": version,
            "tags": [{"tag": t} for t in tags],
        },
    }


def test_update_tags_adds_new_tag_preserving_existing() -> None:
    zc = _client()
    fake_cloud = MagicMock()
    fake_cloud.item.return_value = _tagged_item("X1", 5, ["keep:me"])
    fake_cloud.update_item.return_value = MagicMock(status_code=204)
    zc._cloud = fake_cloud

    changes = zc.update_tags("X1", add=["abstract:include"])

    assert changes == 1
    # Payload merges existing + new, sorted.
    expected_payload = {
        "key": "X1",
        "version": 5,
        "tags": [{"tag": "abstract:include"}, {"tag": "keep:me"}],
    }
    fake_cloud.update_item.assert_called_once_with(expected_payload)


def test_update_tags_noop_when_target_equals_existing() -> None:
    """If the computed target tag set equals the current set, skip the
    PATCH entirely — saves a network round-trip for items already in
    the desired state."""
    zc = _client()
    fake_cloud = MagicMock()
    fake_cloud.item.return_value = _tagged_item(
        "X1", 5, ["abstract:include", "keep:me"],
    )
    zc._cloud = fake_cloud

    changes = zc.update_tags("X1", add=["abstract:include"])

    assert changes == 0
    fake_cloud.update_item.assert_not_called()


def test_update_tags_remove_prefixed_replaces_stage_tag_atomically() -> None:
    """The canonical flip pattern: swap abstract:borderline → abstract:include
    in a single PATCH by combining add + remove_prefixed."""
    zc = _client()
    fake_cloud = MagicMock()
    fake_cloud.item.return_value = _tagged_item(
        "X1", 5, ["abstract:borderline", "predatory:flag"],
    )
    fake_cloud.update_item.return_value = MagicMock(status_code=204)
    zc._cloud = fake_cloud

    changes = zc.update_tags(
        "X1",
        add=["abstract:include"],
        remove_prefixed=["abstract:"],
    )

    # +abstract:include, -abstract:borderline = 2 changes.
    assert changes == 2
    fake_cloud.update_item.assert_called_once_with({
        "key": "X1",
        "version": 5,
        "tags": [{"tag": "abstract:include"}, {"tag": "predatory:flag"}],
    })


def test_update_tags_remove_exact() -> None:
    zc = _client()
    fake_cloud = MagicMock()
    fake_cloud.item.return_value = _tagged_item(
        "X1", 5, ["qa-flag", "qa-hard", "abstract:include"],
    )
    fake_cloud.update_item.return_value = MagicMock(status_code=204)
    zc._cloud = fake_cloud

    changes = zc.update_tags("X1", remove=["qa-flag", "qa-hard"])

    assert changes == 2
    fake_cloud.update_item.assert_called_once_with({
        "key": "X1",
        "version": 5,
        "tags": [{"tag": "abstract:include"}],
    })


def test_update_tags_retries_on_412_version_conflict(monkeypatch) -> None:
    """The @retry decorator catches VersionConflictError and re-runs
    update_tags, which re-fetches the item's current version."""
    import httpx
    zc = _client()
    fake_cloud = MagicMock()

    fake_cloud.item.side_effect = [
        _tagged_item("X1", 5, []),
        _tagged_item("X1", 6, []),
    ]
    conflict = httpx.HTTPStatusError(
        "Precondition Failed",
        request=httpx.Request("PATCH", "http://test"),
        response=httpx.Response(412),
    )
    fake_cloud.update_item.side_effect = [conflict, MagicMock(status_code=204)]
    zc._cloud = fake_cloud

    import tenacity
    monkeypatch.setattr(zc.update_tags.retry, "wait", tenacity.wait_none())

    changes = zc.update_tags("X1", add=["fulltext:include"])

    assert changes == 1
    assert fake_cloud.item.call_count == 2
    assert fake_cloud.update_item.call_count == 2


def test_get_tags_returns_set_of_tag_names() -> None:
    zc = _client()
    fake_cloud = MagicMock()
    fake_cloud.item.return_value = _tagged_item(
        "X1", 5, ["abstract:include", "predatory:flag", ""],
    )
    zc._cloud = fake_cloud

    tags = zc.get_tags("X1")

    assert tags == {"abstract:include", "predatory:flag"}


def test_get_tags_returns_empty_set_for_untagged_item() -> None:
    zc = _client()
    fake_cloud = MagicMock()
    fake_cloud.item.return_value = _tagged_item("X1", 5, [])
    zc._cloud = fake_cloud

    assert zc.get_tags("X1") == set()


# ---------------------------------------------------------------------------
# batch_update_tags — bulk path used by --csv-backfill. Fetches items in
# one call per batch, sends one multi-item PATCH per batch.
# ---------------------------------------------------------------------------


def _data_item(key: str, version: int, tags: list[str]) -> dict:
    """Item shape that comes back from `cloud.items(itemKey=...)` —
    flat (no nested `data` wrapper), matches Zotero's read API."""
    return {
        "key": key,
        "version": version,
        "data": {"key": key, "version": version,
                 "tags": [{"tag": t} for t in tags]},
    }


def test_batch_update_tags_returns_zeroes_on_empty_input() -> None:
    zc = _client()
    zc._cloud = MagicMock()
    stats = zc.batch_update_tags([])
    assert stats == {"applied": 0, "unchanged": 0, "failed": 0}


def test_batch_update_tags_sends_one_multi_item_patch() -> None:
    """Two items, three chunks at most one API call each: one bulk
    items() fetch + one update_items() PATCH."""
    zc = _client()
    fake_cloud = MagicMock()
    fake_cloud.items.return_value = [
        _data_item("A", 5, ["abstract:borderline"]),
        _data_item("B", 7, []),
    ]
    fake_cloud.update_items.return_value = {
        "success": {"0": "A", "1": "B"},
        "unchanged": {},
        "failed": {},
    }
    zc._cloud = fake_cloud

    stats = zc.batch_update_tags([
        ("A", {"add": ["abstract:include"],
               "remove_prefixed": ["abstract:"]}),
        ("B", {"add": ["abstract:exclude"]}),
    ])

    assert stats == {"applied": 2, "unchanged": 0, "failed": 0}
    # One bulk fetch with both keys.
    fake_cloud.items.assert_called_once_with(itemKey="A,B")
    # One multi-item PATCH.
    fake_cloud.update_items.assert_called_once()
    payloads = fake_cloud.update_items.call_args[0][0]
    assert len(payloads) == 2
    # A flipped: borderline removed, include added.
    a = next(p for p in payloads if p["key"] == "A")
    assert a["version"] == 5
    assert {t["tag"] for t in a["tags"]} == {"abstract:include"}
    # B tagged from scratch.
    b = next(p for p in payloads if p["key"] == "B")
    assert {t["tag"] for t in b["tags"]} == {"abstract:exclude"}


def test_batch_update_tags_skips_noops_without_patching() -> None:
    """Items already in the desired state should not appear in the
    PATCH payload at all."""
    zc = _client()
    fake_cloud = MagicMock()
    fake_cloud.items.return_value = [
        _data_item("A", 5, ["abstract:include"]),
    ]
    zc._cloud = fake_cloud

    stats = zc.batch_update_tags([
        ("A", {"add": ["abstract:include"],
               "remove_prefixed": ["abstract:"]}),
    ])

    assert stats == {"applied": 0, "unchanged": 1, "failed": 0}
    fake_cloud.update_items.assert_not_called()


def test_batch_update_tags_counts_missing_items_as_failed() -> None:
    """If the bulk fetch doesn't return an item (deleted between the
    audit scan and backfill), the update is marked failed. Never
    silently dropped."""
    zc = _client()
    fake_cloud = MagicMock()
    fake_cloud.items.return_value = [_data_item("A", 5, [])]  # B missing
    fake_cloud.update_items.return_value = {
        "success": {"0": "A"}, "unchanged": {}, "failed": {},
    }
    zc._cloud = fake_cloud

    stats = zc.batch_update_tags([
        ("A", {"add": ["t1"]}),
        ("B", {"add": ["t2"]}),
    ])

    assert stats == {"applied": 1, "unchanged": 0, "failed": 1}


def test_batch_update_tags_chunks_to_batch_size() -> None:
    """A 150-item backfill with batch_size=50 produces 3 fetches + 3 PATCHes."""
    zc = _client()
    fake_cloud = MagicMock()
    fake_cloud.items.side_effect = lambda itemKey: [
        _data_item(k, 1, []) for k in itemKey.split(",")
    ]
    fake_cloud.update_items.side_effect = lambda payloads: {
        "success": {str(i): p["key"] for i, p in enumerate(payloads)},
        "unchanged": {}, "failed": {},
    }
    zc._cloud = fake_cloud

    updates = [(f"K{i:03d}", {"add": ["x"]}) for i in range(150)]
    stats = zc.batch_update_tags(updates, batch_size=50)

    assert stats["applied"] == 150
    assert fake_cloud.items.call_count == 3
    assert fake_cloud.update_items.call_count == 3


def test_batch_update_tags_falls_back_when_update_items_missing() -> None:
    """Older pyzotero without update_items should still work via the
    per-item fallback path."""
    zc = _client()
    fake_cloud = MagicMock(spec=["items", "update_item"])  # no update_items
    fake_cloud.items.return_value = [_data_item("A", 5, [])]
    fake_cloud.update_item.return_value = True
    zc._cloud = fake_cloud

    stats = zc.batch_update_tags([("A", {"add": ["t1"]})])

    assert stats == {"applied": 1, "unchanged": 0, "failed": 0}
    fake_cloud.update_item.assert_called_once()


# ---------------------------------------------------------------------------
# upsert_child_note — creates a new note if none exists; updates in place
# if a matching-marker note is already attached. Used by fulltext_code.py
# to write/overwrite the SLR Coding child note.
# ---------------------------------------------------------------------------


MARKER = "<h1>SLR Coding</h1>"


def _note_child(key: str, version: int, body: str) -> dict:
    return {
        "key": key,
        "version": version,
        "data": {
            "key": key,
            "version": version,
            "itemType": "note",
            "note": body,
        },
    }


def _non_note_child(key: str) -> dict:
    return {
        "key": key,
        "data": {"key": key, "itemType": "attachment"},
    }


def test_upsert_child_note_creates_new_note_when_none_exists() -> None:
    zc = _client()
    fake_cloud = MagicMock()
    fake_cloud.children.return_value = [_non_note_child("ATT1")]
    # pyzotero's create_items returns success keyed by index as string.
    fake_cloud.create_items.return_value = {
        "success": {"0": {"key": "NEW1", "data": {"key": "NEW1"}}},
    }
    zc._cloud = fake_cloud

    body = f"{MARKER}\n<p>Decision: include</p>"
    key = zc.upsert_child_note("PARENT", marker=MARKER, note_html=body)

    assert key == "NEW1"
    fake_cloud.create_items.assert_called_once()
    call_payload = fake_cloud.create_items.call_args[0][0][0]
    assert call_payload["itemType"] == "note"
    assert call_payload["parentItem"] == "PARENT"
    assert call_payload["note"] == body
    fake_cloud.update_item.assert_not_called()


def test_upsert_child_note_updates_existing_note_matching_marker() -> None:
    zc = _client()
    fake_cloud = MagicMock()
    # One non-note child and one note that starts with MARKER.
    old_note = _note_child("N1", 12, f"{MARKER}\n<p>old body</p>")
    fake_cloud.children.return_value = [_non_note_child("ATT1"), old_note]
    fake_cloud.update_item.return_value = MagicMock(status_code=204)
    zc._cloud = fake_cloud

    new_body = f"{MARKER}\n<p>new body</p>"
    key = zc.upsert_child_note("PARENT", marker=MARKER, note_html=new_body)

    assert key == "N1"
    # No new items created.
    fake_cloud.create_items.assert_not_called()
    # Update patched the existing note with its version.
    fake_cloud.update_item.assert_called_once_with({
        "key": "N1", "version": 12, "note": new_body,
    })


def test_upsert_child_note_ignores_unrelated_notes() -> None:
    """A paper may have user-authored child notes (reading notes,
    annotations-as-notes). Our upsert must only touch notes that start
    with the marker — never overwrite the user's work."""
    zc = _client()
    fake_cloud = MagicMock()
    user_note = _note_child("U1", 3, "<p>My reading notes. Very important.</p>")
    fake_cloud.children.return_value = [user_note]
    fake_cloud.create_items.return_value = {
        "success": {"0": {"key": "NEW1"}},
    }
    zc._cloud = fake_cloud

    body = f"{MARKER}\n<p>Decision: include</p>"
    key = zc.upsert_child_note("PARENT", marker=MARKER, note_html=body)

    assert key == "NEW1"
    fake_cloud.create_items.assert_called_once()
    # The user's note must NOT have been touched.
    fake_cloud.update_item.assert_not_called()


def test_upsert_child_note_rejects_body_without_marker() -> None:
    """Guardrail: if note_html doesn't start with the marker, subsequent
    upserts won't find and update it — we'd leak duplicate notes. Fail
    loudly at the call site."""
    zc = _client()
    zc._cloud = MagicMock()

    with pytest.raises(ValueError, match="must begin with the marker"):
        zc.upsert_child_note("PARENT", marker=MARKER, note_html="<p>oops</p>")


def test_upsert_child_note_retries_on_412_version_conflict(monkeypatch) -> None:
    import httpx
    zc = _client()
    fake_cloud = MagicMock()

    # children() returns the old note twice (once per attempt); update
    # fails first with 412, succeeds second.
    old_note_v1 = _note_child("N1", 12, f"{MARKER}\n<p>old</p>")
    old_note_v2 = _note_child("N1", 13, f"{MARKER}\n<p>old</p>")
    fake_cloud.children.side_effect = [[old_note_v1], [old_note_v2]]
    conflict = httpx.HTTPStatusError(
        "Precondition Failed",
        request=httpx.Request("PATCH", "http://test"),
        response=httpx.Response(412),
    )
    fake_cloud.update_item.side_effect = [conflict, MagicMock(status_code=204)]
    zc._cloud = fake_cloud

    import tenacity
    monkeypatch.setattr(
        zc.upsert_child_note.retry, "wait", tenacity.wait_none(),
    )

    new_body = f"{MARKER}\n<p>new</p>"
    key = zc.upsert_child_note("PARENT", marker=MARKER, note_html=new_body)

    assert key == "N1"
    assert fake_cloud.children.call_count == 2
    assert fake_cloud.update_item.call_count == 2
