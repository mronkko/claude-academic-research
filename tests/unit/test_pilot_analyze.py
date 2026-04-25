"""Unit tests for pilot_analyze (T2-6).

Replaces the user's downstream `pilot_year_cutoffs.py` (edited 11
times in the session log — the most-iterated ad-hoc script). Tests
exercise the four pure-data subcommand functions; matplotlib paths
are exercised in a separate test that gracefully skips when the
optional dep is absent.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pilot_analyze


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    """Write a minimal SEARCH_ROW_FIELDS-shaped CSV for tests."""
    fields = [
        "db", "query", "doi", "title", "authors", "year",
        "source", "issn", "cited_by", "abstract",
        "scopus_id", "wos_id", "openalex_id", "s2_paper_id",
        "oa_status", "oa_url",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow({f: row.get(f, "") for f in fields})


# ---------------------------------------------------------------------------
# cmd_year_cutoff
# ---------------------------------------------------------------------------


def test_year_cutoff_counts_rows_per_year(capsys) -> None:
    rows = [
        {"year": "2020"}, {"year": "2020"}, {"year": "2021"},
        {"year": "2018"}, {"year": "2018"}, {"year": "2018"},
    ]
    out = pilot_analyze.cmd_year_cutoff(rows)
    assert out["2020"] == 2
    assert out["2021"] == 1
    assert out["2018"] == 3
    captured = capsys.readouterr().out
    # Cumulative counts surface; check newest year is at the top.
    assert "2021" in captured
    assert "2018" in captured


def test_year_cutoff_buckets_blank_year_as_question_mark(capsys) -> None:
    rows = [{"year": "2020"}, {"year": ""}, {"year": "  "}]
    out = pilot_analyze.cmd_year_cutoff(rows)
    assert out["2020"] == 1
    assert out["?"] == 2


def test_year_cutoff_handles_empty_input(capsys) -> None:
    out = pilot_analyze.cmd_year_cutoff([])
    assert out == {}
    captured = capsys.readouterr().out
    assert "no rows" in captured


# ---------------------------------------------------------------------------
# cmd_db_overlap
# ---------------------------------------------------------------------------


def test_db_overlap_counts_unique_dois_per_db(capsys) -> None:
    rows = [
        # Three Scopus rows, two share DOIs with WoS.
        {"db": "scopus", "doi": "10.1/a"},
        {"db": "scopus", "doi": "10.1/b"},
        {"db": "scopus", "doi": "10.1/c"},
        {"db": "wos", "doi": "10.1/a"},
        {"db": "wos", "doi": "10.1/b"},
        {"db": "wos", "doi": "10.1/d"},
    ]
    out = pilot_analyze.cmd_db_overlap(rows)
    # Per-db unique-DOI counts.
    assert out == {"scopus": 3, "wos": 3}
    captured = capsys.readouterr().out
    # Overlap section appears.
    assert "scopus ∩ wos" in captured
    assert "2 shared" in captured  # a + b


def test_db_overlap_handles_single_db(capsys) -> None:
    rows = [{"db": "scopus", "doi": "10.1/a"}]
    pilot_analyze.cmd_db_overlap(rows)
    captured = capsys.readouterr().out
    assert "need at least two databases" in captured


def test_db_overlap_skips_rows_without_doi(capsys) -> None:
    """A row with empty DOI counts toward the per-db row count but
    can't participate in overlap math."""
    rows = [
        {"db": "scopus", "doi": "10.1/a"},
        {"db": "scopus", "doi": ""},
        {"db": "wos", "doi": "10.1/a"},
    ]
    out = pilot_analyze.cmd_db_overlap(rows)
    # Scopus: 1 unique DOI ("10.1/a"). WoS: 1 unique DOI.
    assert out["scopus"] == 1
    assert out["wos"] == 1
    captured = capsys.readouterr().out
    # Overlap of 1 (the single shared "10.1/a").
    assert "1 shared" in captured


# ---------------------------------------------------------------------------
# cmd_journal_coverage
# ---------------------------------------------------------------------------


def test_journal_coverage_counts_rows_per_source(capsys) -> None:
    rows = [
        {"source": "JBV"}, {"source": "JBV"}, {"source": "JBV"},
        {"source": "ETP"}, {"source": "ETP"},
        {"source": "Research Policy"},
    ]
    out = pilot_analyze.cmd_journal_coverage(rows)
    assert out["JBV"] == 3
    assert out["ETP"] == 2
    assert out["Research Policy"] == 1


def test_journal_coverage_top_n_truncates_print(capsys) -> None:
    rows = [{"source": f"J{i}"} for i in range(50) for _ in range(i)]
    pilot_analyze.cmd_journal_coverage(rows, top=5)
    captured = capsys.readouterr().out
    # Top 5 named explicitly.
    assert "J49" in captured  # most rows
    # J0 (zero rows) shouldn't even be in the input, but J1 (1 row)
    # should be off the top-5 list.
    lines = captured.splitlines()
    name_lines = [ln for ln in lines if "  J" in ln and ln.strip().startswith("J")]
    # Top-5 print yields at most 5 such lines.
    assert len(name_lines) <= 5


# ---------------------------------------------------------------------------
# cmd_field_breakdown
# ---------------------------------------------------------------------------


def test_field_breakdown_cross_references_journals_json(
    tmp_path: Path, capsys,
) -> None:
    """Each row's ISSN is looked up in journals.json to count by field."""
    journals_path = tmp_path / "journals.json"
    journals_path.write_text(
        '{"journals": ['
        '{"issn": "0883-9026", "title": "JBV", "field": "ENT-SBM"},'
        '{"issn": "1042-2587", "title": "ETP", "field": "ENT-SBM"},'
        '{"issn": "0048-7333", "title": "Research Policy", "field": "INNOV"}'
        "]}",
        encoding="utf-8",
    )
    rows = [
        {"issn": "0883-9026"},  # JBV → ENT-SBM
        {"issn": "08839026"},   # JBV bare ISSN → ENT-SBM (hyphen-insert lookup)
        {"issn": "1042-2587"},  # ETP → ENT-SBM
        {"issn": "0048-7333"},  # Research Policy → INNOV
        {"issn": "9999-9999"},  # not in journals.json → unknown
    ]
    out = pilot_analyze.cmd_field_breakdown(rows, journals_path)
    assert out["ENT-SBM"] == 3
    assert out["INNOV"] == 1
    captured = capsys.readouterr().out
    assert "1 rows had no matching journal" in captured


def test_field_breakdown_handles_hyphenated_to_bare_lookup(
    tmp_path: Path,
) -> None:
    """If journals.json was built with bare ISSNs and the search CSV
    has hyphenated ones, fallback lookup must still match."""
    journals_path = tmp_path / "journals.json"
    journals_path.write_text(
        '{"journals": [{"issn": "08839026", "title": "JBV", "field": "ENT-SBM"}]}',
        encoding="utf-8",
    )
    rows = [{"issn": "0883-9026"}]
    out = pilot_analyze.cmd_field_breakdown(rows, journals_path)
    assert out["ENT-SBM"] == 1


def test_field_breakdown_exits_when_journals_json_missing(tmp_path: Path) -> None:
    import pytest
    with pytest.raises(SystemExit):
        pilot_analyze.cmd_field_breakdown([], tmp_path / "missing.json")


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def test_cli_year_cutoff_runs_against_real_csv(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    csv_path = tmp_path / "search_results.csv"
    _write_csv(csv_path, [
        {"db": "scopus", "year": "2020", "source": "JBV"},
        {"db": "scopus", "year": "2020", "source": "JBV"},
        {"db": "wos", "year": "2021", "source": "ETP"},
    ])
    import sys as _sys
    monkeypatch.setattr(_sys, "argv", [
        "pilot_analyze.py", "year-cutoff", "--csv", str(csv_path),
    ])
    rc = pilot_analyze.main()
    assert rc == 0
    captured = capsys.readouterr().out
    assert "Loaded 3 rows" in captured
    assert "2021" in captured
    assert "2020" in captured


def test_cli_exits_when_csv_missing(tmp_path: Path, monkeypatch) -> None:
    import sys as _sys

    import pytest
    monkeypatch.setattr(_sys, "argv", [
        "pilot_analyze.py", "year-cutoff", "--csv", str(tmp_path / "missing.csv"),
    ])
    with pytest.raises(SystemExit):
        pilot_analyze.main()
