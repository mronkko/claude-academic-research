"""Pass 3's route lookups must show progress, not 15 minutes of silence.

Reported from a live run: 940 Connector-queued items at JYU, stdout
silent for 14+ minutes after the last publisher's "Total: …" line, and
the last progress event `publisher_done` — the user asked if it hung.
"""

from __future__ import annotations

import enrich_pdfs
import pytest
from fetchers.browser import interaction


def _run(total: int, enabled: bool, monkeypatch) -> list[dict]:
    events: list[dict] = []
    monkeypatch.setattr(interaction, "report_progress", events.append)
    p = enrich_pdfs._RouteLookupProgress(total, enabled=enabled)
    p.start()
    for _ in range(total):
        p.tick()
    p.done()
    return events


def test_route_lookups_announce_tick_and_finish(
    monkeypatch, capsys: pytest.CaptureFixture[str],
) -> None:
    events = _run(120, True, monkeypatch)
    out = capsys.readouterr().out

    assert "Looking up library routes for 120 Connector items" in out
    assert "50/120 routes looked up" in out
    assert "100/120 routes looked up" in out
    assert "Route lookups done: 120" in out
    kinds = [e["event"] for e in events]
    assert kinds == ["routes_start", "routes_progress", "routes_progress",
                     "routes_done"]
    assert events[-1]["done"] == 120


def test_no_tick_on_the_final_item(monkeypatch, capsys) -> None:
    """The done line covers the last item; a tick there says it twice."""
    events = _run(100, True, monkeypatch)
    assert [e["event"] for e in events].count("routes_progress") == 1
    assert "100/100 routes looked up" not in capsys.readouterr().out


@pytest.mark.parametrize("total,enabled", [(0, True), (500, False)])
def test_silent_without_lookups(total, enabled, monkeypatch, capsys) -> None:
    assert _run(total, enabled, monkeypatch) == []
    assert capsys.readouterr().out == ""
