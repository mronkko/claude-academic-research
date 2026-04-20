"""Tests for the export_coded_includes.py filtering behaviour."""

from __future__ import annotations

import csv
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "pipelines" / "export_coded_includes.py"


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, timeout=30,
    )


def _write_log(path: Path, rows: list[dict]) -> None:
    fields = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def test_filters_to_includes_only(tmp_path) -> None:
    log = tmp_path / "screening.csv"
    out = tmp_path / "coded.csv"
    _write_log(log, [
        {"item_key": "A", "decision": "include", "title": "A", "year": "2020", "authors": "Zed"},
        {"item_key": "B", "decision": "exclude", "title": "B", "year": "2019", "authors": "Aey"},
        {"item_key": "C", "decision": "include", "title": "C", "year": "2021", "authors": "Mid"},
        {"item_key": "D", "decision": "error",   "title": "D", "year": "2021", "authors": "Mid"},
    ])
    result = _run(["--log-csv", str(log), "--out", str(out)])
    assert result.returncode == 0, f"stderr={result.stderr}\nstdout={result.stdout}"

    with out.open(newline="") as f:
        kept = list(csv.DictReader(f))
    keys = {r["item_key"] for r in kept}
    assert keys == {"A", "C"}, f"kept {keys}"


def test_last_row_wins_on_item_key(tmp_path) -> None:
    """Append a flip (adjudication): the later decision overrides."""
    log = tmp_path / "screening.csv"
    out = tmp_path / "coded.csv"
    _write_log(log, [
        {"item_key": "A", "decision": "include", "title": "A", "year": "2020", "authors": "X"},
        {"item_key": "A", "decision": "exclude", "title": "A", "year": "2020", "authors": "X"},
    ])
    result = _run(["--log-csv", str(log), "--out", str(out)])
    assert result.returncode == 0, result.stderr
    with out.open(newline="") as f:
        kept = list(csv.DictReader(f))
    assert kept == [], f"Expected no includes; got {kept}"


def test_dry_run_writes_nothing(tmp_path) -> None:
    log = tmp_path / "screening.csv"
    out = tmp_path / "coded.csv"
    _write_log(log, [
        {"item_key": "A", "decision": "include", "title": "A", "year": "2020", "authors": "X"},
    ])
    result = _run(["--log-csv", str(log), "--out", str(out), "--dry-run"])
    assert result.returncode == 0, result.stderr
    assert not out.exists(), "dry-run must not write output"


def test_columns_flag_restricts_output(tmp_path) -> None:
    log = tmp_path / "screening.csv"
    out = tmp_path / "coded.csv"
    _write_log(log, [
        {"item_key": "A", "decision": "include", "title": "A",
         "year": "2020", "authors": "X", "extra": "drop_me"},
    ])
    result = _run([
        "--log-csv", str(log), "--out", str(out),
        "--columns", "item_key,title,year",
    ])
    assert result.returncode == 0, result.stderr
    with out.open(newline="") as f:
        header = next(csv.reader(f))
    assert header == ["item_key", "title", "year"], header


def test_alternative_decision(tmp_path) -> None:
    """--decision exclude keeps exclusions (for PRISMA reporting)."""
    log = tmp_path / "screening.csv"
    out = tmp_path / "coded.csv"
    _write_log(log, [
        {"item_key": "A", "decision": "include", "title": "A", "year": "2020", "authors": "X"},
        {"item_key": "B", "decision": "exclude", "title": "B", "year": "2019", "authors": "Y"},
    ])
    result = _run([
        "--log-csv", str(log), "--out", str(out),
        "--decision", "exclude",
    ])
    assert result.returncode == 0, result.stderr
    with out.open(newline="") as f:
        kept = list(csv.DictReader(f))
    assert {r["item_key"] for r in kept} == {"B"}
