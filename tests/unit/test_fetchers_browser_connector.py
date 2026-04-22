"""Tests for scripts/pipelines/fetchers/browser/connector.py.

Exercises extension-path resolution, Zotero Desktop ping, and the
new-item poll. Playwright is NOT loaded — the Chromium-bound paths
are live-tested separately (tests/live/).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fetchers.browser.connector import (
    ZoteroConnectorHandler,
    _poll_for_new_item,
    _wait_for_child_attachment,
    _wait_for_cloud_sync,
    ping_zotero_desktop,
    resolve_connector_extension_path,
)

# ---------------------------------------------------------------------------
# resolve_connector_extension_path
# ---------------------------------------------------------------------------


def test_resolve_connector_path_none_when_base_missing() -> None:
    assert resolve_connector_extension_path("/does/not/exist/anywhere") is None


def test_resolve_connector_path_explicit_version_dir(tmp_path: Path) -> None:
    """An explicit path that already points at a version folder
    (contains manifest.json) is returned verbatim."""
    version = tmp_path / "5.0.130_0"
    version.mkdir()
    (version / "manifest.json").write_text("{}")
    assert resolve_connector_extension_path(version) == version


def test_resolve_connector_path_picks_latest_version_subdir(tmp_path: Path) -> None:
    """When passed the extension base (not a version dir), the helper
    picks the highest-named subdirectory so future Connector updates
    are picked up automatically."""
    (tmp_path / "5.0.100_0").mkdir()
    (tmp_path / "5.0.130_0").mkdir()
    (tmp_path / "4.9.9_0").mkdir()
    result = resolve_connector_extension_path(tmp_path)
    assert result is not None and result.name == "5.0.130_0"


def test_resolve_connector_path_returns_none_on_empty_base(tmp_path: Path) -> None:
    (tmp_path / "random_file").write_text("")  # not a dir — ignored
    assert resolve_connector_extension_path(tmp_path) is None


def test_resolve_connector_path_falls_back_to_platform_defaults(
    monkeypatch, tmp_path: Path,
) -> None:
    """With no explicit path, the helper probes the platform defaults
    in order. Redirect one of them to a fake extension dir and confirm
    it's picked up."""
    from fetchers.browser import connector

    fake_ext = tmp_path / "ext" / "5.0.0_0"
    fake_ext.mkdir(parents=True)
    (fake_ext / "manifest.json").write_text("{}")

    monkeypatch.setattr(
        connector, "_default_extension_search_paths",
        lambda: [tmp_path / "nonexistent", tmp_path / "ext"],
    )
    assert resolve_connector_extension_path() == fake_ext


# ---------------------------------------------------------------------------
# ZoteroConnectorHandler construction
# ---------------------------------------------------------------------------


def test_handler_accepts_explicit_extension_path(tmp_path: Path) -> None:
    """Explicit path flows through into the instance attribute."""
    version = tmp_path / "5.0.130_0"
    version.mkdir()
    (version / "manifest.json").write_text("{}")
    h = ZoteroConnectorHandler(extension_path=version)
    assert h.extension_path == version


def test_handler_declares_attaches_directly() -> None:
    """Signals to the driver that this handler uses download_and_attach,
    not download()."""
    h = ZoteroConnectorHandler(extension_path=None)
    assert h.attaches_directly is True
    # And the standard download() raises — the driver must route to
    # download_and_attach for attaches_directly=True handlers.
    import asyncio
    with pytest.raises(NotImplementedError):
        asyncio.run(h.download(
            None, None, {"doi": "x"}, ".", counter=MagicMock(),
            total=1, t_start=0.0,
        ))


def test_handler_direct_access_domains_empty() -> None:
    """The Connector handler does not claim any direct-access domains —
    it trusts the routing layer to hand it a reachable URL."""
    h = ZoteroConnectorHandler(extension_path=None)
    assert h.direct_access_domains == ()


# ---------------------------------------------------------------------------
# Zotero Desktop ping
# ---------------------------------------------------------------------------


def test_ping_zotero_desktop_true_on_200() -> None:
    session = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    session.get.return_value = resp
    assert ping_zotero_desktop(session) is True


def test_ping_zotero_desktop_false_on_non_200() -> None:
    session = MagicMock()
    resp = MagicMock()
    resp.status_code = 502
    session.get.return_value = resp
    assert ping_zotero_desktop(session) is False


def test_ping_zotero_desktop_false_on_exception() -> None:
    """Zotero Desktop being off is the expected miss — never raise."""
    session = MagicMock()
    session.get.side_effect = RuntimeError("connection refused")
    assert ping_zotero_desktop(session) is False


# ---------------------------------------------------------------------------
# _poll_for_new_item — the DOI-based dedup lookup used after a save.
# ---------------------------------------------------------------------------


def test_poll_for_new_item_returns_new_key_when_found() -> None:
    """A new Zotero item with the same DOI as the keeper (but a
    different key) is exactly what the Connector creates."""
    zot = MagicMock()
    zot.journal_articles.return_value = [
        {"key": "KEEPER", "data": {"DOI": "10.1/x"}},
        {"key": "NEW123", "data": {"DOI": "10.1/x"}},
    ]
    result = _poll_for_new_item(zot, "10.1/x", "KEEPER", timeout_s=0.1)
    assert result == "NEW123"


def test_poll_for_new_item_ignores_keeper_itself() -> None:
    """If the only item with the matching DOI IS the keeper, the poll
    returns None (no duplicate was created)."""
    zot = MagicMock()
    zot.journal_articles.return_value = [
        {"key": "KEEPER", "data": {"DOI": "10.1/x"}},
    ]
    assert _poll_for_new_item(
        zot, "10.1/x", "KEEPER", timeout_s=0.2,
    ) is None


def test_poll_for_new_item_matches_case_insensitive() -> None:
    zot = MagicMock()
    zot.journal_articles.return_value = [
        {"key": "NEW", "data": {"DOI": "10.1/ABC"}},
    ]
    assert _poll_for_new_item(
        zot, "10.1/abc", "KEEPER", timeout_s=0.1,
    ) == "NEW"


def test_poll_for_new_item_survives_zotero_errors() -> None:
    """Transient errors from the library listing must not propagate —
    the pipeline would otherwise crash mid-batch."""
    zot = MagicMock()
    zot.journal_articles.side_effect = RuntimeError("zotero down")
    assert _poll_for_new_item(
        zot, "10.1/x", "KEEPER", timeout_s=0.1,
    ) is None


# ---------------------------------------------------------------------------
# _wait_for_cloud_sync — closes the race between Desktop-save and
# the subsequent cloud-API merge.
# ---------------------------------------------------------------------------


def test_wait_for_cloud_sync_returns_true_when_item_is_visible() -> None:
    """The item is already in the cloud on first poll."""
    zot = MagicMock()
    zot.cloud.item.return_value = {"key": "NEW", "data": {}}
    assert _wait_for_cloud_sync(zot, "NEW", timeout_s=0.2) is True


def test_wait_for_cloud_sync_returns_false_on_persistent_404() -> None:
    """Cloud never replicates within timeout → return False so the
    caller can skip the merge and log a specific error."""
    zot = MagicMock()
    zot.cloud.item.side_effect = Exception("404 Not Found")
    assert _wait_for_cloud_sync(zot, "NEW", timeout_s=0.3) is False


def test_wait_for_cloud_sync_recovers_after_transient_404() -> None:
    """First two poll attempts raise 404; third returns the item.
    Simulates the common case where Desktop sync takes ~2s."""
    zot = MagicMock()
    attempts = {"n": 0}

    def fake_item(key):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise Exception("404 Not Found")
        return {"key": key, "data": {}}

    zot.cloud.item.side_effect = fake_item
    assert _wait_for_cloud_sync(zot, "NEW", timeout_s=5) is True
    assert attempts["n"] >= 2


# ---------------------------------------------------------------------------
# _wait_for_child_attachment — closes the race between parent-synced
# and PDF-child-synced. JSTOR is the canonical slow case.
# ---------------------------------------------------------------------------


def test_wait_for_child_attachment_returns_true_when_pdf_has_md5() -> None:
    """Real attached PDF: attachment with non-empty md5."""
    zot = MagicMock()
    zot.cloud.children.return_value = [
        {"key": "PDF1",
         "data": {"itemType": "attachment", "md5": "deadbeef"}},
    ]
    assert _wait_for_child_attachment(zot, "NEW", timeout_s=0.2) is True


def test_wait_for_child_attachment_rejects_shell_without_md5() -> None:
    """Attachment record exists but md5 is still empty — file upload
    hasn't completed. Merging now would lock in a 'stub' (pdf_map
    deletes these on the next run).  We must wait."""
    zot = MagicMock()
    zot.cloud.children.return_value = [
        {"key": "PDF1",
         "data": {"itemType": "attachment", "md5": ""}},
    ]
    assert _wait_for_child_attachment(zot, "NEW", timeout_s=0.2) is False


def test_wait_for_child_attachment_ignores_non_attachment_children() -> None:
    """A note-only child doesn't count — we specifically want an
    attachment (the PDF)."""
    zot = MagicMock()
    zot.cloud.children.return_value = [
        {"key": "NOTE1", "data": {"itemType": "note"}},
    ]
    assert _wait_for_child_attachment(zot, "NEW", timeout_s=0.2) is False


def test_wait_for_child_attachment_times_out_on_empty() -> None:
    """Translator saved metadata only — no attachment ever appears.
    We must time out (not hang) and return False so the caller can
    proceed with the merge and log PARTIAL."""
    zot = MagicMock()
    zot.cloud.children.return_value = []
    assert _wait_for_child_attachment(zot, "NEW", timeout_s=0.3) is False


def test_wait_for_child_attachment_recovers_after_transient_error() -> None:
    """First poll raises; second returns the attachment with md5 —
    simulates the narrow window where the PDF is mid-upload."""
    zot = MagicMock()
    attempts = {"n": 0}

    def fake_children(_key):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise Exception("transient")
        return [
            {"key": "PDF1",
             "data": {"itemType": "attachment", "md5": "abc123"}},
        ]

    zot.cloud.children.side_effect = fake_children
    assert _wait_for_child_attachment(zot, "NEW", timeout_s=5) is True
    assert attempts["n"] >= 2
