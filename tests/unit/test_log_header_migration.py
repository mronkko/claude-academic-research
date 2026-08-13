"""`open_log` must migrate a run-log when a column is added.

`open_log` appends, and only writes a header for a brand-new file. So
adding `detail` to `PDF_FETCH_FIELDS` would otherwise make every new row
carry seven values under a six-column header — silently misaligning the
accumulated log of anyone who upgrades mid-project, and corrupting the
resume set that `load_done_keys` reads back out of it.

Migration is deliberately limited to pure column *addition* (the
existing header must be an ordered subset). Reordering or renaming is
left alone rather than guessed at — same rule as
`csv_io.upsert_by_item_key`.
"""

from __future__ import annotations

import csv

import shared_orchestrators
from log_schemas import PDF_FETCH_FIELDS

OLD_FIELDS = ["run_date", "item_key", "doi", "title", "status", "source"]


def _write(path, header, rows) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=header)
        writer.writeheader()
        writer.writerows(rows)


def _read(path) -> tuple[list[str], list[dict]]:
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return list(reader.fieldnames or []), list(reader)


def test_existing_six_column_log_is_migrated(tmp_path) -> None:
    path = tmp_path / "pdf_attach_log.csv"
    _write(path, OLD_FIELDS, [{
        "run_date": "2026-08-13", "item_key": "K1", "doi": "10.1/x",
        "title": "T", "status": "attached", "source": "sage",
    }])

    fh, writer = shared_orchestrators.open_log(str(path), PDF_FETCH_FIELDS)
    writer.writerow({
        "run_date": "2026-08-14", "item_key": "K2", "doi": "10.1/y",
        "title": "U", "status": "upload_failed", "source": "sage",
        "detail": "ConnectError: reset",
    })
    fh.close()

    header, rows = _read(path)
    assert header == PDF_FETCH_FIELDS
    assert len(rows) == 2
    # Historical row keeps its data and gains an empty new column.
    assert rows[0]["item_key"] == "K1"
    assert rows[0]["status"] == "attached"
    assert rows[0]["detail"] == ""
    # New row lands in the right columns, not shifted by one.
    assert rows[1]["item_key"] == "K2"
    assert rows[1]["detail"] == "ConnectError: reset"


def test_resume_set_survives_the_migration(tmp_path) -> None:
    """The practical consequence of misalignment: `status` would shift
    out from under `load_done_keys` and the run would re-fetch
    everything it had already attached."""
    path = tmp_path / "pdf_attach_log.csv"
    _write(path, OLD_FIELDS, [{
        "run_date": "2026-08-13", "item_key": "K1", "doi": "10.1/DoneAlready",
        "title": "T", "status": "attached", "source": "sage",
    }])

    fh, _ = shared_orchestrators.open_log(str(path), PDF_FETCH_FIELDS)
    fh.close()

    done = shared_orchestrators.load_done_keys(
        str(path), statuses=("attached",), key_field="doi",
    )
    assert done == {"10.1/donealready"}


def test_already_current_log_is_left_alone(tmp_path) -> None:
    path = tmp_path / "log.csv"
    row = {c: c.upper() for c in PDF_FETCH_FIELDS}
    _write(path, PDF_FETCH_FIELDS, [row])
    before = path.read_bytes()

    fh, _ = shared_orchestrators.open_log(str(path), PDF_FETCH_FIELDS)
    fh.close()

    assert path.read_bytes() == before


def test_new_file_gets_a_header(tmp_path) -> None:
    path = tmp_path / "fresh.csv"
    fh, writer = shared_orchestrators.open_log(str(path), PDF_FETCH_FIELDS)
    writer.writerow({c: "" for c in PDF_FETCH_FIELDS})
    fh.close()

    header, rows = _read(path)
    assert header == PDF_FETCH_FIELDS
    assert len(rows) == 1


def test_unrelated_header_is_not_rewritten(tmp_path) -> None:
    """Not a pure addition — reordering or renaming is not something
    this helper should guess at, so it leaves the file untouched."""
    path = tmp_path / "weird.csv"
    odd = ["status", "run_date", "item_key", "doi", "title", "source"]
    _write(path, odd, [])
    before = path.read_bytes()

    fh, _ = shared_orchestrators.open_log(str(path), PDF_FETCH_FIELDS)
    fh.close()

    assert path.read_bytes() == before


def test_empty_existing_file_is_not_a_crash(tmp_path) -> None:
    path = tmp_path / "empty.csv"
    path.write_text("")
    fh, _ = shared_orchestrators.open_log(str(path), PDF_FETCH_FIELDS)
    fh.close()


# --- headerless logs --------------------------------------------------
#
# `open_log` writes a header only when it *creates* the file, so a log
# first touched by anything else accumulates rows with no header. A real
# user's `pdf_attach_log.csv` is in this state, 1813 rows deep — and a
# plain DictReader on it silently consumes the first record as column
# names and mislabels every field thereafter.

HEADERLESS = (
    "2026-08-05,ZFPNPJI7,10.1177/0149,The Use of Trajectories,skipped_no_pdf,\n"
    "2026-08-13,KGBSSZ3W,10.1177/1094,Eight Simple Guidelines,attached,sage\n"
)


def test_has_header_detects_a_headerless_log(tmp_path) -> None:
    path = tmp_path / "no_header.csv"
    path.write_text(HEADERLESS)
    assert shared_orchestrators.has_header(str(path), PDF_FETCH_FIELDS) is False


def test_has_header_detects_a_real_header(tmp_path) -> None:
    path = tmp_path / "with_header.csv"
    _write(path, PDF_FETCH_FIELDS, [])
    assert shared_orchestrators.has_header(str(path), PDF_FETCH_FIELDS) is True


def test_headerless_rows_are_read_without_losing_the_first(tmp_path) -> None:
    path = tmp_path / "no_header.csv"
    path.write_text(HEADERLESS)

    rows = shared_orchestrators.read_log_rows(str(path), PDF_FETCH_FIELDS)
    assert len(rows) == 2, "the first record was eaten as a header row"
    assert rows[0]["item_key"] == "ZFPNPJI7"
    assert rows[0]["status"] == "skipped_no_pdf"
    # Short row (written before `detail` existed) fills, not None.
    assert rows[0]["detail"] == ""


def test_headerless_log_gains_a_header_on_open(tmp_path) -> None:
    path = tmp_path / "no_header.csv"
    path.write_text(HEADERLESS)

    fh, writer = shared_orchestrators.open_log(str(path), PDF_FETCH_FIELDS)
    writer.writerow({
        "run_date": "2026-08-14", "item_key": "NEW1", "doi": "10.1/z",
        "title": "T", "status": "attached", "source": "cache", "detail": "",
    })
    fh.close()

    header, rows = _read(path)
    assert header == PDF_FETCH_FIELDS
    assert [r["item_key"] for r in rows] == ["ZFPNPJI7", "KGBSSZ3W", "NEW1"]
    assert rows[1]["source"] == "sage"


def test_headerless_resume_set_is_correct(tmp_path) -> None:
    """Before the header fix, `load_done_keys` on this file would read
    the first data row as column names and find nothing."""
    path = tmp_path / "no_header.csv"
    path.write_text(HEADERLESS)

    fh, _ = shared_orchestrators.open_log(str(path), PDF_FETCH_FIELDS)
    fh.close()

    done = shared_orchestrators.load_done_keys(
        str(path), statuses=("attached",), key_field="doi",
    )
    assert done == {"10.1177/1094"}
