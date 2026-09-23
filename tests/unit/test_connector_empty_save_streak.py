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
    assert "_EmptySaveStreak(" in src
    assert "streak.stalled" in inspect.getsource(enrich_pdfs._connector_item_loop)
    from fetchers.browser import connector
    handler_src = inspect.getsource(connector.ZoteroConnectorHandler.download_and_attach)
    assert 'self.last_outcome = "saved_nothing"' in handler_src
    assert "connector_desktop_stalled" in enrich_pdfs.pdf_run_report.STATUS_INFO


# The Connector's own "save over" signal (connector.classify_save_watch).
# Reported live on 2aad88e: an HTML galley (10.20529/ijme.2012.079) waited
# the full 240 s, and three such pages in a row stopped an overnight pass.


def test_a_page_that_offered_nothing_is_logged_at_once_and_releases_the_hold() -> None:
    s = _EmptySaveStreak(limit=3)
    s.observe("A", "saved_nothing", saved_before=5)
    s.observe("B", "saved_nothing", saved_before=5)
    got = s.observe("C", "offered_nothing", saved_before=5)
    assert got == [
        ("A", "connector_save_failed"), ("B", "connector_save_failed"),
        ("C", "connector_offered_nothing"),
    ]
    assert not s.stalled


def test_pages_desktop_was_never_asked_about_neither_grow_nor_clear_the_hold() -> None:
    s = _EmptySaveStreak(limit=3)
    s.observe("A", "saved_nothing", saved_before=5)
    for k in "XYZW":
        assert s.observe(k, "offered_nothing_unasked", saved_before=5) == [
            (k, "connector_offered_nothing"),
        ]
    assert not s.stalled
    assert s.flush() == [("A", "connector_save_failed")]


def test_the_driver_leaves_streak_outcomes_to_the_streak() -> None:
    src = inspect.getsource(enrich_pdfs._connector_item_loop)
    assert "_STREAK_LOGGED_OUTCOMES" in src
    assert {"saved_nothing", "offered_nothing", "offered_nothing_unasked"} <= (
        enrich_pdfs._STREAK_LOGGED_OUTCOMES
    )


def test_an_unmatched_save_releases_the_hold_without_an_access_verdict() -> None:
    s = _EmptySaveStreak(limit=3)
    s.observe("A", "saved_nothing", saved_before=5)
    assert s.observe("B", "saved_unmatched", saved_before=5) == [
        ("A", "connector_save_failed"), ("B", "connector_save_unmatched"),
    ]


def test_offered_nothing_is_not_an_access_verdict() -> None:
    """Reported live: a PDF-less HTML galley logged ACCESS_BLOCKED read
    downstream as "we were refused" and was hand-corrected all day."""
    import pdf_fetch_log
    cause = pdf_fetch_log.FailureCause.NO_PDF_OFFERED.value
    assert cause in pdf_fetch_log.RECOVERABLE_CAUSES
    assert "ILL" not in pdf_fetch_log.SUGGESTED_FE_CODE[cause].split("—")[0]
    src = inspect.getsource(enrich_pdfs._drive_connector)
    assert "FailureCause.NO_PDF_OFFERED" in src
    for status in ("connector_offered_nothing", "connector_save_unmatched"):
        assert status in enrich_pdfs.pdf_run_report.STATUS_INFO
