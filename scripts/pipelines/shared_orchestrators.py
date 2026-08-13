"""Shared run-log helpers for the enrich_* orchestrators.

Each of `enrich_abstracts.py`, `enrich_pdfs.py`, and `enrich_dois.py`
writes an append-only CSV run-log, and the resumable ones read it back
to skip already-processed items. That logic used to be re-implemented in
all three scripts: a private `_open_log` (identical), and an
`_already_done` / `_load_done_dois` pair that differed only in the
status string it filtered on (`"updated"` vs `"attached"`). Centralising
it here means a schema or behaviour change happens in one place.

The column lists themselves live in `log_schemas` (imported by both the
scripts and any downstream template that has to read these CSVs).

Deliberately functional, not a class. An earlier `LogManager` wrapper
bundling the handle, a write lock, and the resume lookup was added here and
never adopted by any orchestrator — `enrich_pdfs` opens its log across
several phases and owns its own lock, and the other two want nothing more
than these two functions. It was removed rather than left as an unused
third way to do the same thing; don't re-add one without a caller.
"""

from __future__ import annotations

import csv
import os
import tempfile
from collections.abc import Iterable
from typing import TextIO


def has_header(path: str, fieldnames: list[str]) -> bool:
    """True if `path`'s first line is a header row for `fieldnames`.

    Run-logs in the wild are not guaranteed to have one: `open_log`
    writes a header only when it creates the file, so a log first
    touched by something else — or by a version that predated headers —
    accumulates rows with no header at all. A real user's
    `pdf_attach_log.csv` is in exactly that state, 1813 rows deep.

    Reading such a file with a plain `DictReader` silently consumes its
    first record as column names and mislabels every field after it, so
    every reader has to make this check first.

    The test is "every cell names a known column". Matching only the
    first cell would misread a *reordered* header as data — and then
    rewrite the user's file on a false premise.
    """
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            first = next(csv.reader(fh), None)
    except OSError:
        return False
    if not first:
        return False
    known = set(fieldnames)
    return all(cell.strip() in known for cell in first if cell.strip())


def read_log_rows(path: str, fieldnames: list[str]) -> list[dict[str, str]]:
    """Read a run-log into dicts, tolerating a missing header row.

    Rows shorter than `fieldnames` (written before a column was added)
    get empty strings for the missing columns rather than None, so
    callers can treat every value as a string.
    """
    if not os.path.exists(path):
        return []
    header = has_header(path, fieldnames)
    with open(path, newline="", encoding="utf-8") as fh:
        reader = (
            csv.DictReader(fh) if header
            else csv.DictReader(fh, fieldnames=fieldnames)
        )
        return [
            {k: ("" if v is None else v) for k, v in row.items() if k is not None}
            for row in reader
        ]


def _migrate_header(path: str, fieldnames: list[str]) -> None:
    """Rewrite `path` under `fieldnames` when columns have been added.

    `open_log` appends, so it only writes a header for a brand-new file.
    Without this, adding a column to a schema in `log_schemas` would make
    every subsequent row carry more values than the existing header
    declares — silently misaligning a user's accumulated run-log.

    Migration is allowed only when the file's header is an *ordered
    subset* of `fieldnames` (a pure column addition); historical rows are
    empty-filled for the new columns. Any other divergence is left alone
    — reordering or renaming is not something this helper should guess
    at. Mirrors the same rule in `csv_io.upsert_by_item_key`.
    """
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return

    if not has_header(path, fieldnames):
        # Headerless log: give it one, and pad its short rows, so
        # appended rows line up with what readers will now expect.
        rows = read_log_rows(path, fieldnames)
        _rewrite(path, fieldnames, rows)
        return

    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        actual = list(reader.fieldnames or [])
        if actual == fieldnames or not actual:
            return
        if [c for c in fieldnames if c in actual] != actual:
            return  # not a pure addition — leave the file untouched
        rows = list(reader)
    _rewrite(path, fieldnames, rows)


def _rewrite(path: str, fieldnames: list[str], rows: list[dict]) -> None:
    """Atomically rewrite `path` under `fieldnames`, empty-filling gaps."""

    parent = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=parent,
    )
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as out:
            writer = csv.DictWriter(out, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for r in rows:
                writer.writerow({col: r.get(col, "") or "" for col in fieldnames})
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def open_log(path: str, fieldnames: list[str]) -> tuple[TextIO, csv.DictWriter]:
    """Open `path` for append, writing the header row if the file is new.

    Returns `(file_handle, writer)`. The caller owns the handle and is
    responsible for closing it. Parent directories are created if needed.

    An existing file whose header is missing newly-added columns is
    migrated in place first — see `_migrate_header`.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    is_new = not os.path.exists(path)
    if not is_new:
        _migrate_header(path, fieldnames)
    fh = open(path, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(fh, fieldnames=fieldnames)
    if is_new:
        writer.writeheader()
    return fh, writer


def load_done_keys(
    path: str,
    *,
    statuses: str | Iterable[str],
    key_field: str = "doi",
) -> set[str]:
    """Return resume keys for rows whose `status` is in `statuses`.

    Each returned key is `row[key_field]` stripped and lower-cased — the
    canonical form the orchestrators compare against so a re-run skips
    items already processed. A missing log file yields an empty set.

    `statuses` accepts a single status string or an iterable of them
    (e.g. `"updated"` for abstracts, `"attached"` for PDFs).
    """
    if isinstance(statuses, str):
        statuses = (statuses,)
    wanted = set(statuses)
    if not os.path.exists(path):
        return set()
    with open(path, newline="", encoding="utf-8") as f:
        return {
            (r.get(key_field) or "").strip().lower()
            for r in csv.DictReader(f)
            if r.get("status") in wanted
        }
