"""Unit tests for `shared_orchestrators` — the run-log helpers shared by
the three enrich_* orchestrators (P1).

These pin the behaviour the scripts used to implement inline, so the
extraction is provably no-behaviour-change.
"""

from __future__ import annotations

import csv

import shared_orchestrators as so
from log_schemas import (
    ABSTRACT_FETCH_FIELDS,
    DOI_ENRICH_FIELDS,
    PDF_FETCH_FIELDS,
)

# --- open_log --------------------------------------------------------


def test_open_log_writes_header_on_new_file(tmp_path) -> None:
    path = str(tmp_path / "log.csv")
    fh, writer = so.open_log(path, ABSTRACT_FETCH_FIELDS)
    writer.writerow({k: "" for k in ABSTRACT_FETCH_FIELDS})
    fh.close()

    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ABSTRACT_FETCH_FIELDS


def test_open_log_appends_without_second_header(tmp_path) -> None:
    path = str(tmp_path / "log.csv")
    fh, writer = so.open_log(path, ABSTRACT_FETCH_FIELDS)
    writer.writerow({k: "a" for k in ABSTRACT_FETCH_FIELDS})
    fh.close()

    fh, writer = so.open_log(path, ABSTRACT_FETCH_FIELDS)
    writer.writerow({k: "b" for k in ABSTRACT_FETCH_FIELDS})
    fh.close()

    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    # Exactly two data rows, one header (DictReader consumes it).
    assert len(rows) == 2
    assert rows[0]["item_key"] == "a"
    assert rows[1]["item_key"] == "b"


def test_open_log_creates_parent_dirs(tmp_path) -> None:
    path = str(tmp_path / "nested" / "deep" / "log.csv")
    fh, _ = so.open_log(path, PDF_FETCH_FIELDS)
    fh.close()
    assert (tmp_path / "nested" / "deep" / "log.csv").exists()


# --- load_done_keys --------------------------------------------------


def _write_log(path, fieldnames, rows) -> None:
    fh, writer = so.open_log(str(path), fieldnames)
    for r in rows:
        writer.writerow({**{k: "" for k in fieldnames}, **r})
    fh.close()


def test_load_done_keys_missing_file_is_empty(tmp_path) -> None:
    assert so.load_done_keys(str(tmp_path / "nope.csv"), statuses="updated") == set()


def test_load_done_keys_filters_by_status(tmp_path) -> None:
    path = tmp_path / "log.csv"
    _write_log(path, ABSTRACT_FETCH_FIELDS, [
        {"doi": "10.1/A", "status": "updated"},
        {"doi": "10.1/B", "status": "not_found"},
        {"doi": "10.1/C", "status": "updated"},
    ])
    assert so.load_done_keys(str(path), statuses="updated") == {"10.1/a", "10.1/c"}


def test_load_done_keys_normalises_strip_and_lower(tmp_path) -> None:
    path = tmp_path / "log.csv"
    _write_log(path, ABSTRACT_FETCH_FIELDS, [
        {"doi": "  10.1/MixedCase  ", "status": "updated"},
    ])
    assert so.load_done_keys(str(path), statuses="updated") == {"10.1/mixedcase"}


def test_load_done_keys_accepts_iterable_of_statuses(tmp_path) -> None:
    path = tmp_path / "log.csv"
    _write_log(path, PDF_FETCH_FIELDS, [
        {"doi": "10.1/A", "status": "attached"},
        {"doi": "10.1/B", "status": "cached"},
        {"doi": "10.1/C", "status": "failed"},
    ])
    got = so.load_done_keys(str(path), statuses=("attached", "cached"))
    assert got == {"10.1/a", "10.1/b"}


def test_load_done_keys_custom_key_field(tmp_path) -> None:
    path = tmp_path / "log.csv"
    _write_log(path, DOI_ENRICH_FIELDS, [
        {"item_key": "ABCD1234", "status": "updated"},
    ])
    assert so.load_done_keys(
        str(path), statuses="updated", key_field="item_key",
    ) == {"abcd1234"}


# The `LogManager` class these tests used to cover was removed: it was
# extracted alongside `open_log` / `load_done_keys` but no orchestrator ever
# adopted it. See the module docstring in shared_orchestrators.py.
