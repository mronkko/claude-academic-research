"""Consent causes and the `network` column in the failure log, and what
the audit and the manual queue make of them (plan step 5)."""

from __future__ import annotations

import csv
from pathlib import Path

import audit_zotero_library as audit
import manual_pdfs
import pdf_fetch_log
import pdf_run_report
import pytest

C = pdf_fetch_log.FailureCause


@pytest.fixture(autouse=True)
def _no_run_network():
    pdf_fetch_log.set_run_network("")
    yield
    pdf_fetch_log.set_run_network("")


# ---------------------------------------------------------------------------
# The network column
# ---------------------------------------------------------------------------

def test_network_column_is_appended_last() -> None:
    assert pdf_fetch_log.FAILURE_FIELDS[-1] == "network"


def test_rows_carry_the_runs_network(tmp_path: Path) -> None:
    log = tmp_path / "log.csv"
    pdf_fetch_log.set_run_network("Aalto")
    pdf_fetch_log.log_failure(log, item_key="K1", source="sage", http_status=403)
    pdf_fetch_log.log_failure(log, item_key="K2", source="sage", network="Jyu")
    rows = {r["item_key"]: r for r in pdf_fetch_log.read_failures(log)}
    assert rows["K1"]["network"] == "Aalto"
    assert rows["K2"]["network"] == "Jyu"


def test_no_resolver_means_an_empty_network(tmp_path: Path) -> None:
    log = tmp_path / "log.csv"
    pdf_fetch_log.log_failure(log, item_key="K1", source="crossref", http_status=404)
    assert pdf_fetch_log.read_failures(log)[0]["network"] == ""


def test_a_log_written_before_the_network_column_is_migrated(tmp_path: Path) -> None:
    """Same append-only migration as every other column: the old header is
    an ordered prefix of the schema, so the next write rewrites the file
    with the new column and the old rows read as empty (not recorded)."""
    log = tmp_path / "log.csv"
    legacy = pdf_fetch_log.FAILURE_FIELDS[:-1]
    with log.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=legacy)
        w.writeheader()
        w.writerow({"item_key": "OLD", "source": "crossref",
                    "cause": C.UNAVAILABLE.value})
    pdf_fetch_log.set_run_network("Aalto")
    pdf_fetch_log.log_failure(log, item_key="NEW", source="sage", http_status=403)
    with log.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames == pdf_fetch_log.FAILURE_FIELDS
        rows = {r["item_key"]: r for r in reader}
    assert rows["OLD"]["network"] == ""
    assert rows["NEW"]["network"] == "Aalto"


# ---------------------------------------------------------------------------
# Consent causes
# ---------------------------------------------------------------------------

def test_consent_causes_are_recoverable_and_never_an_fe_code() -> None:
    for cause in (C.CONSENT_NEEDED, C.CONSENT_DENIED):
        assert cause.value in pdf_fetch_log.RECOVERABLE_CAUSES
        assert cause.value in pdf_fetch_log.NOT_ATTEMPTED_CAUSES
        assert "NOT an exclusion" in pdf_fetch_log.SUGGESTED_FE_CODE[cause.value]
        assert not pdf_fetch_log.SUGGESTED_FE_CODE[cause.value].startswith("FE")


def test_a_consent_row_outranks_the_api_cascades_403() -> None:
    rows = [
        {"item_key": "K", "source": "api_cascade", "cause": C.ACCESS_BLOCKED.value},
        {"item_key": "K", "source": "sage", "cause": C.CONSENT_NEEDED.value,
         "attempt": "2"},
    ]
    assert pdf_fetch_log.latest_per_item(rows)["K"]["cause"] == C.CONSENT_NEEDED.value
    rows[1]["cause"] = C.CONSENT_DENIED.value
    assert pdf_fetch_log.latest_per_item(rows)["K"]["cause"] == C.CONSENT_DENIED.value


def test_a_consent_row_answers_the_api_cascades_browser_required() -> None:
    """The cascade said "run the browser handler"; the browser pass did
    and stopped at consent. The consent row is now the actionable one."""
    rows = [
        {"item_key": "K", "source": "api_cascade", "untried_handler": "sage",
         "cause": C.BROWSER_REQUIRED.value},
        {"item_key": "K", "source": "sage", "cause": C.CONSENT_NEEDED.value,
         "attempt": "2"},
    ]
    assert pdf_fetch_log.latest_per_item(rows)["K"]["cause"] == C.CONSENT_NEEDED.value


def test_run_report_explains_the_consent_statuses() -> None:
    for status in ("skipped_consent_needed", "skipped_consent_denied"):
        assert status in pdf_run_report.STATUS_INFO


# ---------------------------------------------------------------------------
# Audit key files and the manual queue
# ---------------------------------------------------------------------------

def _audit(tmp_path: Path, rows: list[tuple[str, str]]) -> dict[str, list[str]]:
    log = tmp_path / "log.csv"
    with log.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=pdf_fetch_log.FAILURE_FIELDS)
        w.writeheader()
        for key, cause in rows:
            w.writerow({"item_key": key, "source": "sage", "publisher": "Sage",
                        "cause": cause, "attempt": "2"})
    report = {"total_items": len(rows), "have_pdf": 0,
              "missing_pdf": [{"key": k} for k, _ in rows]}
    audit._report_retrieval_failures(str(log), report, tmp_path / "audit", "a.json")
    return {p.name[len("audit."):-len(".keys")]: p.read_text().split()
            for p in tmp_path.glob("audit.*.keys")}


def test_audit_writes_the_consent_retry_key_files(tmp_path: Path) -> None:
    files = _audit(tmp_path, [
        ("N1", C.CONSENT_NEEDED.value), ("N2", C.CONSENT_NEEDED.value),
        ("D1", C.CONSENT_DENIED.value),
    ])
    assert files["retry.consent_needed"] == ["N1", "N2"]
    assert files["retry.consent_denied"] == ["D1"]
    assert "retry.manual" not in files


def test_denied_items_open_first_in_the_manual_queue(tmp_path: Path) -> None:
    """A deny falls through to manual download, not to nothing."""
    assert "retry.consent_denied" in manual_pdfs.PRIORITY_RETRY_SETS
    assert "retry.consent_needed" not in manual_pdfs.PRIORITY_RETRY_SETS
    _audit(tmp_path, [("D1", C.CONSENT_DENIED.value)])
    log = tmp_path / "attach.csv"
    log.write_text("item_key,status\nD2,skipped_consent_denied\nN3,skipped_consent_needed\n")
    keys = manual_pdfs.priority_keys(str(tmp_path / "audit"), str(log))
    assert keys == {"D1", "D2"}
