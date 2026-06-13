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
"""

from __future__ import annotations

import csv
import os
import threading
from collections.abc import Iterable
from typing import TextIO


def open_log(path: str, fieldnames: list[str]) -> tuple[TextIO, csv.DictWriter]:
    """Open `path` for append, writing the header row if the file is new.

    Returns `(file_handle, writer)`. The caller owns the handle and is
    responsible for closing it. Parent directories are created if needed.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    is_new = not os.path.exists(path)
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


class LogManager:
    """Thread-safe wrapper bundling `open_log` + locked row writes.

    Convenience for orchestrators that want one object to own the run-log
    handle, its writer lock, and the resume-key lookup. The functional
    helpers `open_log` / `load_done_keys` remain available for scripts
    (like `enrich_pdfs`) that open the log in several phases and manage
    their own lock.
    """

    def __init__(self, path: str, fieldnames: list[str]) -> None:
        self.path = path
        self.fieldnames = fieldnames
        self._fh: TextIO | None = None
        self._writer: csv.DictWriter | None = None
        self._lock = threading.Lock()

    def done_keys(
        self,
        statuses: str | Iterable[str],
        key_field: str = "doi",
    ) -> set[str]:
        return load_done_keys(self.path, statuses=statuses, key_field=key_field)

    def open(self) -> LogManager:
        self._fh, self._writer = open_log(self.path, self.fieldnames)
        return self

    def write(self, row: dict) -> None:
        if self._writer is None:
            raise RuntimeError("LogManager.write called before open()")
        with self._lock:
            self._writer.writerow(row)

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
            self._writer = None

    def __enter__(self) -> LogManager:
        return self.open()

    def __exit__(self, *exc: object) -> bool:
        self.close()
        return False
