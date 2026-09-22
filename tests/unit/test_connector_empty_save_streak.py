"""Zotero Desktop stops taking saves mid-run.

Run 7 (2026-09-23): 54 items attached in a row, then four consecutive
"the translator saved nothing" on ordinary ScienceDirect pages with the
same licence as their neighbours. Desktop's local API still answered
reads but received nothing new. Each was logged ACCESS_BLOCKED and cost
240 s. After earlier successes, a streak of empty saves says more about
Desktop than about access.
"""

from __future__ import annotations

import inspect

import enrich_pdfs
from enrich_pdfs import _EmptySaveStreak


def test_empty_saves_before_any_success_are_ordinary_failures() -> None:
    s = _EmptySaveStreak(limit=3)
    assert s.observe("A", "saved_nothing", saved_before=0) == [("A", "connector_save_failed")]
    assert not s.stalled


def test_a_success_releases_held_items_as_real_failures() -> None:
    s = _EmptySaveStreak(limit=3)
    assert s.observe("A", "saved_nothing", saved_before=5) == []
    assert s.observe("B", "saved_nothing", saved_before=5) == []
    released = s.observe("C", "attached", saved_before=5)
    assert released == [("A", "connector_save_failed"), ("B", "connector_save_failed")]
    assert not s.stalled


def test_a_streak_after_successes_is_a_stall() -> None:
    s = _EmptySaveStreak(limit=3)
    s.observe("A", "saved_nothing", saved_before=54)
    s.observe("B", "saved_nothing", saved_before=54)
    got = s.observe("C", "saved_nothing", saved_before=54)
    assert got == [(k, "connector_desktop_stalled") for k in "ABC"]
    assert s.stalled


def test_held_items_are_released_at_the_end() -> None:
    s = _EmptySaveStreak(limit=3)
    s.observe("A", "saved_nothing", saved_before=2)
    assert s.flush() == [("A", "connector_save_failed")]


def test_the_driver_stops_on_a_stall_and_the_handler_reports_empty_saves() -> None:
    src = inspect.getsource(enrich_pdfs._drive_connector)
    assert "_EmptySaveStreak(" in src and "streak.stalled" in src
    from fetchers.browser import connector
    handler_src = inspect.getsource(connector.ZoteroConnectorHandler.download_and_attach)
    assert 'self.last_outcome = "saved_nothing"' in handler_src
    assert "connector_desktop_stalled" in enrich_pdfs.pdf_run_report.STATUS_INFO
