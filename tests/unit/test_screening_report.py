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


def _write(path: Path, header: str, *lines: str) -> Path:
    path.write_text("\n".join((header, *lines)) + "\n", encoding="utf-8")
    return path


def test_search_csv_reports_removed_before_screening(tmp_path: Path,
                                                     capsys) -> None:
    log_csv = _write(tmp_path / "log.csv", "item_key,decision", "A,include")
    search_csv = _write(
        tmp_path / "search.csv", "doi,title,doctype",
        "10.1037/apl0000550,Paper,",
        "10.1037/apl0000550.supp,Supplemental Material for Paper,supplement",
        "10.1002/job.2525,Issue Information,front-matter",
        "10.1016/x,Editorial Board,front-matter",
    )
    assert mod.main([str(log_csv), "--search-csv", str(search_csv)]) == 0
    out = capsys.readouterr().out
    assert "Search records:     4" in out
    assert ("Removed before screening (not research articles): 3 "
            "(supplement 1, front-matter 2)") in out


def test_search_csv_without_doctype_column_says_unknown(tmp_path: Path,
                                                        capsys) -> None:
    log_csv = _write(tmp_path / "log.csv", "item_key,decision", "A,include")
    search_csv = _write(tmp_path / "s.csv", "doi,title", "10.1/x,Paper")
    assert mod.main([str(log_csv), "--search-csv", str(search_csv)]) == 0
    assert "Removed before screening: unknown" in capsys.readouterr().out
