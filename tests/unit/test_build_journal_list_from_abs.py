"""Unit tests for build_journal_list_from_abs (T2-1).

Replaces the user's downstream `build_journal_list.py` (edited 11 times
in the session log) with a plugin-shipped, source-explicit script. Tests
exercise the pure logic (filtering + ISSN normalization) so the script
is verifiable without a real ABS xlsx fixture, and one integration test
runs end-to-end through openpyxl on a tiny synthesized workbook.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import build_journal_list_from_abs as build
import pytest

# ---------------------------------------------------------------------------
# _normalise_issn
# ---------------------------------------------------------------------------


def test_normalise_issn_inserts_hyphen_when_missing() -> None:
    assert build._normalise_issn("00401625") == "0040-1625"


def test_normalise_issn_passes_hyphenated_through() -> None:
    assert build._normalise_issn("0040-1625") == "0040-1625"


def test_normalise_issn_uppercases_check_digit_x() -> None:
    assert build._normalise_issn("0317847x") == "0317-847X"


def test_normalise_issn_returns_empty_on_invalid() -> None:
    assert build._normalise_issn("") == ""
    assert build._normalise_issn("not-an-issn") == ""
    assert build._normalise_issn("123") == ""
    assert build._normalise_issn(None) == ""


def test_normalise_issn_handles_int_input() -> None:
    """openpyxl can return numeric cells as ints — make sure we cope."""
    assert build._normalise_issn(40_1625) == ""  # too few digits → empty
    # A real-world Scopus-form 8-digit numeric cell.
    assert build._normalise_issn(401625_00) == "4016-2500"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# filter_journals
# ---------------------------------------------------------------------------


COLUMNS = {"field": 0, "title": 1, "issn": 2, "rank": 3}

ROWS = [
    ("ENT-SBM", "Journal of Business Venturing", "0883-9026", "4*"),
    ("ENT-SBM", "Entrepreneurship Theory and Practice", "10422587", "4*"),
    ("ENT-SBM", "Family Business Review", "0894-4865", "3"),
    ("ENT-SBM", "Some Mediocre Journal", "1234-5678", "2"),
    ("INNOV", "Research Policy", "0048-7333", "4"),
    ("INNOV", "Technovation", "0166-4972", "3"),
    ("OR&MANSCI", "Operations Research", "0030-364X", "4*"),
    ("ECON", "Econometrica", "0012-9682", "4*"),  # out-of-discipline
]


def test_filter_no_filters_returns_every_well_formed_row() -> None:
    out = build.filter_journals(ROWS, COLUMNS)
    titles = [r["title"] for r in out]
    assert "Journal of Business Venturing" in titles
    assert "Econometrica" in titles
    assert len(out) == len(ROWS)


def test_filter_by_ranks_keeps_only_matching() -> None:
    out = build.filter_journals(ROWS, COLUMNS, ranks=["4*", "4"])
    titles = [r["title"] for r in out]
    assert "Some Mediocre Journal" not in titles  # rank 2
    assert "Family Business Review" not in titles  # rank 3
    assert "Journal of Business Venturing" in titles
    assert "Operations Research" in titles


def test_filter_by_fields_keeps_only_matching() -> None:
    out = build.filter_journals(ROWS, COLUMNS, fields=["ENT-SBM", "INNOV"])
    titles = {r["title"] for r in out}
    assert "Operations Research" not in titles  # OR&MANSCI excluded
    assert "Econometrica" not in titles
    assert {"Journal of Business Venturing", "Research Policy"} <= titles


def test_filter_combines_ranks_and_fields() -> None:
    out = build.filter_journals(
        ROWS, COLUMNS, ranks=["4*"], fields=["ENT-SBM"],
    )
    titles = {r["title"] for r in out}
    assert titles == {
        "Journal of Business Venturing",
        "Entrepreneurship Theory and Practice",
    }


def test_filter_is_case_insensitive_on_rank_and_field() -> None:
    """Lowercase user input shouldn't miss uppercased spreadsheet entries."""
    out = build.filter_journals(
        ROWS, COLUMNS, ranks=["4*"], fields=["ent-sbm"],
    )
    assert len(out) == 2  # JBV + ETP


def test_filter_normalises_issns_in_output() -> None:
    """A row with an unhyphenated ISSN should emit the canonical L-form."""
    out = build.filter_journals(ROWS, COLUMNS, ranks=["4*"], fields=["ENT-SBM"])
    etp = next(r for r in out if "Theory and Practice" in r["title"])
    assert etp["issn"] == "1042-2587"


def test_filter_drops_rows_with_invalid_issn() -> None:
    rows = [
        ("ENT-SBM", "Real Journal", "0883-9026", "4*"),
        ("ENT-SBM", "Junk Row", "not-an-issn", "4*"),
        ("ENT-SBM", "", "", ""),
    ]
    out = build.filter_journals(rows, COLUMNS)
    assert len(out) == 1
    assert out[0]["title"] == "Real Journal"


def test_filter_writes_field_into_each_record() -> None:
    """Audit / cross-source traceability needs field on every row."""
    out = build.filter_journals(ROWS, COLUMNS, ranks=["4*"])
    for row in out:
        assert row["field"]


def test_filter_handles_empty_filter_lists_as_no_filter() -> None:
    """ranks=[] is the same as ranks=None — no filter applied."""
    a = build.filter_journals(ROWS, COLUMNS, ranks=[])
    b = build.filter_journals(ROWS, COLUMNS, ranks=None)
    assert a == b
    assert len(a) == len(ROWS)


# ---------------------------------------------------------------------------
# _resolve_columns
# ---------------------------------------------------------------------------


def _header_cell(value: str) -> object:
    """Mock of openpyxl cell: just needs a `.value` attribute."""
    cell = type("Cell", (), {})()
    cell.value = value
    return cell


def test_resolve_columns_returns_indices_for_default_headers() -> None:
    header = tuple(
        _header_cell(h) for h in ("Field", "Journal", "ISSN", "AJG 2024", "Note")
    )
    cols = build._resolve_columns(
        header, "Field", "Journal", "ISSN", "AJG 2024",
    )
    assert cols == {"field": 0, "title": 1, "issn": 2, "rank": 3}


def test_resolve_columns_exits_with_helpful_error_on_missing_header() -> None:
    header = tuple(_header_cell(h) for h in ("Field", "Journal"))  # no ISSN
    with pytest.raises(SystemExit) as exc:
        build._resolve_columns(
            header, "Field", "Journal", "ISSN", "AJG 2024",
        )
    msg = str(exc.value)
    assert "ISSN" in msg
    assert "--issn-column" in msg


# ---------------------------------------------------------------------------
# CLI integration via a stubbed openpyxl workbook
# ---------------------------------------------------------------------------


def test_cli_writes_journals_json_with_full_audit_trail(
    tmp_path: Path, monkeypatch,
) -> None:
    """End-to-end happy path. Avoids needing a real .xlsx by stubbing
    openpyxl.load_workbook with a row-tuple mock."""
    fake_rows = [
        # header
        tuple(
            _header_cell(h) for h in ("Field", "Journal", "ISSN", "AJG 2024")
        ),
        # data rows
        tuple(_header_cell(v) for v in ("ENT-SBM", "JBV", "0883-9026", "4*")),
        tuple(_header_cell(v) for v in ("ENT-SBM", "ETP", "10422587", "4*")),
        tuple(_header_cell(v) for v in ("ENT-SBM", "Mediocre", "1234-5678", "2")),
        tuple(_header_cell(v) for v in ("ECON", "Econometrica", "0012-9682", "4*")),
    ]

    fake_sheet = type("Sheet", (), {})()
    fake_sheet.iter_rows = lambda: iter(fake_rows)

    class FakeWorkbook:
        sheetnames = ["Sheet1"]

        def __getitem__(self, _name: str):
            return fake_sheet

    fake_wb = FakeWorkbook()

    monkeypatch.setattr(build, "_open_workbook", lambda path: fake_wb)

    out_path = tmp_path / "journals.json"
    abs_path = tmp_path / "abs.xlsx"
    abs_path.write_text("placeholder")  # only needs to exist for is_file()

    args = [
        "build_journal_list_from_abs.py",
        "--abs-xlsx", str(abs_path),
        "--ranks", "4*",
        "--fields", "ENT-SBM",
        "--out", str(out_path),
    ]
    with patch("sys.argv", args):
        rc = build.main()
    assert rc == 0

    import json
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["rating_source"] == "ABS"
    assert payload["ranks_included"] == ["4*"]
    assert payload["fields_included"] == ["ENT-SBM"]
    assert payload["journal_count"] == 2
    titles = {r["title"] for r in payload["journals"]}
    assert titles == {"JBV", "ETP"}
    # ETP's unhyphenated input ISSN normalized in output:
    etp = next(r for r in payload["journals"] if r["title"] == "ETP")
    assert etp["issn"] == "1042-2587"
