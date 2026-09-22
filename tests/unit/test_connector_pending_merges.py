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


def test_settle_merges_what_has_synced_and_keeps_the_rest(tmp_path) -> None:
    q = PendingMerges(tmp_path)
    q.add(keeper="K1", new_key="SYNCED", doi="10.1/a")
    q.add(keeper="K2", new_key="LAGGING", doi="10.1/b")
    zot = MagicMock()
    zot.cloud.item.side_effect = lambda key: (
        {"key": key} if key == "SYNCED" else (_ for _ in ()).throw(RuntimeError("404"))
    )
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
