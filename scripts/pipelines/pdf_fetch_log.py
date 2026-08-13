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
import os
import tempfile
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

    BROWSER_REQUIRED = "BROWSER_REQUIRED"
    """The DOI resolves to a publisher this plugin has a browser handler
    for, and that handler has not been run for this item.

    Cloudflare-gated publishers (Sage, Academy of Management, APA,
    Emerald, INFORMS, OUP, Taylor & Francis, AAA) block the plain-HTTP
    cascade before any API key matters, so the automated pass returns
    either a 403 or nothing at all. Both used to land in ACCESS_BLOCKED
    or UNAVAILABLE, whose suggested actions ("flag for ILL", "FE6, no
    fulltext available") are *wrong* here: the PDF is reachable, it just
    needs `enrich_pdfs.py --sources browser`.

    This is not an exclusion and must never be adjudicated as one until
    the browser pass has actually been tried."""

    UNAVAILABLE = "UNAVAILABLE"
    """No fetcher matched (DOI not in any provider) or every provider
    returned 404 / 5xx, *and* no untried browser handler covers the
    publisher. The PDF probably doesn't exist online in any form the
    pipeline can reach. Suggested FE code: FE6 (no fulltext
    available)."""

    NETWORK_ERROR = "NETWORK_ERROR"
    """Transport-level failure (timeout, DNS, connection refused). Not
    an exclusion — retry next run. Captured for diagnostics so the
    user can spot persistent network issues."""

    CORRUPT_DOWNLOAD = "CORRUPT_DOWNLOAD"
    """A source returned bytes that are not a usable PDF — most often a
    truncated download (correct header, missing tail). Not an exclusion
    and not an access problem: the article exists and other sources may
    serve it intact. A live run had OpenAlex hand back the same
    truncated copy on every retry while the publisher's own TDM route
    returned a perfect file, so the useful action is a different source,
    not another attempt at the same one."""

    UPLOAD_FAILED = "UPLOAD_FAILED"
    """The PDF was fetched successfully but could not be attached to
    Zotero. Categorically different from the four above: the full text
    exists and is sitting in the local cache, so this must never map to
    an exclusion code. Re-running attaches it from cache without
    re-fetching. Added after a live run silently lost 48 downloaded
    PDFs here and the audit had no way to see them."""


# CSV schema for pdf_fetch_log.csv. `attempt` is the cascade pass
# number (1-based) so a later retry pass surfaces independently.
# `source` is the fetcher name ("elsevier", "openalex", "browser_sage", …).
# `publisher` is the human-readable publisher behind the DOI, resolved
# from `doi_resolver_cache.json` — the dimension a triage report needs
# and the one thing the old schema could not express.
FAILURE_FIELDS: list[str] = [
    "timestamp", "item_key", "doi", "item_type",
    "attempt", "source", "publisher", "http_status", "cause",
]

# Keyed by (item_key, source): one row per item *per attempt*.
#
# This used to be item_key alone, which collapsed the ladder — the
# browser attempt overwrote the Crossref attempt, so "we tried the API
# and it 404'd, and we have never run the browser handler" was
# indistinguishable from "we tried everything". Triage needs both rows.
# Old single-key files still read fine: their rows simply have distinct
# `source` values already, or an empty one.
FAILURE_KEY_FIELDS: tuple[str, ...] = ("item_key", "source")

#: Deprecated alias for the pre-composite key. Kept so an out-of-tree
#: caller doesn't break; new code uses FAILURE_KEY_FIELDS.
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
    untried_browser_handler: str = "",
) -> FailureCause:
    """Classify a PDF-fetch failure based on item type and HTTP response.

    Resolution order:
      1. Item type in `scope_types` (default: book / thesis / preprint
         / report / manuscript / blog) → OUT_OF_SCOPE. Item type wins
         over everything: a book chapter behind Cloudflare is still a
         book chapter.
      2. `untried_browser_handler` non-empty → BROWSER_REQUIRED. This
         outranks both ACCESS_BLOCKED and UNAVAILABLE deliberately —
         a Cloudflare 403 and a silent miss are the *same situation*
         when a handler for that publisher exists and has not been run,
         and in both cases the right next step is the browser pass, not
         an ILL request or an FE6 exclusion.
      3. http_status in (401, 402, 403) → ACCESS_BLOCKED.
      4. http_status in (404, 410) → UNAVAILABLE.
      5. http_status >= 500 (server error) → NETWORK_ERROR (treat as
         transient — server may recover).
      6. http_status is None and no exception info → UNAVAILABLE
         (every fetcher returned None without raising; PDF probably
         doesn't exist).

    `untried_browser_handler` is the *caller's* judgement, not a lookup
    done here: only the orchestrator knows which pass it is in and
    therefore whether the handler has already had its turn. Passing a
    handler name that was already tried and failed would relabel a true
    negative as recoverable. `pdf_fetch_log` stays free of any
    `fetchers.browser` import for the same reason this function is
    documented as pure.

    Pure function — safe to call from any thread / fetcher.
    """
    out_of_scope = scope_types if scope_types is not None else DEFAULT_OUT_OF_SCOPE_TYPES
    if item_type and item_type in out_of_scope:
        return FailureCause.OUT_OF_SCOPE
    if untried_browser_handler:
        return FailureCause.BROWSER_REQUIRED
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
    publisher: str = "",
    http_status: int | None = None,
    cause: FailureCause | None = None,
    untried_browser_handler: str = "",
) -> FailureCause:
    """Append a row to `pdf_fetch_log.csv` describing why this fetch failed.

    Returns the resolved cause (computed via `classify_failure` if not
    supplied). Schema-stable + upserted by `(item_key, source)` via
    `csv_io.upsert_by_item_key`, so re-running the same source on the
    same item replaces its row while a *different* source adds one.

    `log_path` is created if missing. Parent dirs are auto-created.
    """
    if cause is None:
        cause = classify_failure(
            item_type=item_type,
            http_status=http_status,
            untried_browser_handler=untried_browser_handler,
        )
    row = {
        "timestamp": datetime.now(UTC).isoformat(),
        "item_key": item_key,
        "doi": doi,
        "item_type": item_type,
        "attempt": str(attempt),
        "source": source,
        "publisher": publisher,
        "http_status": "" if http_status is None else str(http_status),
        "cause": cause.value,
    }
    csv_io.upsert_by_item_key(
        log_path, row, FAILURE_FIELDS, key_field=FAILURE_KEY_FIELDS,
    )
    return cause


def clear_failure(log_path: str | Path, item_key: str) -> bool:
    """Drop `item_key`'s row from the failure log. True if one was removed.

    The log is keyed one-row-per-item, so without this a resolved item
    keeps its stale failure row forever and `audit_zotero_library.py`
    goes on proposing an exclusion code for a PDF that is now attached.
    Called on every successful attach.

    Missing file or missing row is a no-op. Same serialization contract
    as `csv_io.upsert_by_item_key` — caller holds the lock.
    """
    log_path = Path(log_path)
    if not log_path.is_file():
        return False
    with log_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        header = list(reader.fieldnames or [])
        all_rows = list(reader)
    rows = [r for r in all_rows if r.get("item_key") != item_key]
    if not header or len(rows) == len(all_rows):
        return False

    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{log_path.name}.", suffix=".tmp", dir=str(log_path.parent),
    )
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as out:
            writer = csv.DictWriter(out, fieldnames=header, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp_path, log_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return True


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


def latest_per_item(failures: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    """Collapse per-source rows to one verdict per item, worst-first.

    With a composite key the log holds several rows per item — one per
    source tried. Triage wants a single answer per item, and the answer
    that matters is the most *actionable* one: an item with a
    BROWSER_REQUIRED row is recoverable regardless of how many API
    sources returned 404 alongside it.
    """
    priority = {
        FailureCause.OUT_OF_SCOPE.value: 0,
        FailureCause.BROWSER_REQUIRED.value: 1,
        FailureCause.ACCESS_BLOCKED.value: 2,
        FailureCause.NETWORK_ERROR.value: 3,
        FailureCause.UNAVAILABLE.value: 4,
    }
    best: dict[str, dict[str, str]] = {}
    for row in failures:
        key = row.get("item_key", "")
        if not key:
            continue
        current = best.get(key)
        if current is None or priority.get(row.get("cause", ""), 9) < priority.get(
            current.get("cause", ""), 9
        ):
            best[key] = row
    return best


# Mapping from FailureCause → suggested FE / action label, for the
# audit-time adjudication report. The user can override per-item, but
# these are the defaults the report displays.
#
# Note BROWSER_REQUIRED's entry is deliberately NOT an FE code: it is an
# instruction to try harder. Handing an FE code to a recoverable item is
# the exact defect this cause was added to fix.
SUGGESTED_FE_CODE: dict[str, str] = {
    FailureCause.OUT_OF_SCOPE.value: "FE2 / FE3 (out of scope: non-journal item type)",
    FailureCause.BROWSER_REQUIRED.value:
        "Not an exclusion — run enrich_pdfs.py --sources browser",
    FailureCause.ACCESS_BLOCKED.value: "Flag for ILL — paywall, full text exists",
    FailureCause.UNAVAILABLE.value: "FE6 (no fulltext available)",
    FailureCause.NETWORK_ERROR.value: "Retry next run (transport error, not an exclusion)",
    FailureCause.UPLOAD_FAILED.value:
        "NOT an exclusion — PDF is in the local cache; re-run enrich_pdfs.py to attach it",
    FailureCause.CORRUPT_DOWNLOAD.value:
        "NOT an exclusion — the source served a broken file; retry via a "
        "different source (publisher TDM route or --sources browser)",
}

#: Causes that must never be adjudicated as a full-text exclusion —
#: the item is reachable, the pipeline just has not reached it yet.
#: `audit_zotero_library` and the systematic-review skill both gate on
#: this rather than re-deriving the list.
#:
#: `CORRUPT_DOWNLOAD` and `UPLOAD_FAILED` are here for the same reason
#: the other two are, and their own docstrings say so: the article
#: exists, and in the UPLOAD_FAILED case the PDF is already sitting in
#: the local cache. They arrived with the run-report work while this set
#: arrived with the triage work, and for a while nothing joined them —
#: which understated the audit's "N are recoverable" line by exactly the
#: items a re-run would have fixed for free.
RECOVERABLE_CAUSES: frozenset[str] = frozenset({
    FailureCause.BROWSER_REQUIRED.value,
    FailureCause.NETWORK_ERROR.value,
    FailureCause.CORRUPT_DOWNLOAD.value,
    FailureCause.UPLOAD_FAILED.value,
})
