"""Canonical column lists for screening / coding CSV logs.

Single source of truth so every writer (`abstract_screen.py`,
`fulltext_code.py`, manual adjudication paths) emits the same shape.
Without this, three independent writers drift and the CSV ends up
needing a "repair" pass — the workaround that motivated this module.
"""

from __future__ import annotations

# --- Stage 1: abstract screening -------------------------------------

ABSTRACT_SCREENING_FIELDS: list[str] = [
    "timestamp", "item_key", "doi", "title", "source", "query",
    "decision", "reason", "model", "prompt_version",
]

# --- Stage 2: full-text coding ---------------------------------------
#
# Two-part schema: a fixed base (provenance + decision metadata) and
# a project-specific block of coded fields defined by the user's
# `screening_config.FULLTEXT_CODING_FIELDS`. `fulltext_screening_fields`
# composes them in the canonical order. Writers should always pass the
# full column list to `csv.DictWriter` and supply empty strings for any
# field they don't compute, so every row in the CSV has the same shape
# regardless of which writer produced it.

FULLTEXT_BASE_FIELDS: list[str] = [
    "timestamp", "item_key", "doi", "title", "year", "journal",
    "pdf_path", "fulltext_chars", "truncated",
    "decision", "exclusion_code", "reason",
]

FULLTEXT_TRAILING_FIELDS: list[str] = [
    "model", "prompt_version",
]


def fulltext_screening_fields(coding_field_names: list[str]) -> list[str]:
    """Compose the canonical full-text-screening column list.

    `coding_field_names` is the project-specific block, taken from
    `screening_config.FULLTEXT_CODING_FIELDS` (each entry's `name`).
    Order is fixed: provenance/decision base → coded fields → model
    metadata trailers.
    """
    return FULLTEXT_BASE_FIELDS + list(coding_field_names) + FULLTEXT_TRAILING_FIELDS


# --- Enrichment run-logs ---------------------------------------------
#
# The three enrich_* orchestrators each append a run-log CSV (and the
# resumable ones read it back to skip done items). These used to be
# inline `LOG_FIELDS` lists, one per script — adding a column meant
# editing every script in sync. They live here so the shared
# `shared_orchestrators` helpers and the scripts share one definition.

ABSTRACT_FETCH_FIELDS: list[str] = [
    "run_date", "item_key", "doi", "title", "source", "status",
]

# Schema for `enrich_pdfs.py`'s run-log, `output/pdf_attach_log.csv`.
# (The name predates the split; the *failure* log `pdf_fetch_log.csv` has
# its own schema in `pdf_fetch_log.FAILURE_FIELDS`. Renaming this constant
# would touch every caller for no behavioural gain, so the name stays and
# this comment carries the correction.)
#
# Note the column order differs from ABSTRACT_FETCH_FIELDS (status before
# source); kept as-is so existing files stay readable.
#
# `detail` carries the failure reason for non-success statuses —
# exception type + message, HTTP status where known. Without it an
# `upload_failed` row was bare and the only copy of the reason lived in
# terminal scrollback, which is how a live run lost 48 downloaded PDFs
# without a diagnosable trace. Appended last; `shared_orchestrators.
# open_log` migrates existing 6-column logs on open.
PDF_FETCH_FIELDS: list[str] = [
    "run_date", "item_key", "doi", "title", "status", "source", "detail",
]

DOI_ENRICH_FIELDS: list[str] = [
    "run_date", "item_key",
    "zotero_doi", "zotero_title", "zotero_year",
    "crossref_doi", "crossref_title", "crossref_authors",
    "status",
]
