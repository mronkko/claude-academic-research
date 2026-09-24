"""Ctrl-C during a browser/Connector pass must end the run.

Reported 2026-09-23 and 2026-09-24: SIGINT to the Python process during
the Connector stage was ignored, and only SIGTERM stopped it. `asyncio.run`
does cancel the main task on SIGINT, but it then waits for every
default-executor thread before raising KeyboardInterrupt, and a prompt
thread blocked on /dev/tty never returns.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import threading
import time
from pathlib import Path

import enrich_pdfs
from fetchers.browser import base, connector
from fetchers.browser.connector import BackgroundMerger
from fetchers.browser.interaction import ask_in_daemon


def test_ask_in_daemon_returns_the_answer_and_raises_the_error() -> None:
    assert asyncio.run(ask_in_daemon(lambda p: p.upper(), "y")) == "Y"

    def boom():
        raise ValueError("no tty")

    try:
        asyncio.run(ask_in_daemon(boom))
    except ValueError as e:
        assert str(e) == "no tty"
    else:
        raise AssertionError("the error was swallowed")


def test_a_cancelled_prompt_does_not_hold_up_the_exit() -> None:
    release = threading.Event()

    async def main():
        task = asyncio.ensure_future(ask_in_daemon(release.wait, 30))
        await asyncio.sleep(0.1)
        task.cancel()                     # what asyncio.run does on SIGINT
        try:
            await task
        except asyncio.CancelledError:
            pass

    t0 = time.monotonic()
    asyncio.run(main())                   # would wait out the prompt with to_thread
    elapsed = time.monotonic() - t0
    release.set()
    assert elapsed < 2.0


def test_no_prompt_runs_in_the_default_executor() -> None:
    root = Path(enrich_pdfs.__file__).parent
    offenders = []
    for path in [root / "enrich_pdfs.py", *sorted((root / "fetchers" / "browser").glob("*.py"))]:
        src = path.read_text(encoding="utf-8")
        for m in re.finditer(
            r"to_thread\(\s*(_read_user_line|_wait_for_user|_prompt_on_first_failure)",
            src,
        ):
            offenders.append(f"{path.name}: {m.group(1)}")
    assert offenders == []
    assert base.ask_in_daemon is ask_in_daemon


def test_abandon_drops_queued_merges(tmp_path, monkeypatch) -> None:
    started, release = threading.Event(), threading.Event()
    ran = []

    def merge(keeper, new):
        ran.append(keeper)
        started.set()
        release.wait(5)
        return {"moved": 1}

    q = connector.PendingMerges(tmp_path)
    m = BackgroundMerger(object(), q, merge=merge, on_done=lambda *a: None,
                         wait_s=0)
    monkeypatch.setattr(connector, "_wait_for_child_attachment",
                        lambda *a, **k: True)
    try:
        m.submit({"keeper": "K1", "new_key": "N1", "doi": ""})
        m.submit({"keeper": "K2", "new_key": "N2", "doi": ""})
        assert started.wait(2)
        m.abandon()
        release.set()
        m._pool.shutdown(wait=True)
    finally:
        release.set()
    assert ran == ["K1"]


def test_the_connector_driver_abandons_merges_and_reports_the_queue() -> None:
    drive = inspect.getsource(enrich_pdfs._drive_connector)
    assert "except asyncio.CancelledError" in drive and "merger.abandon()" in drive
    src = inspect.getsource(enrich_pdfs)
    assert "except KeyboardInterrupt" in src and "INTERRUPTED." in src
    save = inspect.getsource(connector.ZoteroConnectorHandler.download_and_attach)
    assert "except asyncio.CancelledError" in save and "stop.set()" in save
