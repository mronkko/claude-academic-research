"""`_drive_handler` under concurrency, driven against fake Playwright.

`test_browser_lanes.py` pins the pieces; this drives the loop that uses
them. The failure modes worth catching here are the ones a live run
would report as success: an item handed to two lanes and attached twice,
an item silently dropped, or an outage that keeps shredding the queue
because only the lane that noticed it stopped.

Playwright is faked rather than launched — a real context would open a
window and want a profile. What is *not* faked is `_drive_handler`
itself, so the claim/dispatch path under test is the shipped one.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import enrich_pdfs
import pytest
from fetchers.browser.base import NetworkOutage

# --- fakes ------------------------------------------------------------


class _FakePage:
    def __init__(self, label: str) -> None:
        self.label = label
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeContext:
    def __init__(self) -> None:
        self.pages = [_FakePage("setup")]
        self.extra_pages: list[_FakePage] = []
        self.closed = False

    async def new_page(self) -> _FakePage:
        page = _FakePage(f"lane{len(self.extra_pages) + 1}")
        self.extra_pages.append(page)
        return page

    async def close(self) -> None:
        self.closed = True


class _FakePlaywright:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakeHandler:
    """Records what each lane saw, and how many ran at once.

    `state` is deliberately one shared mutable object: `_drive_handler`
    gives each lane its own handler via `copy.copy`, which is a shallow
    copy, so every copy observes the same dict. That is also what makes
    `last_error` per-instance, which is the point of copying at all.
    """

    name = "fake"
    display_name = "Fake Publisher"
    doi_prefixes = ()
    delay_s = 0.0
    attaches_directly = False
    needs_interactive_solve = False

    def __init__(self, state: dict, *, concurrency: int, fail: bool = False,
                 error: str = "") -> None:
        self.state = state
        self.concurrency = concurrency
        self._fail = fail
        self._error = error
        self.last_error = ""

    async def setup(self, page, doi):  # pragma: no cover - not reached
        return "proceed"

    async def download(self, page, ctx, item, cache_dir, *,
                       counter, total, t_start):
        self.last_error = ""
        self.state["in_flight"] += 1
        self.state["peak"] = max(self.state["peak"], self.state["in_flight"])
        self.state["seen"].append(item["doi"])
        self.state["by_page"].setdefault(page.label, []).append(item["doi"])
        try:
            await asyncio.sleep(0.01)
            if self._fail:
                self.last_error = self._error
                counter.failed += 1
                return None
            counter.ok += 1
            return Path(cache_dir) / f"{item['doi']}.pdf", "https://example/pdf"
        finally:
            self.state["in_flight"] -= 1


class _Rows:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def writerow(self, row: dict) -> None:
        self.rows.append(row)


def _state() -> dict:
    return {"in_flight": 0, "peak": 0, "seen": [], "by_page": {}}


def _args(tmp_path, lanes: int) -> argparse.Namespace:
    return argparse.Namespace(
        cache_dir=str(tmp_path), dry_run=False, no_check_text=True,
        failure_log_csv="", browser_workers=lanes, on_first_failure="skip",
        no_prompt=True, plan=False, control_file="",
    )


def _items(n: int) -> list[dict]:
    # Empty `item_key` keeps the success path short of the Zotero
    # upload — the dispatch is what is under test, not the attach.
    return [
        {"doi": f"10.1/{i}", "item_key": "", "title": f"T{i}",
         "item_type": "journalArticle"}
        for i in range(n)
    ]


@pytest.fixture
def fake_browser(monkeypatch):
    """Swap Playwright out from under `_drive_handler`'s local imports."""
    ctx = _FakeContext()
    import fetchers.browser as browser_pkg
    import playwright.async_api as pw

    async def _launch(_p, _cache_dir, **_kw):
        return ctx

    monkeypatch.setattr(browser_pkg, "launch_context", _launch)
    monkeypatch.setattr(pw, "async_playwright", lambda: _FakePlaywright())
    return ctx


def _drive(handler, items, args, rows) -> None:
    asyncio.run(enrich_pdfs._drive_handler(
        handler, items, object(), rows, args, "2026-08-17",
    ))


# --- dispatch ---------------------------------------------------------


def test_every_item_is_processed_exactly_once(fake_browser, tmp_path) -> None:
    """The claim must be atomic. Two lanes taking the same item means a
    PDF attached twice; a skipped index means an article silently
    dropped from the run with no row to show for it."""
    state = _state()
    items = _items(20)
    rows = _Rows()

    _drive(_FakeHandler(state, concurrency=4), items, _args(tmp_path, 4), rows)

    assert sorted(state["seen"]) == sorted(it["doi"] for it in items)
    assert len(state["seen"]) == 20
    assert len(rows.rows) == 20


def test_lanes_actually_run_in_parallel(fake_browser, tmp_path) -> None:
    state = _state()

    _drive(_FakeHandler(state, concurrency=4), _items(20),
           _args(tmp_path, 4), _Rows())

    assert state["peak"] == 4


def test_each_lane_drives_its_own_page(fake_browser, tmp_path) -> None:
    """Concurrent downloads on one shared page would cross-talk — the
    EBSCO handler attaches a response listener to the page it was given
    and reads whatever that page fetches."""
    state = _state()

    _drive(_FakeHandler(state, concurrency=4), _items(20),
           _args(tmp_path, 4), _Rows())

    assert len(state["by_page"]) == 4
    assert "setup" in state["by_page"], "lane 0 must reuse the solved page"


def test_extra_tabs_are_closed_after_the_run(fake_browser, tmp_path) -> None:
    _drive(_FakeHandler(_state(), concurrency=4), _items(8),
           _args(tmp_path, 4), _Rows())

    assert [p.closed for p in fake_browser.extra_pages] == [True] * 3


# --- the serial path is unchanged -------------------------------------


def test_one_lane_opens_no_extra_tabs(fake_browser, tmp_path) -> None:
    """At `--browser-workers 1` this must behave exactly as the serial
    loop it replaced: one page, the one `setup()` was solved on."""
    state = _state()

    _drive(_FakeHandler(state, concurrency=1), _items(6),
           _args(tmp_path, 1), _Rows())

    assert fake_browser.extra_pages == []
    assert list(state["by_page"]) == ["setup"]
    assert state["peak"] == 1


def test_one_lane_preserves_queue_order(fake_browser, tmp_path) -> None:
    state = _state()
    items = _items(6)

    _drive(_FakeHandler(state, concurrency=1), items,
           _args(tmp_path, 1), _Rows())

    assert state["seen"] == [it["doi"] for it in items]


def test_a_publisher_ceiling_of_one_survives_a_high_flag(
    fake_browser, tmp_path,
) -> None:
    state = _state()

    _drive(_FakeHandler(state, concurrency=1), _items(6),
           _args(tmp_path, 10), _Rows())

    assert state["peak"] == 1
    assert fake_browser.extra_pages == []


# --- the outage breaker -----------------------------------------------


def test_an_outage_stops_every_lane_not_just_the_one_that_saw_it(
    fake_browser, tmp_path,
) -> None:
    """The breaker exists because a live run lost the network for four
    minutes and burned 193 items at ~1.2 s each. Under concurrency that
    is four times worse if only the noticing lane stops."""
    state = _state()
    handler = _FakeHandler(
        state, concurrency=4, fail=True, error="net::ERR_INTERNET_DISCONNECTED",
    )
    rows = _Rows()

    with pytest.raises(NetworkOutage):
        _drive(handler, _items(200), _args(tmp_path, 4), rows)

    # Threshold is 5; four lanes in flight can overshoot by at most the
    # three that were already running. Nowhere near 200.
    assert len(state["seen"]) <= enrich_pdfs._OUTAGE_THRESHOLD + 4


def test_un_attempted_items_are_left_unlogged_after_an_outage(
    fake_browser, tmp_path,
) -> None:
    """What makes them re-runnable. A row saying "no PDF" for an article
    no server was ever asked about is the failure this whole mechanism
    exists to prevent."""
    state = _state()
    handler = _FakeHandler(
        state, concurrency=4, fail=True, error="net::ERR_NAME_NOT_RESOLVED",
    )
    rows = _Rows()
    bucket: list[dict] = []

    with pytest.raises(NetworkOutage):
        asyncio.run(enrich_pdfs._drive_handler(
            handler, _items(200), object(), rows, _args(tmp_path, 4),
            "2026-08-17", on_failure="retry_bucket", retry_bucket=bucket,
        ))

    assert len(bucket) <= len(state["seen"])
    assert len(bucket) < 200


def test_a_publisher_failure_is_not_mistaken_for_an_outage(
    fake_browser, tmp_path,
) -> None:
    """`last_error` is per-lane instance state. Sharing one handler
    across lanes would let one lane read another's reason and file a
    plain "no PDF here" as a network outage, or the reverse."""
    state = _state()
    handler = _FakeHandler(state, concurrency=4, fail=True, error="no PDF link")
    bucket: list[dict] = []

    asyncio.run(enrich_pdfs._drive_handler(
        handler, _items(30), object(), _Rows(), _args(tmp_path, 4),
        "2026-08-17", on_failure="retry_bucket", retry_bucket=bucket,
    ))

    assert len(state["seen"]) == 30
    assert len(bucket) == 30
