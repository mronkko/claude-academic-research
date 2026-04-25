"""Tests for `pdf_fetch_log` — structured PDF-fetch failure logging (T4-3).

Captures the cause at fetch time so audit_zotero_library can group by
cause and suggest FE codes during adjudication, replacing the user's
free-text ("This is a book chapter that I have no access to") flow
from the session log.
"""

from __future__ import annotations

from pathlib import Path

import pdf_fetch_log

# ---------------------------------------------------------------------------
# classify_failure
# ---------------------------------------------------------------------------


def test_classify_book_chapter_is_out_of_scope() -> None:
    """Book chapters never become journal-article includes — surface
    early so the user adjudicates with FE2/FE3 in one tick."""
    assert (
        pdf_fetch_log.classify_failure(item_type="bookSection", http_status=200)
        == pdf_fetch_log.FailureCause.OUT_OF_SCOPE
    )


def test_classify_thesis_preprint_report_are_out_of_scope() -> None:
    for it in ("thesis", "preprint", "report", "manuscript", "blogPost"):
        assert (
            pdf_fetch_log.classify_failure(item_type=it)
            == pdf_fetch_log.FailureCause.OUT_OF_SCOPE
        ), it


def test_classify_paywall_is_access_blocked() -> None:
    """401 / 402 / 403 mean the publisher returned a paywall — full text
    exists, user just lacks the entitlement. Suggest ILL, not exclude."""
    for status in (401, 402, 403):
        assert (
            pdf_fetch_log.classify_failure(item_type="journalArticle", http_status=status)
            == pdf_fetch_log.FailureCause.ACCESS_BLOCKED
        ), status


def test_classify_404_is_unavailable() -> None:
    assert (
        pdf_fetch_log.classify_failure(item_type="journalArticle", http_status=404)
        == pdf_fetch_log.FailureCause.UNAVAILABLE
    )


def test_classify_5xx_is_network_error() -> None:
    """Server errors are transient — treat as retry, not exclude."""
    for status in (500, 502, 503, 504):
        assert (
            pdf_fetch_log.classify_failure(item_type="journalArticle", http_status=status)
            == pdf_fetch_log.FailureCause.NETWORK_ERROR
        ), status


def test_classify_no_status_falls_back_to_unavailable() -> None:
    """When every fetcher returned None without raising, the PDF
    probably doesn't exist online in any form we can reach."""
    assert (
        pdf_fetch_log.classify_failure(item_type="journalArticle", http_status=None)
        == pdf_fetch_log.FailureCause.UNAVAILABLE
    )


def test_classify_custom_scope_overrides_defaults() -> None:
    """A SLR that includes book chapters can pass an empty scope set;
    bookSection then falls back to status-based classification."""
    assert (
        pdf_fetch_log.classify_failure(
            item_type="bookSection",
            http_status=403,
            scope_types=frozenset(),  # everything is in scope
        )
        == pdf_fetch_log.FailureCause.ACCESS_BLOCKED
    )


def test_classify_unknown_status_is_unavailable() -> None:
    """Status codes we don't classify (e.g. 200 with empty body) fall
    through to UNAVAILABLE rather than getting silently labeled."""
    assert (
        pdf_fetch_log.classify_failure(item_type="journalArticle", http_status=200)
        == pdf_fetch_log.FailureCause.UNAVAILABLE
    )


# ---------------------------------------------------------------------------
# log_failure / read_failures / group_by_cause
# ---------------------------------------------------------------------------


def test_log_failure_writes_row_with_full_schema(tmp_path: Path) -> None:
    log_path = tmp_path / "pdf_fetch_log.csv"
    pdf_fetch_log.log_failure(
        log_path,
        item_key="ITEM0001",
        doi="10.1016/j.respol.2020.01.001",
        item_type="journalArticle",
        attempt=1,
        source="elsevier",
        http_status=403,
    )
    rows = pdf_fetch_log.read_failures(log_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["item_key"] == "ITEM0001"
    assert row["doi"] == "10.1016/j.respol.2020.01.001"
    assert row["item_type"] == "journalArticle"
    assert row["source"] == "elsevier"
    assert row["http_status"] == "403"
    assert row["cause"] == pdf_fetch_log.FailureCause.ACCESS_BLOCKED.value
    assert row["timestamp"]  # populated; non-empty


def test_log_failure_uses_explicit_cause_override(tmp_path: Path) -> None:
    """Caller can pass a cause directly when it has more context than
    the (item_type, http_status) signature can convey — e.g. P11's
    Elsevier preview detection knows the response was a preview, not a
    paywall."""
    log_path = tmp_path / "log.csv"
    pdf_fetch_log.log_failure(
        log_path,
        item_key="ITEM0002",
        item_type="journalArticle",
        http_status=200,  # would default to UNAVAILABLE
        cause=pdf_fetch_log.FailureCause.ACCESS_BLOCKED,
    )
    rows = pdf_fetch_log.read_failures(log_path)
    assert rows[0]["cause"] == pdf_fetch_log.FailureCause.ACCESS_BLOCKED.value


def test_log_failure_upserts_by_item_key(tmp_path: Path) -> None:
    """Re-running on the same item replaces the prior row — last cause
    wins. Important for cascade re-runs that reach a different fetcher
    on the second pass."""
    log_path = tmp_path / "log.csv"
    pdf_fetch_log.log_failure(
        log_path,
        item_key="ITEM0003", source="crossref",
        item_type="journalArticle", http_status=404,
    )
    pdf_fetch_log.log_failure(
        log_path,
        item_key="ITEM0003", source="elsevier",
        item_type="journalArticle", http_status=403,
    )
    rows = pdf_fetch_log.read_failures(log_path)
    assert len(rows) == 1
    assert rows[0]["source"] == "elsevier"
    assert rows[0]["cause"] == pdf_fetch_log.FailureCause.ACCESS_BLOCKED.value


def test_read_failures_returns_empty_list_for_missing_file(tmp_path: Path) -> None:
    assert pdf_fetch_log.read_failures(tmp_path / "nope.csv") == []


def test_group_by_cause_buckets_rows() -> None:
    failures = [
        {"item_key": "A", "cause": pdf_fetch_log.FailureCause.ACCESS_BLOCKED.value},
        {"item_key": "B", "cause": pdf_fetch_log.FailureCause.OUT_OF_SCOPE.value},
        {"item_key": "C", "cause": pdf_fetch_log.FailureCause.ACCESS_BLOCKED.value},
        {"item_key": "D", "cause": pdf_fetch_log.FailureCause.UNAVAILABLE.value},
    ]
    grouped = pdf_fetch_log.group_by_cause(failures)
    assert {k: len(v) for k, v in grouped.items()} == {
        pdf_fetch_log.FailureCause.ACCESS_BLOCKED.value: 2,
        pdf_fetch_log.FailureCause.OUT_OF_SCOPE.value: 1,
        pdf_fetch_log.FailureCause.UNAVAILABLE.value: 1,
    }


def test_suggested_fe_code_covers_every_cause() -> None:
    """If a new cause is added, force the suggestion table to be
    updated alongside — a missing entry would show as an empty action
    line in the audit report and confuse the user."""
    for cause in pdf_fetch_log.FailureCause:
        assert cause.value in pdf_fetch_log.SUGGESTED_FE_CODE
        assert pdf_fetch_log.SUGGESTED_FE_CODE[cause.value]  # non-empty
