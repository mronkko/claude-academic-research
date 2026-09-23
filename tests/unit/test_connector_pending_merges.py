"""Connector saves whose cloud sync is slower than the per-item wait.

Zotero Desktop's upload to the cloud lagged ~7 minutes under concurrent
writers on 2026-09-23, so every item failed the 30 s wait, and the
message promised the item "will be auto-merged next time" — which
nothing implemented: a re-run saved a second copy and orphaned the
first. Now the pair is queued in a file and merged once it syncs, at the
end of the pass or at the start of the next one.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import enrich_pdfs
from fetchers.browser.connector import PendingMerges, settle_pending_merges


def test_the_queue_survives_a_restart(tmp_path) -> None:
    q = PendingMerges(tmp_path)
    q.add(keeper="K1", new_key="N1", doi="10.1/a")
    q.add(keeper="K1", new_key="N1", doi="10.1/a")          # idempotent
    q.add(keeper="K2", new_key="N2", doi="10.1/b")
    again = PendingMerges(tmp_path)
    assert again.keepers() == {"K1", "K2"}
    again.remove("N1")
    assert PendingMerges(tmp_path).keepers() == {"K2"}


def test_settle_merges_what_has_synced_and_keeps_the_rest(tmp_path, monkeypatch) -> None:
    from fetchers.browser import connector
    monkeypatch.setattr(connector, "_pdf_child_settled", lambda zot, key: key == "SYNCED")
    q = PendingMerges(tmp_path)
    q.add(keeper="K1", new_key="SYNCED", doi="10.1/a")
    q.add(keeper="K2", new_key="LAGGING", doi="10.1/b")
    zot = MagicMock()
    zot.cloud.item.side_effect = lambda key: (
        {"key": key} if key == "SYNCED" else (_ for _ in ()).throw(RuntimeError("404"))
    )
    zot.cloud.children.return_value = [
        {"key": "P", "data": {"itemType": "attachment", "contentType": "application/pdf"}},
    ]
    merge = MagicMock(return_value={"moved": 1, "moved_pdf_keys": ["P"]})
    merged = []
    settle_pending_merges(
        zot, q, merge=merge, wait_s=0,
        on_merged=lambda entry, stats: merged.append((entry["keeper"], stats)),
    )
    merge.assert_called_once_with("K1", "SYNCED")
    assert merged == [("K1", {"moved": 1, "moved_pdf_keys": ["P"]})]
    assert PendingMerges(tmp_path).keepers() == {"K2"}


def test_a_failed_merge_stays_queued(tmp_path) -> None:
    q = PendingMerges(tmp_path)
    q.add(keeper="K1", new_key="N1", doi="10.1/a")
    zot = MagicMock()
    zot.cloud.item.return_value = {"key": "N1"}
    settle_pending_merges(
        zot, q, merge=MagicMock(side_effect=RuntimeError("412")), wait_s=0,
        on_merged=lambda *a: None,
    )
    assert PendingMerges(tmp_path).keepers() == {"K1"}


def test_the_pass_settles_before_and_after_and_skips_queued_keepers() -> None:
    src = inspect.getsource(enrich_pdfs._run_browser_in_process)
    block = src[src.index("if connector_items:"):]
    block = block[:block.index("asyncio.run(_drive_connector(") + 400]
    assert block.index("settle_pending_merges(") < block.index("asyncio.run(_drive_connector(")
    assert "pending.keepers()" in block
    after = src[src.index("asyncio.run(_drive_connector("):]
    assert "_settle(" in after


def test_a_queued_save_is_logged_as_pending_not_failed() -> None:
    src = inspect.getsource(enrich_pdfs._drive_connector)
    assert '"connector_merge_pending"' in src


def test_the_waits_are_flags() -> None:
    args = enrich_pdfs._build_parser().parse_args(
        ["--connector-sync-timeout", "90", "--connector-merge-wait", "1200"])
    assert (args.connector_sync_timeout, args.connector_merge_wait) == (90, 1200)
    d = enrich_pdfs._build_parser().parse_args([])
    assert d.connector_sync_timeout == 30 and d.connector_merge_wait == 600


# ---------------------------------------------------------------------------
# The PDF child lags the parent
# ---------------------------------------------------------------------------
#
# Run 5, item 17 (10.1016/s0305-750x(03)00012-3): the parent synced, the
# PDF attachment record had not, the merge moved nothing, reported
# PARTIAL "metadata only" and trashed the only item holding the PDF.


def _pdf_child(key="P1"):
    return {"key": key, "version": 1,
            "data": {"itemType": "attachment", "contentType": "application/pdf",
                     "md5": "aa"}}


def _html_child():
    return {"key": "H1", "data": {"itemType": "attachment", "contentType": "text/html"}}


def test_the_child_wait_wants_a_pdf_not_any_attachment(monkeypatch) -> None:
    from fetchers.browser import connector
    from fetchers.browser.connector import _wait_for_child_attachment

    monkeypatch.setattr(connector.time, "sleep", lambda s: None)
    zot = MagicMock()
    zot.cloud.item.return_value = _pdf_child()
    zot.cloud.children.return_value = [_html_child()]
    assert _wait_for_child_attachment(zot, "N", 0.1) is False
    zot.cloud.children.return_value = [_html_child(), _pdf_child()]
    assert _wait_for_child_attachment(zot, "N", 0.1) is True


def test_settle_waits_for_the_pdf_child_before_merging(tmp_path) -> None:
    q = PendingMerges(tmp_path)
    q.add(keeper="K1", new_key="N1", doi="10.1/a")
    zot = MagicMock()
    zot.cloud.item.return_value = {"key": "N1"}
    zot.cloud.children.return_value = [_html_child()]
    merge = MagicMock()
    settle_pending_merges(zot, q, merge=merge, wait_s=0, on_merged=lambda *a: None)
    merge.assert_not_called()
    assert PendingMerges(tmp_path).keepers() == {"K1"}


def test_a_pdfless_save_is_given_up_after_its_grace_period(tmp_path) -> None:
    """Metadata-only saves exist; queued forever, their keepers would
    never be tried again."""
    q = PendingMerges(tmp_path)
    q.add(keeper="K1", new_key="N1", doi="10.1/a")
    q._rows[0]["queued_at"] = "2026-01-01T00:00:00+00:00"
    q._save()
    zot = MagicMock()
    zot.cloud.item.return_value = {"key": "N1"}
    zot.cloud.children.return_value = [_html_child()]
    gave_up = []
    settle_pending_merges(
        zot, PendingMerges(tmp_path), merge=MagicMock(),
        wait_s=0, on_merged=lambda *a: None, on_given_up=gave_up.append,
    )
    assert [r["new_key"] for r in gave_up] == ["N1"]
    assert PendingMerges(tmp_path).keepers() == set()


def test_a_merge_that_raises_is_queued_not_failed() -> None:
    """A still-live temporary item holds the PDF, so a failed merge is a
    retry, not a verdict."""
    import inspect

    from fetchers.browser import connector
    src = inspect.getsource(connector.ZoteroConnectorHandler.download_and_attach)
    handler = src[src.index("self.merge_saved_item"):]
    handler = handler[:handler.index("moved = stats.get(")]
    assert "self.pending.add(" in handler


# ---------------------------------------------------------------------------
# Desktop's in-flight upload overwrote the re-parent
# ---------------------------------------------------------------------------


def _pdf(key, parent, version, md5="aa"):
    return {"key": key, "version": version,
            "data": {"itemType": "attachment", "contentType": "application/pdf",
                     "md5": md5, "parentItem": parent}}


def test_a_pdf_is_settled_only_with_md5_and_a_steady_version(monkeypatch) -> None:
    from fetchers.browser import connector

    monkeypatch.setattr(connector.time, "sleep", lambda s: None)
    zot = MagicMock()
    zot.cloud.children.return_value = [_pdf("P", "N", 5)]
    zot.cloud.item.side_effect = [_pdf("P", "N", 5)]
    assert connector._pdf_child_settled(zot, "N") is True
    zot.cloud.item.side_effect = [_pdf("P", "N", 6)]          # still moving
    assert connector._pdf_child_settled(zot, "N") is False
    zot.cloud.children.return_value = [_pdf("P", "N", 5, md5="")]
    assert connector._pdf_child_settled(zot, "N") is False


def test_a_merge_counts_only_if_the_pdf_stays_under_the_keeper(monkeypatch) -> None:
    import pytest
    from fetchers.browser import connector

    monkeypatch.setattr(connector.time, "sleep", lambda s: None)
    handler = connector.ZoteroConnectorHandler.__new__(connector.ZoteroConnectorHandler)
    handler.keep_extras = False
    zot = MagicMock()
    zot.merge_duplicate_item.return_value = {"moved": 1, "moved_pdf_keys": ["P"]}
    # Read on the merge's own surface (`parent_of`), not the cloud.
    zot.parent_of.side_effect = ["KEEPER", "KEEPER"]
    assert handler.merge_saved_item(zot, "KEEPER", "N")["moved_pdf_keys"] == ["P"]

    zot.parent_of.side_effect = ["KEEPER", "N"]  # overwritten
    with pytest.raises(connector.MergeNotVerified):
        handler.merge_saved_item(zot, "KEEPER", "N")


def test_settle_uses_the_settled_check(tmp_path, monkeypatch) -> None:
    from fetchers.browser import connector

    q = PendingMerges(tmp_path)
    q.add(keeper="K1", new_key="N1", doi="10.1/a")
    zot = MagicMock()
    zot.cloud.item.return_value = {"key": "N1"}
    monkeypatch.setattr(connector, "_pdf_child_settled", lambda zot, key: False)
    merge = MagicMock()
    settle_pending_merges(zot, q, merge=merge, wait_s=0, on_merged=lambda *a: None)
    merge.assert_not_called()


def test_restore_from_trash_patches_deleted_zero(monkeypatch) -> None:
    import zotero_io

    zc = zotero_io.ZoteroClient.__new__(zotero_io.ZoteroClient)
    cloud = MagicMock()
    cloud.endpoint, cloud.library_type, cloud.library_id = "https://api.zotero.org", "users", "5591"
    cloud.item.return_value = {"key": "G2FY5SZZ", "version": 41, "data": {"deleted": 1}}
    cloud.client.patch.return_value = MagicMock(status_code=204)
    monkeypatch.setattr(type(zc), "cloud", cloud, raising=False)
    zc.api_key = "k"
    assert zc.restore_from_trash("G2FY5SZZ") is True
    kwargs = cloud.client.patch.call_args.kwargs
    assert kwargs["content"] == '{"deleted": 0}'
    assert kwargs["headers"]["If-Unmodified-Since-Version"] == "41"


def test_trash_item_patches_deleted_one(monkeypatch) -> None:
    import zotero_io

    zc = zotero_io.ZoteroClient.__new__(zotero_io.ZoteroClient)
    cloud = MagicMock()
    cloud.endpoint, cloud.library_type, cloud.library_id = "https://api.zotero.org", "users", "5591"
    cloud.item.return_value = {"key": "OLD", "version": 9, "data": {}}
    cloud.client.patch.return_value = MagicMock(status_code=204)
    monkeypatch.setattr(type(zc), "cloud", cloud, raising=False)
    zc.api_key = "k"
    zc.trash_item("OLD")
    assert cloud.client.patch.call_args.kwargs["content"] == '{"deleted": 1}'
