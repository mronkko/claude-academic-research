"""Tests for audit_zotero_library row-level subcommands (T1-2).

Replaces the 22 inline `python3 -c "import csv; ..."` introspections
visible in the SLR session log with named subcommands routed through
`csv_summary` helpers. These tests exercise each subcommand's CLI
handling against synthetic CSVs.
"""

from __future__ import annotations

import csv
from pathlib import Path

import audit_zotero_library as audit


def _write_screening_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fields = [
        "timestamp", "item_key", "doi", "title", "year", "journal",
        "decision", "exclusion_code", "reason", "model", "prompt_version",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow({f: row.get(f, "") for f in fields})


# ---------------------------------------------------------------------------
# find
# ---------------------------------------------------------------------------


def test_find_locates_rows_with_matching_substring(tmp_path: Path, capsys) -> None:
    csv_path = tmp_path / "screening.csv"
    _write_screening_csv(csv_path, [
        {"item_key": "A1", "title": "AI in entrepreneurship review",
         "decision": "include"},
        {"item_key": "A2", "title": "Random unrelated paper",
         "decision": "exclude"},
        {"item_key": "A3", "title": "Generative AI usage in startups",
         "decision": "include"},
    ])
    rc = audit._row_query_main(["find", "AI", "--csv", str(csv_path)])
    assert rc == 0
    captured = capsys.readouterr().out
    assert "A1" in captured
    assert "A3" in captured
    assert "A2" not in captured


def test_find_with_in_field_limits_search_scope(tmp_path: Path, capsys) -> None:
    """`AI` appears in title for one row and in reason for another;
    --in-field title should match only the title hit."""
    csv_path = tmp_path / "screening.csv"
    _write_screening_csv(csv_path, [
        {"item_key": "T1", "title": "AI study", "reason": "scope ok"},
        {"item_key": "T2", "title": "Random", "reason": "no AI methods"},
    ])
    audit._row_query_main([
        "find", "AI", "--csv", str(csv_path), "--in-field", "title",
    ])
    captured = capsys.readouterr().out
    assert "T1" in captured
    assert "T2" not in captured


def test_find_reports_no_matches(tmp_path: Path, capsys) -> None:
    csv_path = tmp_path / "screening.csv"
    _write_screening_csv(csv_path, [
        {"item_key": "X1", "title": "Something else"},
    ])
    audit._row_query_main(["find", "AI", "--csv", str(csv_path)])
    captured = capsys.readouterr().out
    assert "No rows match" in captured


def test_find_default_is_case_insensitive(tmp_path: Path, capsys) -> None:
    csv_path = tmp_path / "screening.csv"
    _write_screening_csv(csv_path, [{"item_key": "C1", "title": "AI Paper"}])
    audit._row_query_main(["find", "ai paper", "--csv", str(csv_path)])
    captured = capsys.readouterr().out
    assert "C1" in captured


def test_find_case_sensitive_flag_filters_lowercase(tmp_path: Path, capsys) -> None:
    csv_path = tmp_path / "screening.csv"
    _write_screening_csv(csv_path, [{"item_key": "C1", "title": "AI Paper"}])
    audit._row_query_main([
        "find", "ai paper", "--csv", str(csv_path), "--case-sensitive",
    ])
    captured = capsys.readouterr().out
    assert "No rows match" in captured


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def test_show_prints_full_row_for_known_key(tmp_path: Path, capsys) -> None:
    csv_path = tmp_path / "screening.csv"
    _write_screening_csv(csv_path, [
        {"item_key": "S1", "title": "Test paper", "decision": "include",
         "doi": "10.1/x", "year": "2020"},
    ])
    rc = audit._row_query_main(["show", "S1", "--csv", str(csv_path)])
    assert rc == 0
    captured = capsys.readouterr().out
    assert "Test paper" in captured
    assert "include" in captured
    assert "10.1/x" in captured


def test_show_returns_nonzero_for_missing_key(tmp_path: Path, capsys) -> None:
    csv_path = tmp_path / "screening.csv"
    _write_screening_csv(csv_path, [{"item_key": "OTHER"}])
    rc = audit._row_query_main(["show", "MISSING", "--csv", str(csv_path)])
    assert rc == 1
    captured = capsys.readouterr().out
    assert "No row" in captured


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


def test_diff_summarises_only_in_a_only_in_b_changed(tmp_path: Path, capsys) -> None:
    csv_a = tmp_path / "before.csv"
    csv_b = tmp_path / "after.csv"
    _write_screening_csv(csv_a, [
        {"item_key": "A", "decision": "include"},
        {"item_key": "B", "decision": "exclude"},
        {"item_key": "C", "decision": "borderline"},
    ])
    _write_screening_csv(csv_b, [
        {"item_key": "A", "decision": "include"},        # unchanged
        {"item_key": "B", "decision": "include"},        # changed
        {"item_key": "D", "decision": "exclude"},        # added in B
        # C dropped from B
    ])
    rc = audit._row_query_main(["diff", str(csv_a), str(csv_b)])
    assert rc == 0
    captured = capsys.readouterr().out
    assert "only in A: 1" in captured  # C
    assert "only in B: 1" in captured  # D
    assert "changed:   1" in captured  # B


# ---------------------------------------------------------------------------
# by-decision
# ---------------------------------------------------------------------------


def test_by_decision_summary_prints_counts_per_decision(
    tmp_path: Path, capsys,
) -> None:
    csv_path = tmp_path / "screening.csv"
    _write_screening_csv(csv_path, [
        {"item_key": "A", "decision": "include"},
        {"item_key": "B", "decision": "include"},
        {"item_key": "C", "decision": "exclude"},
        {"item_key": "D", "decision": "borderline"},
    ])
    rc = audit._row_query_main(["by-decision", "--csv", str(csv_path)])
    assert rc == 0
    captured = capsys.readouterr().out
    assert "include" in captured
    assert "exclude" in captured
    assert "borderline" in captured
    # The most-common decision appears first; check at least one count
    # line is present.
    assert "2" in captured


def test_by_decision_filter_lists_matching_rows(tmp_path: Path, capsys) -> None:
    csv_path = tmp_path / "screening.csv"
    _write_screening_csv(csv_path, [
        {"item_key": "INC1", "title": "Included paper", "decision": "include"},
        {"item_key": "EXC1", "title": "Excluded paper", "decision": "exclude",
         "exclusion_code": "FE2"},
        {"item_key": "INC2", "title": "Another include", "decision": "include"},
    ])
    rc = audit._row_query_main([
        "by-decision", "--csv", str(csv_path), "--filter", "include",
    ])
    assert rc == 0
    captured = capsys.readouterr().out
    assert "INC1" in captured
    assert "INC2" in captured
    assert "EXC1" not in captured


def test_by_decision_filter_is_case_insensitive(tmp_path: Path, capsys) -> None:
    csv_path = tmp_path / "screening.csv"
    _write_screening_csv(csv_path, [
        {"item_key": "I1", "decision": "Include"},  # capitalized
    ])
    audit._row_query_main([
        "by-decision", "--csv", str(csv_path), "--filter", "INCLUDE",
    ])
    captured = capsys.readouterr().out
    assert "I1" in captured


# ---------------------------------------------------------------------------
# Routing — `main()` should fast-path subcommands without hitting Zotero
# ---------------------------------------------------------------------------


def test_main_dispatches_to_row_query_when_first_arg_is_subcommand(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    """The legacy macro audit requires --group/--user; the row-level
    ops must NOT trip that requirement. Verify by invoking `find`
    without any --group flag — should succeed."""
    csv_path = tmp_path / "screening.csv"
    _write_screening_csv(csv_path, [{"item_key": "Z1", "title": "Zotero paper"}])
    import sys as _sys
    monkeypatch.setattr(_sys, "argv", [
        "audit_zotero_library.py", "find", "Zotero", "--csv", str(csv_path),
    ])
    rc = audit.main()
    assert rc == 0
    captured = capsys.readouterr().out
    assert "Z1" in captured
