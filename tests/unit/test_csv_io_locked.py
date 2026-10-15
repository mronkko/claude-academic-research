"""A CSV held open by Excel does not kill a screening run (A6).

On Windows `os.replace` onto a file Excel has open raises PermissionError;
the run used to die on it mid-corpus.
"""

from __future__ import annotations

import csv

import csv_io
import pytest

SCHEMA = ["item_key", "decision"]


def _flaky_replace(monkeypatch, failures: int) -> list:
    real = csv_io.os.replace
    calls: list = []

    def replace(src, dst):
        calls.append(dst)
        if len(calls) <= failures:
            raise PermissionError(13, "The process cannot access the file")
        return real(src, dst)

    monkeypatch.setattr(csv_io.os, "replace", replace)
    monkeypatch.setattr(csv_io.time, "sleep", lambda s: None)
    return calls


def test_a_briefly_locked_csv_is_written_after_a_retry(
    tmp_path, monkeypatch, capsys,
) -> None:
    calls = _flaky_replace(monkeypatch, failures=2)
    path = tmp_path / "log.csv"
    csv_io.upsert_by_item_key(path, {"item_key": "K1", "decision": "include"},
                              SCHEMA)
    assert len(calls) == 3
    with path.open(newline="") as fh:
        assert list(csv.DictReader(fh)) == [{"item_key": "K1",
                                             "decision": "include"}]
    assert "Excel" in capsys.readouterr().err


def test_a_csv_that_stays_locked_fails_with_the_reason(
    tmp_path, monkeypatch,
) -> None:
    _flaky_replace(monkeypatch, failures=99)
    path = tmp_path / "log.csv"
    with pytest.raises(PermissionError, match="open in Excel"):
        csv_io.upsert_by_item_key(path, {"item_key": "K1"}, SCHEMA)
    # No temp file left behind.
    assert list(tmp_path.iterdir()) == []
