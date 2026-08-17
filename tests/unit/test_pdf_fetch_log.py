"""Tests for `pdf_fetch_log` — structured PDF-fetch failure logging (T4-3).

Captures the cause at fetch time so audit_zotero_library can group by
cause and suggest FE codes during adjudication, replacing the user's
free-text ("This is a book chapter that I have no access to") flow
from the session log.
"""

from __future__ import annotations

import csv
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
    """Every fetcher returned None without raising, *and* the caller
    says no route is left — see the `browser_pass_untried` block below
    for the far more common case where one is."""
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
# BROWSER_REQUIRED — the misdiagnosis this cause exists to prevent.
#
# On a real 244-item run, 76 Sage / Academy of Management items were
# headed for an FE6 "no fulltext available" exclusion. Every one of them
# was reachable; Cloudflare simply blocks the plain-HTTP cascade before
# any API key matters, so the cascade reported either a 403 or nothing.
# ---------------------------------------------------------------------------


def test_cloudflare_silence_is_browser_required_not_unavailable() -> None:
    """The 34-vs-76 case: no status at all, because nothing got through.

    Without an untried handler this is UNAVAILABLE → "FE6". With one, it
    is a job for `--sources browser`.
    """
    assert (
        pdf_fetch_log.classify_failure(
            item_type="journalArticle", http_status=None,
            untried_browser_handler="sage",
        )
        == pdf_fetch_log.FailureCause.BROWSER_REQUIRED
    )


def test_browser_required_outranks_access_blocked() -> None:
    """A Cloudflare 403 is not an ILL request.

    ACCESS_BLOCKED suggests "flag for ILL — paywall, full text exists",
    which sends the user to their library for an article the browser
    pass would have downloaded in the same session.
    """
    assert (
        pdf_fetch_log.classify_failure(
            item_type="journalArticle", http_status=403,
            untried_browser_handler="aom",
        )
        == pdf_fetch_log.FailureCause.BROWSER_REQUIRED
    )


def test_browser_required_outranks_404() -> None:
    assert (
        pdf_fetch_log.classify_failure(
            item_type="journalArticle", http_status=404,
            untried_browser_handler="tandf",
        )
        == pdf_fetch_log.FailureCause.BROWSER_REQUIRED
    )


def test_item_type_still_outranks_browser_required() -> None:
    """A book chapter behind Cloudflare is still out of scope."""
    assert (
        pdf_fetch_log.classify_failure(
            item_type="bookSection", http_status=403,
            untried_browser_handler="sage",
        )
        == pdf_fetch_log.FailureCause.OUT_OF_SCOPE
    )


def test_no_untried_handler_leaves_classification_unchanged() -> None:
    """Default is empty, so every pre-existing call site behaves as before."""
    for status, expected in (
        (403, pdf_fetch_log.FailureCause.ACCESS_BLOCKED),
        (404, pdf_fetch_log.FailureCause.UNAVAILABLE),
        (503, pdf_fetch_log.FailureCause.NETWORK_ERROR),
        (None, pdf_fetch_log.FailureCause.UNAVAILABLE),
    ):
        assert pdf_fetch_log.classify_failure(
            item_type="journalArticle", http_status=status,
        ) == expected, status


# ---------------------------------------------------------------------------
# UNAVAILABLE must be earned.
#
# The API cascade is the *first* of several routes and cannot reach a
# Cloudflare-gated publisher, a link-resolver platform, or anything the
# Zotero Connector saves. Reading its silence as "no full text exists"
# is reading absence of evidence as evidence of absence — and UNAVAILABLE
# is the one cause that licenses an exclusion.
#
# A live run wrote 227 such rows. Every one had an empty `http_status`,
# so not one source had ever answered "not found", and every one landed
# in the audit's `true_negative` key file. Three (PD2ZFM9M, JTMVVXI5,
# FEE68VW2 — all 10.1007 DOIs) then opened on the first click through
# EBSCOhost via the link resolver.
# ---------------------------------------------------------------------------


def test_silent_cascade_is_not_unavailable_while_the_browser_pass_is_untried() -> None:
    """The 227-row case: nobody answered, so nobody said "not found"."""
    assert (
        pdf_fetch_log.classify_failure(
            item_type="journalArticle", http_status=None,
            browser_pass_untried=True,
        )
        == pdf_fetch_log.FailureCause.BROWSER_REQUIRED
    )


def test_no_handler_match_does_not_authorise_unavailable() -> None:
    """The exact shape of the three proof items.

    No browser handler covers the DOI, so `untried_browser_handler` is
    empty — but the link resolver and the Connector are keyed on the
    *item*, not the DOI prefix, and both were still ahead of it. All
    three were reachable through EBSCOhost.
    """
    for doi_prefix in ("10.1109", "10.2307", "10.1145", "10.1007"):
        assert (
            pdf_fetch_log.classify_failure(
                item_type="journalArticle", http_status=None,
                untried_browser_handler="",     # nothing matched the prefix
                browser_pass_untried=True,
            )
            != pdf_fetch_log.FailureCause.UNAVAILABLE
        ), doi_prefix


def test_a_404_alone_does_not_authorise_unavailable() -> None:
    """One provider's 404 is that provider's answer, not the world's."""
    for status in (404, 410):
        assert (
            pdf_fetch_log.classify_failure(
                item_type="journalArticle", http_status=status,
                browser_pass_untried=True,
            )
            == pdf_fetch_log.FailureCause.BROWSER_REQUIRED
        ), status


def test_untried_routes_do_not_relabel_a_real_paywall() -> None:
    """The guard is confined to the would-be-UNAVAILABLE branch.

    A 403 is a real answer from a real server, and ACCESS_BLOCKED
    already says "not an exclusion, flag for ILL". Widening the guard
    to swallow it would lose evidence for no gain.
    """
    for status in (401, 402, 403):
        assert (
            pdf_fetch_log.classify_failure(
                item_type="journalArticle", http_status=status,
                browser_pass_untried=True,
            )
            == pdf_fetch_log.FailureCause.ACCESS_BLOCKED
        ), status


def test_untried_routes_do_not_relabel_a_server_error() -> None:
    for status in (500, 503):
        assert (
            pdf_fetch_log.classify_failure(
                item_type="journalArticle", http_status=status,
                browser_pass_untried=True,
            )
            == pdf_fetch_log.FailureCause.NETWORK_ERROR
        ), status


def test_item_type_still_outranks_untried_routes() -> None:
    """A book chapter nobody has looked for yet is still a book chapter."""
    assert (
        pdf_fetch_log.classify_failure(
            item_type="bookSection", http_status=None,
            browser_pass_untried=True,
        )
        == pdf_fetch_log.FailureCause.OUT_OF_SCOPE
    )


def test_unavailable_survives_once_the_routes_are_spent() -> None:
    """Not a blanket amnesty. Once the browser pass has run, a silent
    miss and a 404 are both true negatives again — otherwise no item
    could ever be excluded on retrieval grounds."""
    for status in (None, 404, 410):
        assert (
            pdf_fetch_log.classify_failure(
                item_type="journalArticle", http_status=status,
                browser_pass_untried=False,
            )
            == pdf_fetch_log.FailureCause.UNAVAILABLE
        ), status


def test_browser_pass_untried_defaults_to_false() -> None:
    """Conservative default: a caller that says nothing is not claiming
    routes remain. Only an orchestrator knows which pass it is in."""
    assert (
        pdf_fetch_log.classify_failure(item_type="journalArticle")
        == pdf_fetch_log.FailureCause.UNAVAILABLE
    )


def test_browser_required_is_not_an_fe_code() -> None:
    """Handing an FE code to a recoverable item is the original defect."""
    suggestion = pdf_fetch_log.SUGGESTED_FE_CODE[
        pdf_fetch_log.FailureCause.BROWSER_REQUIRED.value
    ]
    assert "FE" not in suggestion.replace("Not an exclusion", "")
    assert "--sources browser" in suggestion


def test_recoverable_causes_cover_browser_required() -> None:
    """The skill and the audit both gate exclusions on this set."""
    assert (
        pdf_fetch_log.FailureCause.BROWSER_REQUIRED.value
        in pdf_fetch_log.RECOVERABLE_CAUSES
    )
    assert (
        pdf_fetch_log.FailureCause.UNAVAILABLE.value
        not in pdf_fetch_log.RECOVERABLE_CAUSES
    )


def test_every_cause_has_a_suggested_action() -> None:
    for cause in pdf_fetch_log.FailureCause:
        assert cause.value in pdf_fetch_log.SUGGESTED_FE_CODE, cause


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


def test_log_failure_records_the_untried_handler_separately(tmp_path: Path) -> None:
    """`source` is what has been tried; `untried_handler` is what has
    not. Reading one as the other is what made the audit offer
    "--sources browser --publisher core"."""
    log_path = tmp_path / "log.csv"
    pdf_fetch_log.log_failure(
        log_path,
        item_key="ITEM0001", item_type="journalArticle",
        source="api_cascade", untried_browser_handler="sage",
    )
    row = pdf_fetch_log.read_failures(log_path)[0]
    assert row["source"] == "api_cascade"
    assert row["untried_handler"] == "sage"
    assert row["cause"] == pdf_fetch_log.FailureCause.BROWSER_REQUIRED.value


def test_log_failure_leaves_untried_handler_empty_when_none_applies(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "log.csv"
    pdf_fetch_log.log_failure(
        log_path, item_key="ITEM0002", item_type="journalArticle",
        source="crossref", http_status=404,
    )
    assert pdf_fetch_log.read_failures(log_path)[0]["untried_handler"] == ""


def test_log_failure_reads_a_log_written_before_the_handler_column(
    tmp_path: Path,
) -> None:
    """Adding a column must not orphan an existing log. `csv_io` accepts
    a header that is a prefix-ordered subset of the schema and
    empty-fills the rest."""
    log_path = tmp_path / "log.csv"
    legacy = [f for f in pdf_fetch_log.FAILURE_FIELDS if f != "untried_handler"]
    with log_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=legacy)
        writer.writeheader()
        writer.writerow({
            "item_key": "OLD001", "source": "crossref",
            "cause": pdf_fetch_log.FailureCause.UNAVAILABLE.value,
        })
    pdf_fetch_log.log_failure(
        log_path, item_key="NEW001", item_type="journalArticle",
        source="api_cascade", untried_browser_handler="sage",
    )
    rows = {r["item_key"]: r for r in pdf_fetch_log.read_failures(log_path)}
    assert rows["OLD001"]["untried_handler"] == ""
    assert rows["NEW001"]["untried_handler"] == "sage"


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


def test_log_failure_upserts_per_item_and_source(tmp_path: Path) -> None:
    """Re-running the *same* source on the same item replaces its row."""
    log_path = tmp_path / "log.csv"
    for status in (500, 404):
        pdf_fetch_log.log_failure(
            log_path,
            item_key="ITEM0003", source="crossref",
            item_type="journalArticle", http_status=status,
        )
    rows = pdf_fetch_log.read_failures(log_path)
    assert len(rows) == 1
    assert rows[0]["http_status"] == "404"
    assert rows[0]["cause"] == pdf_fetch_log.FailureCause.UNAVAILABLE.value


def test_log_failure_keeps_one_row_per_source(tmp_path: Path) -> None:
    """A different source adds a row rather than overwriting.

    This is the whole point of the composite key. Keyed on `item_key`
    alone, the browser attempt overwrote the Crossref attempt, so
    "Crossref 404'd and we have never run the browser handler" was
    indistinguishable from "we tried everything and nothing worked" —
    and triage read the survivor as a true negative.
    """
    log_path = tmp_path / "log.csv"
    pdf_fetch_log.log_failure(
        log_path,
        item_key="ITEM0003", source="crossref",
        item_type="journalArticle", http_status=404,
    )
    pdf_fetch_log.log_failure(
        log_path,
        item_key="ITEM0003", source="sage",
        item_type="journalArticle", untried_browser_handler="sage",
    )
    rows = pdf_fetch_log.read_failures(log_path)
    assert len(rows) == 2
    by_source = {r["source"]: r["cause"] for r in rows}
    assert by_source == {
        "crossref": pdf_fetch_log.FailureCause.UNAVAILABLE.value,
        "sage": pdf_fetch_log.FailureCause.BROWSER_REQUIRED.value,
    }


def test_latest_per_item_prefers_the_actionable_cause() -> None:
    """One verdict per item, and the verdict is the recoverable one.

    An item with four API misses and one BROWSER_REQUIRED row is
    recoverable. Taking the last row written, or the most common one,
    would bury that.
    """
    rows = [
        {"item_key": "A", "source": "crossref",
         "cause": pdf_fetch_log.FailureCause.UNAVAILABLE.value},
        {"item_key": "A", "source": "sage",
         "cause": pdf_fetch_log.FailureCause.BROWSER_REQUIRED.value},
        {"item_key": "A", "source": "openalex",
         "cause": pdf_fetch_log.FailureCause.UNAVAILABLE.value},
        {"item_key": "B", "source": "crossref",
         "cause": pdf_fetch_log.FailureCause.UNAVAILABLE.value},
    ]
    best = pdf_fetch_log.latest_per_item(rows)
    assert best["A"]["cause"] == pdf_fetch_log.FailureCause.BROWSER_REQUIRED.value
    assert best["B"]["cause"] == pdf_fetch_log.FailureCause.UNAVAILABLE.value


def test_every_cause_has_a_precedence() -> None:
    """A cause missing from the table scores worse than UNAVAILABLE via
    the lookup default, which is the one place it must never sit."""
    assert set(pdf_fetch_log.CAUSE_PRECEDENCE) == {
        c.value for c in pdf_fetch_log.FailureCause
    }


def test_every_recoverable_cause_outranks_unavailable() -> None:
    """The invariant behind `latest_per_item`: one recoverable row makes
    the item recoverable, no matter how many API misses sit beside it."""
    rank = {c: i for i, c in enumerate(pdf_fetch_log.CAUSE_PRECEDENCE)}
    unavailable = rank[pdf_fetch_log.FailureCause.UNAVAILABLE.value]
    for cause in pdf_fetch_log.RECOVERABLE_CAUSES:
        assert rank[cause] < unavailable, cause


def test_latest_per_item_does_not_bury_a_pdf_already_in_the_cache() -> None:
    """UPLOAD_FAILED and CORRUPT_DOWNLOAD were absent from the precedence
    table, so they scored below UNAVAILABLE. An item whose PDF had been
    downloaded and was sitting in the local cache collapsed to "FE6 (no
    fulltext available)" as soon as one API source logged a miss.
    """
    for recoverable in (
        pdf_fetch_log.FailureCause.UPLOAD_FAILED,
        pdf_fetch_log.FailureCause.CORRUPT_DOWNLOAD,
    ):
        rows = [
            {"item_key": "A", "source": "crossref",
             "cause": pdf_fetch_log.FailureCause.UNAVAILABLE.value},
            {"item_key": "A", "source": "openalex", "cause": recoverable.value},
        ]
        assert (
            pdf_fetch_log.latest_per_item(rows)["A"]["cause"] == recoverable.value
        ), recoverable


def test_latest_per_item_lets_item_type_win() -> None:
    """A book chapter behind Cloudflare is still a book chapter."""
    rows = [
        {"item_key": "A", "source": "sage",
         "cause": pdf_fetch_log.FailureCause.BROWSER_REQUIRED.value},
        {"item_key": "A", "source": "crossref",
         "cause": pdf_fetch_log.FailureCause.OUT_OF_SCOPE.value},
    ]
    best = pdf_fetch_log.latest_per_item(rows)
    assert best["A"]["cause"] == pdf_fetch_log.FailureCause.OUT_OF_SCOPE.value


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
