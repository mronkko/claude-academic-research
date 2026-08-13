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
