"""ScienceDirect blocked the library proxy's IP mid-pass.

2026-09-23 19:22 UTC, JYU EZproxy: after ~55 Connector saves at one per
15-20 s, ScienceDirect served "There was a problem providing the content
you requested… Reference number a3fbeabfbd8fb606" in a second window.
Every save after it was metadata-only and sat in the pending queue. The
proxy IP is the whole institution's, so the pace itself is the hazard.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import MagicMock

import enrich_pdfs
from fetchers.browser import connector
from fetchers.browser.connector import (
    PendingMerges,
    host_family,
    publisher_block,
    settle_pending_merges,
)

BLOCK_TEXT = (
    "ScienceDirect\nThere was a problem providing the content you requested\n"
    "Please contact our support team for more information and provide the "
    "details below.\nReference number: a3fbeabfbd8fb606\nIP Address: "
    "130.234.10.199\nTimestamp: 2026-09-23 19:22:14 UTC"
)


def test_the_block_page_is_recognised_with_its_reference() -> None:
    assert publisher_block(BLOCK_TEXT) == ("Elsevier", "a3fbeabfbd8fb606")
    assert publisher_block("Journal of Business Venturing — Abstract") is None


def test_sciencedirect_and_linkinghub_share_one_clock() -> None:
    assert host_family("www.sciencedirect.com") == "elsevier"
    assert host_family("linkinghub.elsevier.com") == "elsevier"
    assert host_family("www.jstor.org") == "www.jstor.org"
    assert connector.HOST_MIN_INTERVAL_S["elsevier"] >= 60


class _Page:
    def __init__(self, text):
        self.text, self.closed = text, False

    async def evaluate(self, js):
        return self.text

    async def close(self):
        self.closed = True


def test_a_block_in_a_second_window_is_found_and_closed() -> None:
    main, popup = _Page("article"), _Page(BLOCK_TEXT)
    ctx = MagicMock()
    ctx.pages = [main, popup]
    found = asyncio.run(connector._find_block_page(ctx, main))
    assert found == ("Elsevier", "a3fbeabfbd8fb606")
    assert popup.closed and not main.closed


def test_a_blocked_family_is_not_tried_again_and_gets_no_access_verdict() -> None:
    src = inspect.getsource(connector.ZoteroConnectorHandler.download_and_attach)
    assert src.index("self._blocked_families") < src.index("page.goto(")
    assert src.index("HOST_MIN_INTERVAL_S") < src.index("page.goto(")
    assert src.count("_stop_if_blocked(") == 2          # before and after the save
    loop = inspect.getsource(enrich_pdfs._connector_item_loop)
    assert '"connector_publisher_blocked"' in loop
    assert "connector_publisher_blocked" in enrich_pdfs.pdf_run_report.STATUS_INFO


def test_pacing_waits_out_the_family_interval(monkeypatch) -> None:
    h = connector.ZoteroConnectorHandler.__new__(connector.ZoteroConnectorHandler)
    h._blocked_families, h._last_load_at = {}, {"elsevier": 1000.0}
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(connector.time, "monotonic", lambda: 1030.0)
    monkeypatch.setattr(connector.asyncio, "sleep", fake_sleep)
    h.last_outcome, h.last_error = "", ""

    class Stop(Exception):
        pass

    page = MagicMock()

    async def goto(*a, **k):
        raise Stop

    page.goto = goto
    counter = MagicMock()
    counter.done = 0
    item = {"doi": "10.1016/x", "title": "t", "item_key": "K",
            "resolver_target_url": "https://www.sciencedirect.com/science/article/pii/S1"}
    h._logged_out_proxies, h._skipped_hosts = set(), set()
    asyncio.run(h.download_and_attach(page, MagicMock(), MagicMock(), item,
                                      MagicMock(), counter=counter, total=1,
                                      t_start=0.0))
    assert slept and abs(slept[0] - 45.0) < 0.01


# ---------------------------------------------------------------------------
# Saves that never grow a PDF
# ---------------------------------------------------------------------------


def _html():
    return {"key": "S", "data": {"itemType": "attachment", "contentType": "text/html"}}


def test_a_local_pdfless_save_is_trashed_after_half_an_hour(tmp_path, monkeypatch) -> None:
    q = PendingMerges(tmp_path)
    q.add(keeper="K1", new_key="N1", doi="10.1016/a")
    zot = MagicMock()
    zot.local_writes_enabled = True
    surface = MagicMock()
    surface.item.return_value = {"key": "N1"}
    surface.children.return_value = [_html()]
    monkeypatch.setattr(connector, "_merge_surface", lambda z: surface)
    monkeypatch.setattr(connector, "_pdf_child_settled", lambda z, k: False)
    monkeypatch.setattr(connector, "_age_s", lambda row: 31 * 60)
    gave_up = []
    settle_pending_merges(zot, q, merge=MagicMock(), wait_s=0,
                          on_merged=lambda *a: None, on_given_up=gave_up.append)
    assert [r["new_key"] for r in gave_up] == ["N1"]
    zot.trash_item.assert_called_once_with("N1")
    assert q.keepers() == set()


def test_a_given_up_save_with_an_unsettled_pdf_is_not_trashed(tmp_path, monkeypatch) -> None:
    q = PendingMerges(tmp_path)
    q.add(keeper="K1", new_key="N1", doi="10.1016/a")
    zot = MagicMock()
    zot.local_writes_enabled = True
    surface = MagicMock()
    surface.item.return_value = {"key": "N1"}
    surface.children.return_value = [
        {"key": "P", "data": {"itemType": "attachment", "contentType": "application/pdf"}},
    ]
    monkeypatch.setattr(connector, "_merge_surface", lambda z: surface)
    monkeypatch.setattr(connector, "_pdf_child_settled", lambda z, k: False)
    monkeypatch.setattr(connector, "_age_s", lambda row: 31 * 60)
    settle_pending_merges(zot, q, merge=MagicMock(), wait_s=0,
                          on_merged=lambda *a: None, on_given_up=lambda r: None)
    zot.trash_item.assert_not_called()


def test_the_cloud_path_keeps_the_long_grace(tmp_path, monkeypatch) -> None:
    q = PendingMerges(tmp_path)
    q.add(keeper="K1", new_key="N1", doi="10.1016/a")
    zot = MagicMock()                      # local_writes_enabled is not True
    zot.cloud.item.return_value = {"key": "N1"}
    zot.cloud.children.return_value = [_html()]
    monkeypatch.setattr(connector, "_pdf_child_settled", lambda z, k: False)
    monkeypatch.setattr(connector, "_age_s", lambda row: 31 * 60)
    settle_pending_merges(zot, q, merge=MagicMock(), wait_s=0,
                          on_merged=lambda *a: None, on_given_up=lambda r: None)
    assert q.keepers() == {"K1"}


def test_the_driver_logs_given_up_saves_as_no_pdf_offered() -> None:
    src = inspect.getsource(enrich_pdfs)
    assert "on_given_up=_on_given_up" in src
    body = src[src.index("def _on_given_up"):src.index("def _settle")]
    assert "NO_PDF_OFFERED" in body and "connector_offered_nothing" in body
