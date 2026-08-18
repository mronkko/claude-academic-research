"""The setup prompt waits for evidence that a human is needed.

`_drive_handler` used to run `handler.setup()` — the "can you see/reach
the PDF from this page?" question — before trying anything. Several
publishers authenticate on institutional IP, so that question often
guards a wall that is not there: a user driving eight publisher blocks
answers it eight times and it does nothing seven of them. Reported as
"I have just said Y to most prompts."

So the order is inverted: try the first item, and treat its failure as
the evidence. On that failure the setup step opens, the human signs in
or clears a challenge, and the item is retried once before the failure
is taken at face value.

Single-lane only. With parallel tabs the solve has to land on lane 0
before the others open, or they hit the login page simultaneously with
no session to inherit — the constraint the `needs_solve` block already
documents.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import enrich_pdfs
import pytest


class _FakePage:
    def __init__(self, label: str = "p") -> None:
        self.label = label

    async def close(self) -> None:
        pass


class _FakeContext:
    def __init__(self) -> None:
        self.pages = [_FakePage("setup")]

    async def new_page(self) -> _FakePage:
        return _FakePage("lane")

    async def close(self) -> None:
        pass


class _FakePlaywright:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


class _Handler:
    """Fails until `setup()` runs, then succeeds — a login wall."""

    name = "fake"
    display_name = "Fake Publisher"
    doi_prefixes = ()
    delay_s = 0.0
    attaches_directly = False
    needs_interactive_solve = True

    def __init__(self, *, concurrency: int = 1, setup_result: str = "proceed",
                 succeeds_after_setup: bool = True) -> None:
        self.concurrency = concurrency
        self.last_error = ""
        self.setup_calls: list[str] = []
        self.download_calls: list[str] = []
        self._setup_result = setup_result
        self._succeeds_after_setup = succeeds_after_setup

    async def setup(self, page, doi):
        self.setup_calls.append(doi)
        return self._setup_result

    async def download(self, page, ctx, item, cache_dir, *,
                       counter, total, t_start):
        self.download_calls.append(item["doi"])
        if self.setup_calls and self._succeeds_after_setup:
            counter.ok += 1
            return Path(cache_dir) / "x.pdf", "https://example/pdf"
        self.last_error = "no PDF link"
        counter.failed += 1
        return None


class _Rows:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def writerow(self, row: dict) -> None:
        self.rows.append(row)


def _args(tmp_path, lanes: int = 1) -> argparse.Namespace:
    return argparse.Namespace(
        cache_dir=str(tmp_path), dry_run=True, no_check_text=True,
        failure_log_csv="", browser_workers=lanes,
        on_first_failure="skip", no_prompt=True, plan=False, control_file="",
    )


def _items(n: int) -> list[dict]:
    return [
        {"doi": f"10.1/{i}", "item_key": "", "title": f"T{i}",
         "item_type": "journalArticle"}
        for i in range(n)
    ]


@pytest.fixture
def fake_browser(monkeypatch):
    ctx = _FakeContext()
    import fetchers.browser as browser_pkg
    import playwright.async_api as pw

    async def _launch(_p, _cache_dir, **_kw):
        return ctx

    monkeypatch.setattr(browser_pkg, "launch_context", _launch)
    monkeypatch.setattr(pw, "async_playwright", lambda: _FakePlaywright())
    return ctx


def _drive(handler, items, args, rows, **kw):
    asyncio.run(enrich_pdfs._drive_handler(
        handler, items, object(), rows, args, "2026-08-18", **kw,
    ))


def test_nothing_is_asked_before_the_first_attempt(
    fake_browser, tmp_path,
) -> None:
    """The whole point: a publisher that just works is never asked about."""
    handler = _Handler()
    handler._succeeds_after_setup = True
    # Succeeds immediately — no setup has run, so make download succeed
    # on its own by pre-marking setup as unnecessary.
    handler.setup_calls.append("pre")          # simulate "already fine"
    _drive(handler, _items(3), _args(tmp_path), _Rows(), defer_solve=True)

    assert handler.setup_calls == ["pre"], "setup ran despite downloads working"
    assert len(handler.download_calls) == 3


def test_the_first_failure_opens_setup_and_retries_that_item(
    fake_browser, tmp_path,
) -> None:
    """The failure is the evidence. The item that paid for it gets
    another go, rather than being spent on the discovery."""
    handler = _Handler()
    _drive(handler, _items(3), _args(tmp_path), _Rows(), defer_solve=True)

    assert len(handler.setup_calls) == 1, handler.setup_calls
    # item 0 tried, failed, setup, retried — then items 1 and 2.
    assert handler.download_calls[:2] == ["10.1/0", "10.1/0"], (
        f"the failed item was not retried: {handler.download_calls}"
    )


def test_setup_is_offered_once_not_once_per_item(
    fake_browser, tmp_path,
) -> None:
    """A publisher that stays broken must not re-ask on every item."""
    handler = _Handler(succeeds_after_setup=False)
    _drive(handler, _items(4), _args(tmp_path), _Rows(), defer_solve=True)

    assert len(handler.setup_calls) == 1, handler.setup_calls


def test_declining_at_the_deferred_prompt_skips_the_publisher(
    fake_browser, tmp_path,
) -> None:
    handler = _Handler(setup_result="skip", succeeds_after_setup=False)
    _drive(handler, _items(5), _args(tmp_path), _Rows(), defer_solve=True)

    assert len(handler.setup_calls) == 1
    assert len(handler.download_calls) < 5, (
        "kept going after the user declined"
    )


def test_parallel_lanes_still_solve_upfront(fake_browser, tmp_path) -> None:
    """Not a simplification: with several tabs the solve must land on
    lane 0 before the others open, or they all hit the login page at
    once with no session to inherit."""
    handler = _Handler(concurrency=4)
    _drive(handler, _items(4), _args(tmp_path, lanes=4), _Rows(),
           defer_solve=True)

    assert handler.setup_calls, "deferred the solve on a multi-lane run"
    assert handler.setup_calls[0] == "10.1/0"


def test_deferral_is_opt_in(fake_browser, tmp_path) -> None:
    """Callers that have not been reviewed for it keep the old order."""
    handler = _Handler()
    _drive(handler, _items(2), _args(tmp_path), _Rows())

    assert handler.setup_calls, "defer_solve defaulted to on"
