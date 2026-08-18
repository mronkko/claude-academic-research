"""A Connector bail-out must not erase what an earlier pass found.

Two passes can look at the same item in one run: EBSCOhost drives its
resolver target first, and whatever it fails to attach is handed to the
Zotero Connector as a second chance (`on_failure="retry_bucket"`). Five
pre-flights can then stop the Connector before it tries anything, and
each writes one row per item saying why *the pass* stopped.

Live, on 2026-08-17, that overwrote the better answer. Two EBSCO items
had earned real verdicts — an unconfirmed no-match and a positively
located `unique_record` — and both appeared in `pdf_attach_log.csv` as
`connector_setup_failed`, whose advertised lever is "re-run the
Connector pass". Read alone, the row says the item was never looked at.
The finding survived only in `pdf_fetch_log.csv` under `cause`: two logs
disagreeing about one item, with the less informed one printed in the
run report.

The fix keeps `status` honest about the Connector — it really did not
run — and puts the earlier pass's answer in `detail`, which
`pdf_run_report` prints directly beneath the item.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import enrich_pdfs
import pdf_fetch_log
import pytest

# --- fakes ------------------------------------------------------------


class _FakePage:
    async def close(self) -> None:
        pass


class _FakeContext:
    def __init__(self) -> None:
        self.pages = [_FakePage()]
        self.closed = False

    async def new_page(self) -> _FakePage:
        return _FakePage()

    async def close(self) -> None:
        self.closed = True


class _FakePlaywright:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakeEbsco:
    """Fails every item, carrying the verdict a live EBSCO run carried."""

    name = "ebsco"
    display_name = "EBSCOhost"
    doi_prefixes = ()
    delay_s = 0.0
    concurrency = 1
    attaches_directly = False
    needs_interactive_solve = False

    def __init__(self, verdict: str) -> None:
        self.last_error = ""
        self.last_verdict = verdict

    async def setup(self, page, doi):  # pragma: no cover - not reached
        return "proceed"

    async def download(self, page, ctx, item, cache_dir, *,
                       counter, total, t_start):
        counter.failed += 1
        return None


class _DecliningConnector:
    """Stands in at the fourth pre-flight: setup does not return "proceed"."""

    name = "connector"
    display_name = "Zotero Connector"
    delay_s = 0.0
    concurrency = 1
    attaches_directly = True
    needs_interactive_solve = True
    extension_path = "/fake/connector"

    async def setup(self, page, doi):
        return "skip"


class _Rows:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def writerow(self, row: dict) -> None:
        self.rows.append(row)


def _args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        cache_dir=str(tmp_path), dry_run=False, no_check_text=True,
        failure_log_csv="", browser_workers=1, on_first_failure="skip",
        no_prompt=True, plan=False, control_file="",
    )


def _item(doi: str = "10.1515/bejeap-2018-0149") -> dict:
    return {"doi": doi, "item_key": "K1", "title": "T",
            "item_type": "journalArticle",
            "resolver_target_url": "https://resolver.example/x"}


@pytest.fixture
def fake_browser(monkeypatch):
    """Swap Playwright and the Zotero pre-flights out from under the driver."""
    ctx = _FakeContext()
    import fetchers.browser as browser_pkg
    import playwright.async_api as pw

    async def _launch(_p, _cache_dir, **_kw):
        return ctx

    async def _sw(_ctx, timeout_s=15):  # pragma: no cover - not reached
        return object()

    monkeypatch.setattr(browser_pkg, "launch_context", _launch)
    monkeypatch.setattr(browser_pkg, "ping_zotero_desktop", lambda _s: True)
    monkeypatch.setattr(browser_pkg, "wait_for_service_worker", _sw)
    monkeypatch.setattr(pw, "async_playwright", lambda: _FakePlaywright())
    return ctx


def _zot():
    # `selected_local_library() is None` takes the "could not determine"
    # warn branch, which is the one that does not bail out itself.
    return SimpleNamespace(
        selected_local_library=lambda: None,
        library_type="group",
        group_name=lambda: "G",
        group_id=1,
        describe_library=lambda: "group 1",
    )


# --- the hand-off ------------------------------------------------------


def test_the_retry_bucket_carries_the_verdict_that_earned_it(
    tmp_path, monkeypatch,
) -> None:
    """`_drive_handler` hands on items, not a report — so the answer has
    to travel on the item or it does not travel at all."""
    import fetchers.browser as browser_pkg
    import playwright.async_api as pw

    ctx = _FakeContext()

    async def _launch(_p, _cache_dir, **_kw):
        return ctx

    monkeypatch.setattr(browser_pkg, "launch_context", _launch)
    monkeypatch.setattr(pw, "async_playwright", lambda: _FakePlaywright())

    bucket: list[dict] = []
    asyncio.run(enrich_pdfs._drive_handler(
        _FakeEbsco("unique_record"), [_item()], object(), _Rows(),
        _args(tmp_path), "2026-08-18",
        on_failure="retry_bucket", retry_bucket=bucket,
    ))

    assert len(bucket) == 1
    note = bucket[0][enrich_pdfs.PRIOR_ATTEMPT_KEY]
    assert "ebsco" in note and "unique_record" in note
    assert pdf_fetch_log.FailureCause.ACCESS_BLOCKED.value in note


def test_a_declined_connector_keeps_the_earlier_answer(
    fake_browser, tmp_path, monkeypatch,
) -> None:
    """The live defect. `status` may say the Connector never started;
    it may not be the only thing the row says about the item."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    rows = _Rows()
    carried = enrich_pdfs._carrying_prior_attempt(
        _item(), _FakeEbsco("no_exact_match_unconfirmed"),
        pdf_fetch_log.FailureCause.ACCESS_BLOCKED,
    )

    asyncio.run(enrich_pdfs._drive_connector(
        _DecliningConnector(), [carried], _zot(), rows,
        _args(tmp_path), "2026-08-18",
    ))

    assert len(rows.rows) == 1
    row = rows.rows[0]
    assert row["status"] == "connector_setup_failed"
    assert "no_exact_match_unconfirmed" in row["detail"]
    assert pdf_fetch_log.FailureCause.ACCESS_BLOCKED.value in row["detail"]


def test_an_item_with_no_earlier_pass_gets_no_invented_detail(
    fake_browser, tmp_path, monkeypatch,
) -> None:
    """The ~175 items that reach the Connector queue fresh really were
    never looked at. `status` is their whole story, and a `detail` here
    would be a claim about an attempt nobody made."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    rows = _Rows()

    asyncio.run(enrich_pdfs._drive_connector(
        _DecliningConnector(), [_item()], _zot(), rows,
        _args(tmp_path), "2026-08-18",
    ))

    assert rows.rows[0]["detail"] == ""


def test_the_report_prints_the_carried_answer_under_the_item() -> None:
    """`detail` is only worth writing because the run report renders it.

    Pins the seam rather than trusting it: the whole point of the fix is
    that a human reading the report sees both facts.
    """
    import pdf_run_report

    text = pdf_run_report.format_report([{
        "run_date": "2026-08-18", "item_key": "K1",
        "doi": "10.1515/bejeap-2018-0149", "title": "T",
        "status": "connector_setup_failed", "source": "connector",
        "detail": "ebsco: unique_record (ACCESS_BLOCKED)",
    }])
    assert "connector_setup_failed" in text
    assert "ebsco: unique_record (ACCESS_BLOCKED)" in text
