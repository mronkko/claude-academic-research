"""Tests for `pdf_run_report`.

This report exists because a live run left the user asking the same
question three times — "why do we have so many fulltexts not
available?", then "79 is too many. What is missing.", then "No, try
harder. Report what is missing." — while the answer sat unread in
`pdf_attach_log.csv`. The assertions below are about that: every
outcome counted, unresolved items itemised, and each failure bucket
carrying the action that actually resolves it.
"""

from __future__ import annotations

import pdf_run_report as report


def _row(**kw) -> dict[str, str]:
    base = {
        "run_date": "2026-08-13", "item_key": "K1", "doi": "10.1/x",
        "title": "A paper", "status": "attached", "source": "openalex",
        "detail": "",
    }
    base.update(kw)
    return base


# --- collapsing the append-only log -----------------------------------

def test_latest_rows_keeps_only_the_final_outcome_per_item() -> None:
    """The log is append-only and never deduped, so an item retried
    across runs has a row per attempt. Counting those naively would
    keep reporting a stale `upload_failed` for an item that has since
    attached cleanly."""
    rows = [
        _row(item_key="K1", status="upload_failed"),
        _row(item_key="K1", status="attached"),
        _row(item_key="K2", status="skipped_no_pdf"),
    ]
    latest = report.latest_rows(rows)
    assert len(latest) == 2
    by_key = {r["item_key"]: r["status"] for r in latest}
    assert by_key == {"K1": "attached", "K2": "skipped_no_pdf"}


def test_latest_rows_ignores_rows_with_no_identity() -> None:
    assert report.latest_rows([_row(item_key="", doi="")]) == []


# --- counting ---------------------------------------------------------

def test_every_status_is_counted_not_just_the_three_old_counters() -> None:
    """`attached / no_pdf / failed` covered 3 of 14 possible statuses;
    the rest existed only as CSV rows nothing ever read."""
    rows = [
        _row(item_key="A", status="attached"),
        _row(item_key="B", status="upload_failed"),
        _row(item_key="C", status="skipped_no_library_coverage"),
        _row(item_key="D", status="connector_save_failed"),
        _row(item_key="E", status="rejected_corrupt_pdf"),
        _row(item_key="F", status="attached_no_text"),
    ]
    text = report.format_report(rows)
    for status in (
        "upload_failed", "skipped_no_library_coverage",
        "connector_save_failed", "rejected_corrupt_pdf", "attached_no_text",
    ):
        assert status in text, f"{status} missing from report"


def test_header_separates_resolved_from_still_missing() -> None:
    rows = [
        _row(item_key="A", status="attached"),
        _row(item_key="B", status="attached_via_connector"),
        _row(item_key="C", status="skipped_no_pdf"),
    ]
    text = report.format_report(rows)
    assert "2 with a PDF attached" in text
    assert "1 still without" in text


def test_attached_no_text_counts_as_still_missing() -> None:
    """It has a file, but nothing downstream can read it — reporting it
    as resolved would hide the item."""
    text = report.format_report([_row(item_key="A", status="attached_no_text")])
    assert "0 with a PDF attached" in text
    assert "1 still without" in text


# --- guidance ---------------------------------------------------------

def test_upload_failed_is_not_described_as_an_access_problem() -> None:
    """The original misdiagnosis: 48 fetched-but-unattached PDFs were
    reported as unreachable paywalls. The file is in the cache."""
    text = report.format_report([_row(status="upload_failed")])
    assert "cache" in text.lower()
    assert "NOT an access problem" in text


def test_textless_guidance_leads_with_a_bad_copy_not_ocr() -> None:
    """"It's a scan, needs OCR" was proposed twice during the incident
    and was wrong both times — 0 of the 5 textless files were scans; all
    5 came back intact from a different source. The report must lead
    with the remediation that worked."""
    text = report.format_report([_row(status="attached_no_text")])
    assert "NOT a scan" in text
    assert "different source" in text
    # OCR may appear, but only as the fallback after a second source
    # returns the same file — never as the headline.
    lead = text.split("OCR")[0]
    assert "different source" in lead


def test_textless_guidance_points_at_the_non_destructive_retry() -> None:
    """The item looks complete and every run skips it, so the guidance has
    to name the way past that gate. It used to say "delete the existing
    attachment first", which made the retry destructive by construction:
    you had to give up the only copy you had before finding out whether a
    replacement would arrive. `--replace` swaps on success instead."""
    text = report.format_report([_row(status="attached_no_text")])
    assert "--replace" in text


def test_corrupt_pdf_guidance_points_at_a_different_source() -> None:
    """Retrying the same provider returned byte-identical broken files;
    only another source helped."""
    text = report.format_report([_row(status="rejected_corrupt_pdf")])
    assert "DIFFERENT source" in text


def test_library_coverage_guidance_tells_the_user_to_verify() -> None:
    """This bucket produced 16 false negatives against journals the
    user demonstrably had access to."""
    text = report.format_report([_row(status="skipped_no_library_coverage")])
    assert "--ignore-library-coverage" in text


def test_unknown_status_does_not_crash_the_report() -> None:
    text = report.format_report([_row(status="something_new")])
    assert "something_new" in text


# --- itemisation ------------------------------------------------------

def test_unresolved_items_are_listed_with_a_doi() -> None:
    text = report.format_report([
        _row(item_key="K9", status="skipped_no_pdf",
             title="Organizational Spirituality", doi="10.1007/s10551-1"),
    ])
    assert "Organizational Spirituality" in text
    assert "https://doi.org/10.1007/s10551-1" in text


def test_metadata_lookup_upgrades_lines_to_real_citations() -> None:
    """The user's closing request was literally "give me the citations
    of the missing PDFs and I will investigate"."""
    def meta(key: str) -> dict[str, str]:
        assert key == "K9"
        return {
            "authors": "Smith & Jones", "year": "2021",
            "journal": "Journal of Business Ethics", "title": "Real Title",
        }

    text = report.format_report(
        [_row(item_key="K9", status="skipped_no_pdf")], metadata=meta,
    )
    assert "Smith & Jones (2021)" in text
    assert "Journal of Business Ethics" in text
    assert "Real Title" in text


def test_resolved_items_are_counted_but_not_itemised() -> None:
    text = report.format_report([
        _row(item_key="A", status="attached", title="Attached Paper"),
        _row(item_key="B", status="skipped_no_pdf", title="Missing Paper"),
    ])
    assert "Missing Paper" in text
    assert "Attached Paper" not in text


def test_detail_is_surfaced_under_the_item() -> None:
    """The whole point of the `detail` column: the reason used to be
    printed to stdout and then dropped."""
    text = report.format_report([
        _row(status="upload_failed", detail="ConnectError: connection reset"),
    ])
    assert "ConnectError: connection reset" in text


def test_bucket_itemisation_can_be_capped() -> None:
    rows = [_row(item_key=f"K{i}", status="skipped_no_pdf") for i in range(10)]
    text = report.format_report(rows, max_items_per_bucket=3)
    assert "and 7 more" in text


def test_empty_log_reports_cleanly() -> None:
    assert "no log rows" in report.format_report([])
