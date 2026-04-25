"""Tests for `csv_io.upsert_by_item_key` — schema-stable, idempotent CSV writer.

This is the upstream fix for T2-3: every screening / coding writer
routes through `upsert_by_item_key`, so re-runs replace prior rows
instead of appending duplicates. Without this, three independent
writers (abstract_screen.py, fulltext_code.py, manual adjudication)
drift schemas and the user has to run a "repair_fulltext_csv.py"
band-aid script to dedup. The test below pins the contract that
makes the band-aid unnecessary.
"""

from __future__ import annotations

from pathlib import Path

import csv_io
import pytest

SCHEMA = ["item_key", "decision", "reason", "model"]


def _read_rows(path: Path) -> list[dict[str, str]]:
    import csv
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_upsert_creates_file_with_header_on_first_write(tmp_path: Path) -> None:
    target = tmp_path / "screening.csv"
    csv_io.upsert_by_item_key(
        target,
        {"item_key": "AAAA0001", "decision": "include", "reason": "fits scope", "model": "haiku"},
        SCHEMA,
    )
    text = target.read_text(encoding="utf-8")
    assert text.splitlines()[0] == ",".join(SCHEMA)
    rows = _read_rows(target)
    assert rows == [{
        "item_key": "AAAA0001", "decision": "include",
        "reason": "fits scope", "model": "haiku",
    }]


def test_upsert_replaces_existing_row_for_same_item_key(tmp_path: Path) -> None:
    """The contract that makes 'repair' scripts unnecessary: re-running
    on the same item replaces, never duplicates."""
    target = tmp_path / "screening.csv"
    csv_io.upsert_by_item_key(target, {
        "item_key": "AAAA0001", "decision": "borderline",
        "reason": "first pass", "model": "haiku",
    }, SCHEMA)
    csv_io.upsert_by_item_key(target, {
        "item_key": "AAAA0001", "decision": "include",
        "reason": "second pass — adjudicated", "model": "sonnet",
    }, SCHEMA)
    rows = _read_rows(target)
    assert len(rows) == 1
    assert rows[0]["decision"] == "include"
    assert rows[0]["reason"] == "second pass — adjudicated"
    assert rows[0]["model"] == "sonnet"


def test_upsert_appends_distinct_item_keys(tmp_path: Path) -> None:
    target = tmp_path / "screening.csv"
    for ikey, dec in [("AAAA0001", "include"), ("AAAA0002", "exclude"), ("AAAA0003", "borderline")]:
        csv_io.upsert_by_item_key(target, {
            "item_key": ikey, "decision": dec, "reason": "", "model": "haiku",
        }, SCHEMA)
    rows = _read_rows(target)
    assert [r["item_key"] for r in rows] == ["AAAA0001", "AAAA0002", "AAAA0003"]
    assert [r["decision"] for r in rows] == ["include", "exclude", "borderline"]


def test_upsert_fills_missing_columns_with_empty_string(tmp_path: Path) -> None:
    """Per-writer specialisation: a writer that only computes some
    fields (e.g. manual FE6 tagging only knows decision + reason)
    must still produce rows with every schema column. Empty fillers
    keep the file uniform across writers."""
    target = tmp_path / "screening.csv"
    csv_io.upsert_by_item_key(
        target,
        {"item_key": "AAAA0001", "decision": "exclude"},  # no reason, no model
        SCHEMA,
    )
    rows = _read_rows(target)
    assert rows == [{
        "item_key": "AAAA0001", "decision": "exclude",
        "reason": "", "model": "",
    }]


def test_upsert_collapses_pre_existing_duplicates_on_replace(tmp_path: Path) -> None:
    """Defensive: if an existing CSV already has duplicate rows for an
    item_key (e.g. left by an old append-only writer before we ship the
    fix), re-upserting that item collapses them into a single row."""
    target = tmp_path / "screening.csv"
    target.write_text(
        "item_key,decision,reason,model\n"
        "AAAA0001,include,first,haiku\n"
        "AAAA0001,borderline,second,haiku\n"
        "AAAA0002,exclude,unrelated,haiku\n",
        encoding="utf-8",
    )
    csv_io.upsert_by_item_key(target, {
        "item_key": "AAAA0001", "decision": "exclude",
        "reason": "adjudicated", "model": "manual",
    }, SCHEMA)
    rows = _read_rows(target)
    assert len(rows) == 2
    assert rows[0] == {
        "item_key": "AAAA0001", "decision": "exclude",
        "reason": "adjudicated", "model": "manual",
    }
    assert rows[1]["item_key"] == "AAAA0002"


def test_upsert_raises_on_schema_mismatch(tmp_path: Path) -> None:
    """If the file on disk uses a different header (e.g. a stale
    schema from before a column was added), refuse to write and
    surface both schemas in the error. Auto-rewriting a header would
    silently mix two writers' columns, which is the bug we're trying
    to prevent."""
    target = tmp_path / "screening.csv"
    target.write_text("item_key,decision,reason\nAAAA0001,include,early\n", encoding="utf-8")
    with pytest.raises(csv_io.SchemaMismatchError) as exc_info:
        csv_io.upsert_by_item_key(target, {
            "item_key": "AAAA0002", "decision": "exclude",
            "reason": "later", "model": "haiku",
        }, SCHEMA)
    assert exc_info.value.expected == SCHEMA
    assert exc_info.value.actual == ["item_key", "decision", "reason"]


def test_upsert_requires_key_field_in_schema() -> None:
    with pytest.raises(ValueError):
        csv_io.upsert_by_item_key(
            Path("/tmp/never-written"),
            {"item_key": "AAAA0001", "decision": "include"},
            ["decision", "reason"],  # no item_key
        )


def test_upsert_requires_non_empty_key_value(tmp_path: Path) -> None:
    target = tmp_path / "screening.csv"
    with pytest.raises(ValueError):
        csv_io.upsert_by_item_key(
            target,
            {"item_key": "", "decision": "include", "reason": "", "model": "haiku"},
            SCHEMA,
        )


def test_upsert_creates_parent_directory(tmp_path: Path) -> None:
    target = tmp_path / "screening" / "abstract_screening.csv"
    assert not target.parent.exists()
    csv_io.upsert_by_item_key(
        target,
        {"item_key": "AAAA0001", "decision": "include", "reason": "", "model": "haiku"},
        SCHEMA,
    )
    assert target.parent.is_dir()
    assert target.is_file()


def test_upsert_atomic_write_does_not_leave_tempfile(tmp_path: Path) -> None:
    """Verify we clean up after ourselves — no `.csv.<id>.tmp` files
    lingering after a successful write. A failed write should also not
    leave one (covered indirectly by the error-path test below)."""
    target = tmp_path / "screening.csv"
    csv_io.upsert_by_item_key(
        target,
        {"item_key": "AAAA0001", "decision": "include", "reason": "", "model": "haiku"},
        SCHEMA,
    )
    leftover = list(tmp_path.glob(".screening.csv.*.tmp"))
    assert leftover == []


def test_upsert_pads_old_rows_to_current_schema_when_columns_match(
    tmp_path: Path,
) -> None:
    """Writers that gain new columns over time: if the file's existing
    header matches the schema exactly (no SchemaMismatchError), but
    historical rows have fewer fields than headers (legacy from a
    misbehaving writer), upsert pads them with empty strings rather
    than carrying garbage."""
    target = tmp_path / "screening.csv"
    target.write_text(
        # Header has all 4 columns; historical row only has 3 values
        # (csv.DictReader returns "" for the missing field).
        "item_key,decision,reason,model\n"
        "AAAA0001,include,first\n",
        encoding="utf-8",
    )
    csv_io.upsert_by_item_key(target, {
        "item_key": "AAAA0002", "decision": "exclude",
        "reason": "scope", "model": "haiku",
    }, SCHEMA)
    rows = _read_rows(target)
    assert len(rows) == 2
    assert rows[0] == {
        "item_key": "AAAA0001", "decision": "include",
        "reason": "first", "model": "",
    }
