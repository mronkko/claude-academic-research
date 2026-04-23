"""Tests for scripts/pipelines/filter_search_results.py."""

from __future__ import annotations

import csv
from pathlib import Path

import filter_search_results as mod


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "db,doi,title,year\n"
        + "\n".join(
            f"{r['db']},{r['doi']},{r['title']},{r['year']}" for r in rows
        )
        + "\n",
        encoding="utf-8",
    )


def test_filter_rows_year_range_drops_outside() -> None:
    rows = [
        {"year": "2018"},
        {"year": "2024"},
        {"year": "2022"},
        {"year": ""},
    ]
    out = mod.filter_rows(rows, year_min=2020, year_max=2025, top_n=None)
    assert [r["year"] for r in out] == ["2024", "2022"]


def test_filter_rows_top_n_after_sort() -> None:
    """Sort is descending by year; top-N picks the most recent."""
    rows = [
        {"year": "2018"},
        {"year": "2024"},
        {"year": "2022"},
        {"year": "2025"},
    ]
    out = mod.filter_rows(rows, year_min=None, year_max=None, top_n=2)
    assert [r["year"] for r in out] == ["2025", "2024"]


def test_filter_rows_missing_year_sinks_to_bottom() -> None:
    """Rows without a parseable year sort to year=0; with no top-N
    cap they remain in the output but at the bottom."""
    rows = [
        {"year": "2024"},
        {"year": ""},
        {"year": "abc"},
        {"year": "2020"},
    ]
    out = mod.filter_rows(rows, year_min=None, year_max=None, top_n=None)
    parsed_order = [mod._year_or_zero(r) for r in out]
    assert parsed_order == sorted(parsed_order, reverse=True)


def test_main_writes_filtered_csv(tmp_path) -> None:
    in_csv = tmp_path / "in.csv"
    out_csv = tmp_path / "out.csv"
    _write_csv(in_csv, [
        {"db": "scopus", "doi": "10.1/a", "title": "A", "year": "2018"},
        {"db": "wos",    "doi": "10.1/b", "title": "B", "year": "2024"},
        {"db": "openalex", "doi": "10.1/c", "title": "C", "year": "2022"},
    ])

    rc = mod.main([
        "--input", str(in_csv),
        "--output", str(out_csv),
        "--year-min", "2020",
    ])
    assert rc == 0

    with out_csv.open(newline="") as f:
        out_rows = list(csv.DictReader(f))
    assert [r["doi"] for r in out_rows] == ["10.1/b", "10.1/c"]


def test_main_returns_2_on_missing_input(tmp_path) -> None:
    rc = mod.main([
        "--input", str(tmp_path / "does-not-exist.csv"),
        "--output", str(tmp_path / "out.csv"),
    ])
    assert rc == 2
