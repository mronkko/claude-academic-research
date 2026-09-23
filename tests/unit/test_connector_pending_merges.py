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
    zc.api_key, zc.prefer_local, zc.local_api_key = "k", True, None
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
    zc.api_key, zc.prefer_local, zc.local_api_key = "k", True, None
    zc.trash_item("OLD")
    assert cloud.client.patch.call_args.kwargs["content"] == '{"deleted": 1}'


def test_with_a_local_key_the_trash_goes_through_desktop(monkeypatch) -> None:
    """Verified live on 2026-09-23 against a scratch note: 204 locally,
    `deleted` read back in Desktop, `deleted: 1` on the Web API after
    sync. Two details are load-bearing. `_write` adds the Server-ID and
    local-key headers (428/401 without them). The explicit Content-Type
    avoids the local API's "400 Empty request body". The version comes
    from Desktop too.
    """
    import zotero_io

    zc = zotero_io.ZoteroClient(api_key="k", group_id="1", local_api_key="LK")
    local, cloud = MagicMock(), MagicMock()
    zc._local, zc._cloud = local, cloud
    local.endpoint, local.library_type, local.library_id = "http://localhost:23119/api", "groups", "1"
    local.item.return_value = {"key": "OLD", "version": 31036, "data": {}}
    local._write.return_value = MagicMock(status_code=204)

    zc.trash_item("OLD")

    method = local._write.call_args.args[0]
    kwargs = local._write.call_args.kwargs
    assert method == "PATCH"
    assert kwargs["content"] == '{"deleted": 1}'
    assert kwargs["headers"]["If-Unmodified-Since-Version"] == "31036"
    assert kwargs["headers"]["Content-Type"] == "application/json"
    cloud.item.assert_not_called()
    cloud.client.patch.assert_not_called()


def _local_zot():
    """A client with local writes on. `local_writes_enabled` must be the
    real `True`: `_merges_locally` ignores a MagicMock's truthy stand-in."""
    zot = MagicMock()
    zot.local_writes_enabled = True
    zot.cloud.item.side_effect = AssertionError("cloud must not be read")
    zot.cloud.children.side_effect = AssertionError("cloud must not be read")
    return zot


def test_with_local_writes_the_waits_poll_desktop_not_the_cloud(monkeypatch) -> None:
    """Live on 2026-09-23 the cloud lagged a Connector save by 167-208 s
    while the PDF was on the local API within 6-14 s, md5 set. The 30 s
    cloud wait queued every item even though the merge (re-parent and
    trash) no longer touches the cloud.
    """
    from fetchers.browser import connector
    from fetchers.browser.connector import (
        _pdf_child_settled,
        _wait_for_child_attachment,
        _wait_for_cloud_sync,
    )

    monkeypatch.setattr(connector.time, "sleep", lambda s: None)
    zot = _local_zot()
    zot.local.item.side_effect = lambda k: (
        _pdf_child() if k == "P1" else {"key": k, "version": 1, "data": {}}
    )
    zot.local.children.return_value = [_pdf_child()]
    assert _wait_for_cloud_sync(zot, "N", 0.1) is True
    assert _pdf_child_settled(zot, "N", stable_s=0) is True
    assert _wait_for_child_attachment(zot, "N", 0.1) is True


def test_with_local_writes_the_queue_settles_from_desktop(
    tmp_path, monkeypatch,
) -> None:
    from fetchers.browser import connector

    zot = _local_zot()
    zot.local.item.return_value = {"key": "N1"}
    monkeypatch.setattr(connector, "_pdf_child_settled", lambda z, k: True)
    q = PendingMerges(tmp_path)
    q.add(keeper="K", new_key="N1", doi="10.1/a")
    merge = MagicMock(return_value={"moved": 1})
    connector.settle_pending_merges(
        zot, q, merge=merge, wait_s=0, on_merged=lambda *a: None,
    )
    merge.assert_called_once_with("K", "N1")


# ---------------------------------------------------------------------------
# BackgroundMerger — the post-save work runs while the next page loads
# ---------------------------------------------------------------------------


def _bg(tmp_path, monkeypatch, *, child_ok=True, merge=None):
    from fetchers.browser import connector

    monkeypatch.setattr(connector, "_wait_for_child_attachment",
                        lambda zot, key, t: child_ok)
    q = PendingMerges(tmp_path)
    done = []
    m = connector.BackgroundMerger(
        MagicMock(), q,
        merge=merge or MagicMock(return_value={"moved": 1, "moved_pdf_keys": ["P"]}),
        on_done=lambda row, outcome, stats: done.append((row["new_key"], outcome)),
        wait_s=1,
    )
    return m, q, done


def _submit(m, q, new_key="N1", keeper="K1"):
    # The handler's order: queue first, then submit.
    q.add(keeper=keeper, new_key=new_key, doi="10.1/a")
    m.submit({"keeper": keeper, "new_key": new_key, "doi": "10.1/a"})


def test_a_verified_background_merge_leaves_the_queue(tmp_path, monkeypatch) -> None:
    m, q, done = _bg(tmp_path, monkeypatch)
    _submit(m, q)
    m.drain()
    assert done == [("N1", "merged")]
    assert PendingMerges(tmp_path).rows() == []


def test_a_failed_background_merge_stays_queued(tmp_path, monkeypatch) -> None:
    """Never a lost PDF: MergeNotVerified (or any error) keeps the pair
    on disk for the end-of-pass sweep, and --replace is not told to
    delete anything."""
    from fetchers.browser.connector import MergeNotVerified

    m, q, done = _bg(tmp_path, monkeypatch,
                     merge=MagicMock(side_effect=MergeNotVerified("moved back")))
    _submit(m, q)
    m.drain()
    assert done == [("N1", "pending")]
    assert PendingMerges(tmp_path).new_keys() == {"N1"}


def test_no_settled_pdf_in_time_stays_queued(tmp_path, monkeypatch) -> None:
    merge = MagicMock()
    m, q, done = _bg(tmp_path, monkeypatch, child_ok=False, merge=merge)
    _submit(m, q)
    m.drain()
    merge.assert_not_called()
    assert done == [("N1", "pending")]
    assert PendingMerges(tmp_path).new_keys() == {"N1"}


def test_a_queued_save_is_excluded_from_the_next_poll_at_once(
    tmp_path, monkeypatch,
) -> None:
    """The next item's `_poll_for_new_item` gets `pending.new_keys()` as
    `exclude`. So the pair must be in the queue before the merge starts,
    not only after it fails."""
    import threading

    gate = threading.Event()
    merge = MagicMock(side_effect=lambda k, n: (gate.wait(5), {"moved": 1})[1])
    m, q, done = _bg(tmp_path, monkeypatch, merge=merge)
    _submit(m, q)
    assert "N1" in q.new_keys()          # merge still in flight
    gate.set()
    m.drain()
    assert done == [("N1", "merged")]


def test_one_worker_serialises_merges(tmp_path, monkeypatch) -> None:
    import threading
    import time as _time

    active, peak = [0], [0]
    lock = threading.Lock()

    def merge(keeper, new):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        _time.sleep(0.02)
        with lock:
            active[0] -= 1
        return {"moved": 1}

    m, q, done = _bg(tmp_path, monkeypatch, merge=merge)
    for i in range(4):
        _submit(m, q, new_key=f"N{i}", keeper=f"K{i}")
    m.drain()
    assert peak[0] == 1
    assert [o for _, o in done] == ["merged"] * 4


def test_a_local_merge_reads_back_once(monkeypatch) -> None:
    """The 2 x 5 s read-back was sized for the cloud re-parent that
    Desktop's pending push overwrote. A re-parent made in Desktop has no
    push behind it, so one read after 2 s is kept and 8 s per item saved."""
    from fetchers.browser import connector

    sleeps = []
    monkeypatch.setattr(connector.time, "sleep", sleeps.append)
    h = connector.ZoteroConnectorHandler.__new__(connector.ZoteroConnectorHandler)
    h.keep_extras = False
    zot = MagicMock()
    zot.merge_duplicate_item.return_value = {"moved_pdf_keys": ["P"]}
    zot.parent_of.return_value = "K"

    zot.local_writes_enabled = True
    h.merge_saved_item(zot, "K", "N")
    assert sleeps == [2.0]

    sleeps.clear()
    zot.local_writes_enabled = False
    h.merge_saved_item(zot, "K", "N")
    assert sleeps == [5.0, 5.0]


def test_an_empty_save_is_retired_when_its_keeper_has_a_pdf(tmp_path) -> None:
    """WKC6FD4U and G5S5R2L7 (2026-09-23): empty saves whose keepers had
    PDFs from elsewhere sat queued, silently, and barred the keepers."""
    q = PendingMerges(tmp_path)
    q.add(keeper="K1", new_key="EMPTY", doi="10.1/a")
    zot = MagicMock()
    zot.local_writes_enabled = True
    zot.local.item.return_value = {"key": "EMPTY"}
    zot.local.children.return_value = []
    merge = MagicMock()
    settle_pending_merges(
        zot, q, merge=merge, wait_s=0, on_merged=lambda *a: None,
        keeper_has_pdf=lambda keeper: True,
    )
    merge.assert_not_called()
    zot.trash_item.assert_called_once_with("EMPTY")
    assert PendingMerges(tmp_path).rows() == []

    # A save with a PDF child still uploading is not retired.
    q.add(keeper="K1", new_key="UPLOADING", doi="10.1/a")
    zot.local.children.return_value = [_pdf_child()]
    zot.local.item.side_effect = lambda k: {"key": k, "version": 1, "data": {}}
    settle_pending_merges(
        zot, q, merge=merge, wait_s=0, on_merged=lambda *a: None,
        keeper_has_pdf=lambda keeper: True,
    )
    assert PendingMerges(tmp_path).new_keys() == {"UPLOADING"}
