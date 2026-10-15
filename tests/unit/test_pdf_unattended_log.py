"""DEFERRED_NEEDS_USER — what an unattended browser run sets aside for a
person (plan `pdf-retrieval.md` §4, step 6) — and what the failure log,
the audit, the run report and the manual queue make of it."""

from __future__ import annotations

import csv
from pathlib import Path

import audit_zotero_library as audit
import manual_pdfs
import pdf_fetch_log
import pdf_run_report

C = pdf_fetch_log.FailureCause


def test_deferred_is_recoverable_not_attempted_and_never_an_fe_code() -> None:
    v = C.DEFERRED_NEEDS_USER.value
    assert v in pdf_fetch_log.RECOVERABLE_CAUSES
    assert v in pdf_fetch_log.NOT_ATTEMPTED_CAUSES
    assert "NOT an exclusion" in pdf_fetch_log.SUGGESTED_FE_CODE[v]
    assert not pdf_fetch_log.SUGGESTED_FE_CODE[v].startswith("FE")


def test_a_deferral_outranks_the_api_cascades_403_and_browser_required() -> None:
    rows = [
        {"item_key": "K", "source": "api_cascade", "untried_handler": "sage",
         "cause": C.BROWSER_REQUIRED.value},
        {"item_key": "K", "source": "crossref", "cause": C.ACCESS_BLOCKED.value},
        {"item_key": "K", "source": "sage", "cause": C.DEFERRED_NEEDS_USER.value,
         "attempt": "2"},
    ]
    assert pdf_fetch_log.latest_per_item(rows)["K"]["cause"] == C.DEFERRED_NEEDS_USER.value


def test_run_report_explains_the_deferred_status() -> None:
    assert "deferred_needs_user" in pdf_run_report.STATUS_INFO


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


def test_audit_writes_retry_deferred_keys(tmp_path: Path) -> None:
    files = _audit(tmp_path, [("D1", C.DEFERRED_NEEDS_USER.value),
                              ("D2", C.DEFERRED_NEEDS_USER.value)])
    assert files["retry.deferred"] == ["D1", "D2"]
    assert "retry.manual" not in files


def test_deferred_items_open_first_in_the_next_manual_cycle(tmp_path: Path) -> None:
    """The unattended run's residue is the next mode-3 queue — from the
    audit's key file, and straight from the run log before any audit."""
    assert "retry.deferred" in manual_pdfs.PRIORITY_RETRY_SETS
    assert "deferred_needs_user" in manual_pdfs.PRIORITY_LOG_STATUSES
    _audit(tmp_path, [("D1", C.DEFERRED_NEEDS_USER.value)])
    log = tmp_path / "attach.csv"
    log.write_text("item_key,status\nD2,deferred_needs_user\nA1,attached\n")
    assert manual_pdfs.priority_keys(str(tmp_path / "audit"), str(log)) == {"D1", "D2"}
