"""Schema-stable, idempotent CSV writes for screening / coding logs.

`upsert_by_item_key` is the canonical writer. Its contract:

- Every row written has exactly the columns in `schema`, in that order.
  Missing fields are stored as empty strings.
- Re-running with the same `item_key` overwrites the prior row rather
  than appending. Last-write-wins per item.
- File is created with the schema header on first write. On subsequent
  writes, the existing header must match the schema exactly — mismatch
  raises rather than silently producing a hybrid file.

Without this contract, three independent writers (`abstract_screen.py`,
`fulltext_code.py`, manual adjudication) drift schemas and append
rather than upsert; the CSV needs a "repair" pass to dedup and
schema-reconcile. That repair script is a workaround for the writers
not being idempotent in the first place.
"""

from __future__ import annotations

import csv
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path


class SchemaMismatchError(RuntimeError):
    """Raised when an existing CSV's header doesn't match the requested schema.

    Carries the conflicting columns so the caller (or the user) can decide
    whether to migrate the file or back out. We never auto-rewrite an
    existing file's schema — that hides intent.
    """

    def __init__(self, path: str | Path, expected: list[str], actual: list[str]):
        super().__init__(
            f"CSV schema mismatch at {path}:\n"
            f"  expected: {expected}\n"
            f"  actual:   {actual}"
        )
        self.path = str(path)
        self.expected = expected
        self.actual = actual


def upsert_by_item_key(
    csv_path: str | Path,
    row: Mapping[str, object],
    schema: list[str],
    *,
    key_field: str = "item_key",
) -> None:
    """Insert or replace a row in `csv_path` keyed by `row[key_field]`.

    `schema` is the canonical column list (from `log_schemas`). Every
    row written has every column; values not present in `row` become
    empty strings. Existing rows with the same `item_key` are replaced.

    File is created on first write. Concurrent writers must serialize
    externally — this function is not thread-safe and not multi-process
    safe. (Pipeline orchestrators already serialize on a `log_lock` per
    `csv_path`; this helper inherits that contract.)
    """
    if key_field not in schema:
        raise ValueError(
            f"key_field {key_field!r} must be present in schema; got {schema!r}"
        )
    key_value = str(row.get(key_field, "")).strip()
    if not key_value:
        raise ValueError(f"row is missing a non-empty {key_field!r}: {dict(row)!r}")

    csv_path = Path(csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    existing_rows: list[dict[str, str]] = []
    if csv_path.exists() and csv_path.stat().st_size > 0:
        with csv_path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            actual_header = list(reader.fieldnames or [])
            if actual_header != schema:
                raise SchemaMismatchError(csv_path, schema, actual_header)
            existing_rows = list(reader)

    # Normalise the new row to the schema, empty-fill any missing fields.
    new_row = {col: str(row.get(col, "")) for col in schema}

    # Replace if present, else append.
    replaced = False
    out_rows: list[dict[str, str]] = []
    for r in existing_rows:
        if r.get(key_field) == key_value:
            if not replaced:
                out_rows.append(new_row)
                replaced = True
            # Drop any further duplicates — last write wins, but we also
            # collapse historical accidents.
        else:
            out_rows.append(r)
    if not replaced:
        out_rows.append(new_row)

    # Atomic write: temp file in same dir, then rename. Avoids leaving
    # a half-written CSV if the process is interrupted.
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{csv_path.name}.", suffix=".tmp", dir=str(csv_path.parent),
    )
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=schema, extrasaction="ignore")
            writer.writeheader()
            for r in out_rows:
                # Empty-fill any historical row that's missing newer schema cols.
                writer.writerow({col: r.get(col, "") for col in schema})
        os.replace(tmp_path, csv_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
