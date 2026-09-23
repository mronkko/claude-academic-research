"""The Connector says when a save is over; stop waiting then.

Reported live on 2aad88e: a page with no PDF (an HTML galley on ijme.in)
cost the full 240 s `_SAVE_POLL_TIMEOUT_S`, against 10-20 s for a normal
save. `_watch_save` reads what the service-worker hooks recorded
(`progressWindow.done`, and the `save…` calls to Desktop) and ends the
poll a short grace after the save is over.
"""

from __future__ import annotations

import asyncio
import threading
import time

from fetchers.browser import connector
from fetchers.browser.connector import (
    _poll_for_new_item,
    _watch_save,
    classify_save_watch,
)


def _state(done, saves=(), ever=True):
    return {"done": done, "saves": list(saves), "everSawSave": ever}


def test_classification() -> None:
    assert classify_save_watch(None) == ""
    assert classify_save_watch(_state(None, ["pending"])) == ""
    assert classify_save_watch(_state({"ok": False}, ["pending"])) == ""
    assert classify_save_watch(_state({"ok": True}, ["ok"])) == "saved"
    assert classify_save_watch(_state({"ok": False}, ["ok", "error"])) == "desktop_error"
    assert classify_save_watch(_state({"ok": False}, ["ok"])) == "offered_nothing"
    assert classify_save_watch(_state({"ok": False}, [])) == "offered_nothing_unasked"
    # The hook never fired in this worker: "Desktop was not asked" is unproven.
    assert classify_save_watch(_state({"ok": False}, [], ever=False)) == "finished_unverified"


class _Zot:
    def recent_items(self):
        return []


def test_stop_ends_the_poll_early() -> None:
    stop = threading.Event()
    threading.Timer(0.2, stop.set).start()
    t0 = time.monotonic()
    assert _poll_for_new_item(_Zot(), "10.1/x", "K", 30.0, stop=stop) is None
    assert time.monotonic() - t0 < 5


class _SW:
    def __init__(self, states):
        self.states = list(states)

    async def evaluate(self, js):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]


def _run_watch(sw, monkeypatch, *, poll_timeout=30.0):
    monkeypatch.setattr(connector, "_SAVE_DONE_GRACE_DEFAULT_S", 0.05)
    monkeypatch.setattr(connector, "_SAVE_DONE_GRACE_S", {"saved": 0.05})

    async def go():
        stop = threading.Event()
        task = asyncio.ensure_future(asyncio.to_thread(
            _poll_for_new_item, _Zot(), "10.1/x", "K", poll_timeout, stop=stop,
        ))
        t0 = time.monotonic()
        verdict = await _watch_save(sw, task, stop, every_s=0.05)
        key = await task
        return verdict, key, time.monotonic() - t0

    return asyncio.run(go())


def test_a_finished_save_ends_the_wait(monkeypatch) -> None:
    sw = _SW([_state(None, ["pending"]), _state({"ok": False}, ["ok"])])
    verdict, key, took = _run_watch(sw, monkeypatch)
    assert (verdict, key) == ("offered_nothing", None)
    assert took < 5


def test_an_unfinished_save_keeps_the_full_wait(monkeypatch) -> None:
    sw = _SW([_state(None, ["pending"])])
    verdict, _, took = _run_watch(sw, monkeypatch, poll_timeout=1.0)
    assert verdict == ""
    assert took >= 0.9


def test_a_worker_that_stops_answering_leaves_the_poll_to_its_deadline(monkeypatch) -> None:
    class Dead:
        async def evaluate(self, js):
            raise RuntimeError("Target closed")

    verdict, _, took = _run_watch(Dead(), monkeypatch, poll_timeout=1.0)
    assert verdict == ""
    assert took >= 0.9


def test_the_handler_installs_the_watch_and_begins_it_before_saving() -> None:
    import inspect
    src = inspect.getsource(connector.ZoteroConnectorHandler.download_and_attach)
    assert "_SAVE_WATCH_INSTALL_JS" in src
    assert src.index("__arSaveWatch.begin()") < src.index("saveWithTranslator(")
    assert "_watch_save(" in src
