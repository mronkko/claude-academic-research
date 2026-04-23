"""Tests for scripts/pipelines/screening_report.py."""

from __future__ import annotations

from pathlib import Path

import screening_report as mod


def test_collapse_last_row_wins_keeps_most_recent() -> None:
    rows = [
        {"item_key": "A", "decision": "include"},
        {"item_key": "B", "decision": "exclude"},
        {"item_key": "A", "decision": "exclude"},   # re-screen
        {"item_key": "C", "decision": "borderline"},
    ]
    latest = mod.collapse_last_row_wins(rows)
    assert latest["A"]["decision"] == "exclude"
    assert latest["B"]["decision"] == "exclude"
    assert latest["C"]["decision"] == "borderline"
    assert len(latest) == 3


def test_collapse_last_row_wins_drops_blank_keys() -> None:
    rows = [
        {"item_key": "", "decision": "include"},
        {"item_key": "A", "decision": "exclude"},
    ]
    latest = mod.collapse_last_row_wins(rows)
    assert list(latest) == ["A"]


def test_find_rescreened_returns_keys_with_multiple_rows() -> None:
    rows = [
        {"item_key": "A", "decision": "include"},
        {"item_key": "B", "decision": "exclude"},
        {"item_key": "A", "decision": "exclude"},   # second pass
        {"item_key": "C", "decision": "include"},
        {"item_key": "C", "decision": "borderline"},   # second pass
    ]
    rescreened = mod.find_rescreened(rows)
    assert rescreened == {"A", "C"}


def test_main_prints_summary_and_lists(tmp_path: Path, capsys) -> None:
    log_csv = tmp_path / "log.csv"
    log_csv.write_text(
        "timestamp,item_key,doi,title,decision,reason\n"
        "2026-04-23T10:00,A,10.1/a,Alpha,include,\n"
        "2026-04-23T10:01,B,10.1/b,Bravo,exclude,e1\n"
        "2026-04-23T10:02,A,10.1/a,Alpha,exclude,e2\n"
        "2026-04-23T10:03,C,10.1/c,Charlie,borderline,\n",
        encoding="utf-8",
    )

    rc = mod.main([str(log_csv), "--list", "exclude", "--list-rescreened"])
    assert rc == 0

    out = capsys.readouterr().out
    # Latest decisions: A exclude, B exclude, C borderline → exclude=2.
    assert "exclude     2" in out
    assert "borderline  1" in out
    assert "include     0" in out
    # Listed exclude items.
    assert "A  Alpha" in out
    assert "B  Bravo" in out
    # Re-screened section names A only.
    assert "A  [2 rows, last=exclude]" in out


def test_main_returns_2_on_missing_log(tmp_path: Path) -> None:
    rc = mod.main([str(tmp_path / "missing.csv")])
    assert rc == 2
