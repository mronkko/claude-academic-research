"""The per-publisher retrieval report.

Reconstructs the real run this whole change came from: 244 candidates,
125 attached, 119 missing, of which 76 were Sage + Academy of Management
items that one browser pass would recover. The old report said
"119 UNAVAILABLE — FE6 (no fulltext available)" and the user rebuilt the
breakdown by hand.
"""

from __future__ import annotations

import csv
from pathlib import Path

import audit_zotero_library as audit
import pdf_fetch_log
import pytest

# The hand-built table from that run.
REAL_RUN = [
    ("Sage", "sage", pdf_fetch_log.FailureCause.BROWSER_REQUIRED, 48),
    ("Academy of Management", "aom",
     pdf_fetch_log.FailureCause.BROWSER_REQUIRED, 28),
    ("Springer", "springer", pdf_fetch_log.FailureCause.UNAVAILABLE, 15),
    ("APA PsycNET", "apa", pdf_fetch_log.FailureCause.BROWSER_REQUIRED, 10),
    ("Wiley", "wiley", pdf_fetch_log.FailureCause.ACCESS_BLOCKED, 8),
    ("Palgrave", "palgrave", pdf_fetch_log.FailureCause.UNAVAILABLE, 1),
]


@pytest.fixture
def run(tmp_path: Path):
    """Write a failure log + audit report matching the real run."""
    log = tmp_path / "pdf_fetch_log.csv"
    rows: list[dict] = []
    keys: list[str] = []
    n = 0
    for publisher, source, cause, count in REAL_RUN:
        for _ in range(count):
            n += 1
            key = f"ITEM{n:04d}"
            keys.append(key)
            rows.append({
                "timestamp": "2026-08-13T00:00:00+00:00",
                "item_key": key, "doi": f"10.1/{key}",
                "item_type": "journalArticle", "attempt": "1",
                "source": source, "publisher": publisher,
                "http_status": "", "cause": cause.value,
            })
    with log.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=pdf_fetch_log.FAILURE_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    report = {
        "total_items": 244,
        "have_pdf": 125,
        "missing_pdf": [{"key": k, "title": k, "doi": ""} for k in keys],
    }
    return log, report, tmp_path / "audit"


def _run_report(run, capsys):
    log, report, stem = run
    audit._report_retrieval_failures(str(log), report, stem, "audit.json")
    return capsys.readouterr().out


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------


def test_headline_counts_match_the_run(run, capsys) -> None:
    out = _run_report(run, capsys)
    assert "125/244 attached" in out
    assert "110 still missing" in out  # 48+28+15+10+8+1


def test_every_publisher_appears_with_its_count(run, capsys) -> None:
    out = _run_report(run, capsys)
    for publisher, _source, _cause, count in REAL_RUN:
        line = next(
            (ln for ln in out.splitlines() if ln.strip().startswith(publisher)),
            None,
        )
        assert line is not None, f"{publisher} missing from report:\n{out}"
        assert str(count) in line, line


def test_browser_publishers_are_listed_before_true_negatives(run, capsys) -> None:
    """Most actionable first: the user should see the 76 before the 16."""
    out = _run_report(run, capsys)
    lines = [ln for ln in out.splitlines() if ln.strip().startswith(("Sage", "Springer"))]
    assert lines[0].strip().startswith("Sage")


def test_sage_and_aom_get_a_browser_command_not_an_fe_code(run, capsys) -> None:
    """The exact misdiagnosis: these were being told FE6."""
    out = _run_report(run, capsys)
    for publisher, handler in (("Sage", "sage"), ("Academy of Management", "aom")):
        line = next(ln for ln in out.splitlines() if ln.strip().startswith(publisher))
        assert f"--sources browser --publisher {handler}" in line
        assert "FE6" not in line


def test_springer_still_gets_fe6(run, capsys) -> None:
    """True negatives must keep saying so — this is not a blanket amnesty."""
    out = _run_report(run, capsys)
    line = next(ln for ln in out.splitlines() if ln.strip().startswith("Springer"))
    assert "FE6" in line


def test_wiley_is_flagged_for_ill(run, capsys) -> None:
    out = _run_report(run, capsys)
    line = next(ln for ln in out.splitlines() if ln.strip().startswith("Wiley"))
    assert "ILL" in line


def test_recoverable_total_is_stated(run, capsys) -> None:
    """86 = 48 Sage + 28 AoM + 10 APA."""
    out = _run_report(run, capsys)
    assert "86 of the 110 are recoverable" in out
    assert "Do NOT mark these full-text-unavailable" in out


def test_one_browser_pass_command_is_offered(run, capsys) -> None:
    out = _run_report(run, capsys)
    assert "covers 3 publishers" in out
    assert "--sources browser --filter-keys-file" in out


# ---------------------------------------------------------------------------
# Retry key files
# ---------------------------------------------------------------------------


def test_retry_key_files_are_written_per_bucket(run, capsys) -> None:
    _out = _run_report(run, capsys)
    _log, _report, stem = run
    browser = Path(f"{stem}.retry.browser.keys")
    assert browser.exists()
    assert len(browser.read_text().split()) == 86
    assert len(Path(f"{stem}.true_negative.keys").read_text().split()) == 16
    assert len(Path(f"{stem}.retry.ill.keys").read_text().split()) == 8


def test_per_publisher_key_files_let_you_run_one_publisher(run, capsys) -> None:
    _out = _run_report(run, capsys)
    _log, _report, stem = run
    assert len(Path(f"{stem}.retry.browser.sage.keys").read_text().split()) == 48
    assert len(Path(f"{stem}.retry.browser.aom.keys").read_text().split()) == 28


def test_empty_buckets_write_no_file(run, capsys) -> None:
    _out = _run_report(run, capsys)
    _log, _report, stem = run
    assert not Path(f"{stem}.retry.network.keys").exists()


# ---------------------------------------------------------------------------
# The joins
# ---------------------------------------------------------------------------


def test_items_that_were_since_attached_are_excluded(tmp_path, capsys) -> None:
    """A stale failure row must never argue for excluding a paper that is
    now sitting in the library."""
    log = tmp_path / "log.csv"
    with log.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=pdf_fetch_log.FAILURE_FIELDS)
        w.writeheader()
        for key in ("GONE", "STILL"):
            w.writerow({
                "item_key": key, "source": "crossref", "publisher": "Springer",
                "cause": pdf_fetch_log.FailureCause.UNAVAILABLE.value,
            })
    report = {
        "total_items": 2, "have_pdf": 1,
        "missing_pdf": [{"key": "STILL"}],
    }
    audit._report_retrieval_failures(str(log), report, tmp_path / "a", "a.json")
    out = capsys.readouterr().out
    assert "1 still missing" in out
    assert len(Path(f"{tmp_path / 'a'}.true_negative.keys").read_text().split()) == 1


def test_an_item_with_any_recoverable_row_is_recoverable(tmp_path, capsys) -> None:
    """Four API misses and one BROWSER_REQUIRED means recoverable.

    The composite key keeps all five rows; taking the last-written one
    would bury the finding.
    """
    log = tmp_path / "log.csv"
    with log.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=pdf_fetch_log.FAILURE_FIELDS)
        w.writeheader()
        for source in ("crossref", "openalex", "unpaywall", "pmc"):
            w.writerow({
                "item_key": "A", "source": source, "publisher": "Sage",
                "cause": pdf_fetch_log.FailureCause.UNAVAILABLE.value,
            })
        w.writerow({
            "item_key": "A", "source": "sage", "publisher": "Sage",
            "cause": pdf_fetch_log.FailureCause.BROWSER_REQUIRED.value,
        })
    report = {"total_items": 1, "have_pdf": 0, "missing_pdf": [{"key": "A"}]}
    audit._report_retrieval_failures(str(log), report, tmp_path / "a", "a.json")
    out = capsys.readouterr().out
    assert "1 of the 1 are recoverable" in out
    assert "--sources browser --publisher sage" in out


def test_missing_log_is_silent(tmp_path, capsys) -> None:
    audit._report_retrieval_failures(
        str(tmp_path / "nope.csv"),
        {"total_items": 0, "have_pdf": 0, "missing_pdf": []},
        tmp_path / "a", "a.json",
    )
    assert capsys.readouterr().out == ""


def test_no_failures_still_missing_is_silent(tmp_path, capsys) -> None:
    """Everything in the log has since been attached — nothing to say."""
    log = tmp_path / "log.csv"
    with log.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=pdf_fetch_log.FAILURE_FIELDS)
        w.writeheader()
        w.writerow({
            "item_key": "A", "source": "crossref",
            "cause": pdf_fetch_log.FailureCause.UNAVAILABLE.value,
        })
    audit._report_retrieval_failures(
        str(log), {"total_items": 1, "have_pdf": 1, "missing_pdf": []},
        tmp_path / "a", "a.json",
    )
    assert capsys.readouterr().out == ""
