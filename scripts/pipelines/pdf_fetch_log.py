"""Structured PDF-fetch failure logging.

When `enrich_pdfs.py` and the per-publisher fetchers can't get a PDF,
the cause matters for downstream adjudication: a paywalled article
flags for ILL, a book chapter flags for FE2/FE3 (out of scope), an
unindexed preprint flags for FE6 (no full text available). Without
capturing the cause structurally, the user has to free-type their
adjudication ("This is a book chapter that I have no access to" —
real example from the session log) which Claude then has to translate.

This module:
- Defines the canonical schema (`FAILURE_FIELDS`).
- Classifies failures from `(item_type, http_status)` into one of
  four causes (`FailureCause`) using rules that match the
  systematic-review skill's exclusion-code conventions.
- Appends rows to a `pdf_fetch_log.csv` at the user's chosen path,
  schema-stable + idempotent via `csv_io.upsert_by_item_key` keyed
  by `(item_key, source)` — re-running the same fetcher on the same
  item replaces the prior row instead of appending duplicates.
- Reads back the log for `audit_zotero_library.py` to group by cause
  and propose FE codes per-cause.
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

import csv_io


class FailureCause(StrEnum):
    """Why a fetch failed. Used for FE-code suggestion in audit_zotero_library."""

    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    """Item type is out of the SLR's scope (book chapter, thesis,
    preprint when scope is journal-only). The fetcher would never
    succeed for these — exclude rather than retry. Suggested FE code:
    FE2 (book chapter) or FE3 (other non-journal)."""

    ACCESS_BLOCKED = "ACCESS_BLOCKED"
    """Publisher returned a paywall / no-subscription response (HTTP
    401, 402, 403, or a known paywall-HTML body). The PDF exists, the
    user just doesn't have access via the paths the fetcher tried.
    Suggested action: flag for institutional ILL — not an exclusion."""

    UNAVAILABLE = "UNAVAILABLE"
    """No fetcher matched (DOI not in any provider) or every provider
    returned 404 / 5xx. The PDF probably doesn't exist online in any
    form the pipeline can reach. Suggested FE code: FE6 (no fulltext
    available)."""

    NETWORK_ERROR = "NETWORK_ERROR"
    """Transport-level failure (timeout, DNS, connection refused). Not
    an exclusion — retry next run. Captured for diagnostics so the
    user can spot persistent network issues."""


# CSV schema for pdf_fetch_log.csv. `attempt` is the cascade pass
# number (1-based) so a later retry pass surfaces independently.
# `source` is the fetcher name ("elsevier", "openalex", "browser_sage", …).
FAILURE_FIELDS: list[str] = [
    "timestamp", "item_key", "doi", "item_type",
    "attempt", "source", "http_status", "cause",
]

# The CSV is keyed by item_key for upsert idempotency. Re-runs replace
# the prior row instead of appending. (For multi-source per-item logs,
# the latest attempt per item wins — earlier attempts are visible only
# in version control / log file backups, which is the right trade.)
FAILURE_KEY_FIELD = "item_key"


# Item types that are out of scope by default for a journal-article SLR.
# Users with broader scope can override via the `scope_types` argument
# to `classify_failure`.
DEFAULT_OUT_OF_SCOPE_TYPES = frozenset({
    "bookSection", "book", "thesis", "preprint", "report",
    "manuscript", "presentation", "blogPost", "encyclopediaArticle",
})


def classify_failure(
    item_type: str = "",
    http_status: int | None = None,
    *,
    scope_types: frozenset[str] | None = None,
) -> FailureCause:
    """Classify a PDF-fetch failure based on item type and HTTP response.

    Resolution order:
      1. Item type in `scope_types` (default: book / thesis / preprint
         / report / manuscript / blog) → OUT_OF_SCOPE.
      2. http_status in (401, 402, 403) → ACCESS_BLOCKED.
      3. http_status in (404, 410) → UNAVAILABLE.
      4. http_status >= 500 (server error) → NETWORK_ERROR (treat as
         transient — server may recover).
      5. http_status is None and no exception info → UNAVAILABLE
         (every fetcher returned None without raising; PDF probably
         doesn't exist).

    Pure function — safe to call from any thread / fetcher.
    """
    out_of_scope = scope_types if scope_types is not None else DEFAULT_OUT_OF_SCOPE_TYPES
    if item_type and item_type in out_of_scope:
        return FailureCause.OUT_OF_SCOPE
    if http_status in (401, 402, 403):
        return FailureCause.ACCESS_BLOCKED
    if http_status in (404, 410):
        return FailureCause.UNAVAILABLE
    if http_status is not None and http_status >= 500:
        return FailureCause.NETWORK_ERROR
    return FailureCause.UNAVAILABLE


def log_failure(
    log_path: str | Path,
    *,
    item_key: str,
    doi: str = "",
    item_type: str = "",
    attempt: int = 1,
    source: str = "",
    http_status: int | None = None,
    cause: FailureCause | None = None,
) -> FailureCause:
    """Append a row to `pdf_fetch_log.csv` describing why this fetch failed.

    Returns the resolved cause (computed via `classify_failure` if not
    supplied). Schema-stable + upserted by `item_key` via
    `csv_io.upsert_by_item_key` so re-runs replace, never duplicate.

    `log_path` is created if missing. Parent dirs are auto-created.
    """
    if cause is None:
        cause = classify_failure(item_type=item_type, http_status=http_status)
    row = {
        "timestamp": datetime.now(UTC).isoformat(),
        "item_key": item_key,
        "doi": doi,
        "item_type": item_type,
        "attempt": str(attempt),
        "source": source,
        "http_status": "" if http_status is None else str(http_status),
        "cause": cause.value,
    }
    csv_io.upsert_by_item_key(
        log_path, row, FAILURE_FIELDS, key_field=FAILURE_KEY_FIELD,
    )
    return cause


def read_failures(log_path: str | Path) -> list[dict[str, str]]:
    """Read a pdf_fetch_log.csv into row dicts. Empty list if missing."""
    log_path = Path(log_path)
    if not log_path.is_file():
        return []
    with log_path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def group_by_cause(failures: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    """Bucket failure rows by their `cause` field for grouped audits."""
    out: dict[str, list[dict[str, str]]] = {}
    for row in failures:
        out.setdefault(row.get("cause", ""), []).append(row)
    return out


# Mapping from FailureCause → suggested FE / action label, for the
# audit-time adjudication report. The user can override per-item, but
# these are the defaults the report displays.
SUGGESTED_FE_CODE: dict[str, str] = {
    FailureCause.OUT_OF_SCOPE.value: "FE2 / FE3 (out of scope: non-journal item type)",
    FailureCause.ACCESS_BLOCKED.value: "Flag for ILL — paywall, full text exists",
    FailureCause.UNAVAILABLE.value: "FE6 (no fulltext available)",
    FailureCause.NETWORK_ERROR.value: "Retry next run (transport error, not an exclusion)",
}
