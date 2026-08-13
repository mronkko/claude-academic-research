"""Tests for the single attach path and the cache-recovery pass.

Covers the two halves of the "48 lost PDFs" incident:

- The attach step swallowed its exception into a bare `upload_failed`
  row, so the only copy of the reason lived in terminal scrollback.
- The successfully-downloaded files stayed in the cache with nothing
  ever going back for them; recovery took a hand-written one-off script.
"""

from __future__ import annotations

import argparse
import csv
import io
from pathlib import Path
from unittest.mock import MagicMock

import enrich_pdfs
import pdf_fetch_log
import pytest
from log_schemas import PDF_FETCH_FIELDS


def _pdf_bytes(marker: bytes = b"body") -> bytes:
    return b"%PDF-1.4\n" + marker + b"\n" + b"0" * 2000 + b"\n%%EOF\n"


def _truncated_bytes() -> bytes:
    return b"%PDF-1.4\n" + b"0" * 2000 + b"\nstartxref\n1744085\n%%EOF\n"


class _Log:
    """Captures rows the way the real DictWriter would."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self._buf = io.StringIO()
        self._writer = csv.DictWriter(self._buf, fieldnames=PDF_FETCH_FIELDS)

    def writerow(self, row: dict) -> None:
        self._writer.writerow(row)      # asserts the row fits the schema
        self.rows.append(row)


def _args(tmp_path: Path, **kw) -> argparse.Namespace:
    base = dict(
        cache_dir=str(tmp_path), failure_log_csv="", no_check_text=True,
        dry_run=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def _attach(zot, log, path, **kw):
    defaults = dict(
        run_date="2026-08-13", item_key="K1", doi="10.1/x", title="T",
        source="sage", pdf_path=path, check_text=False,
    )
    defaults.update(kw)
    return enrich_pdfs._attach_and_log(zot, log, **defaults)


# --- failure detail ---------------------------------------------------

def test_upload_failure_records_the_reason(tmp_path) -> None:
    path = tmp_path / "p.pdf"
    path.write_bytes(_pdf_bytes())
    zot = MagicMock()
    zot.attach_pdf.side_effect = RuntimeError("attach_pdf failed: [{'title': 'x'}]")
    log = _Log()

    assert _attach(zot, log, path) is False
    row = log.rows[-1]
    assert row["status"] == "upload_failed"
    assert "RuntimeError" in row["detail"]
    assert "attach_pdf failed" in row["detail"]


def test_upload_failure_detail_includes_http_status(tmp_path) -> None:
    path = tmp_path / "p.pdf"
    path.write_bytes(_pdf_bytes())

    exc = RuntimeError("server said no")
    exc.response = MagicMock(status_code=503)
    zot = MagicMock()
    zot.attach_pdf.side_effect = exc
    log = _Log()

    assert _attach(zot, log, path) is False
    assert "HTTP 503" in log.rows[-1]["detail"]


def test_upload_failure_reaches_the_structured_failure_log(tmp_path) -> None:
    """`pdf_fetch_log.csv` had zero rows for these items — the browser
    path never wrote to it at all, so the audit could not see them."""
    path = tmp_path / "p.pdf"
    path.write_bytes(_pdf_bytes())
    failure_log = tmp_path / "pdf_fetch_log.csv"
    zot = MagicMock()
    zot.attach_pdf.side_effect = RuntimeError("nope")

    _attach(zot, _Log(), path, failure_log_path=str(failure_log))

    rows = pdf_fetch_log.read_failures(failure_log)
    assert len(rows) == 1
    assert rows[0]["cause"] == pdf_fetch_log.FailureCause.UPLOAD_FAILED.value


def test_upload_failed_cause_is_never_an_exclusion_suggestion() -> None:
    """Mapping it onto UNAVAILABLE would suggest "FE6 no fulltext
    available" for a PDF sitting on disk."""
    suggestion = pdf_fetch_log.SUGGESTED_FE_CODE[
        pdf_fetch_log.FailureCause.UPLOAD_FAILED.value
    ]
    assert "FE" not in suggestion.replace("NOT an exclusion", "")


# --- tag PATCH isolation ----------------------------------------------

def test_tag_failure_after_a_good_upload_still_reports_attached(tmp_path) -> None:
    """Folding the tag PATCH into the attach try-block recorded a fully
    successful attachment as `upload_failed`, permanently."""
    path = tmp_path / "10.1_x-tdm-recovered.pdf"
    path.write_bytes(_pdf_bytes())
    zot = MagicMock()
    zot.update_tags.side_effect = RuntimeError("412 conflict")
    log = _Log()

    assert _attach(zot, log, path) is True
    assert log.rows[-1]["status"] == "attached"


# --- structural rejection ---------------------------------------------

def test_corrupt_pdf_is_not_attached(tmp_path) -> None:
    """Attaching it would make the item look done forever: later runs
    see an attachment via `pdf_map()` and skip it."""
    path = tmp_path / "p.pdf"
    path.write_bytes(_truncated_bytes())
    zot = MagicMock()
    log = _Log()

    assert _attach(zot, log, path) is False
    zot.attach_pdf.assert_not_called()
    assert log.rows[-1]["status"] == "rejected_corrupt_pdf"
    assert "truncated" in log.rows[-1]["detail"]


def test_corrupt_pdf_is_removed_from_the_cache(tmp_path) -> None:
    path = tmp_path / "p.pdf"
    path.write_bytes(_truncated_bytes())
    _attach(MagicMock(), _Log(), path)
    assert not path.exists()


# --- cache recovery ---------------------------------------------------

def _item(key: str, doi: str) -> dict:
    return {"key": key, "data": {"DOI": doi, "title": "T", "itemType": "journalArticle"}}


def test_cache_recovery_attaches_without_any_fetch(tmp_path) -> None:
    doi = "10.1177/01492063231207341"
    (tmp_path / f"{doi.replace('/', '_')}.pdf").write_bytes(_pdf_bytes())
    zot = MagicMock()
    log = _Log()

    done = enrich_pdfs._attach_from_cache(
        [_item("K1", doi)], zot, log, _args(tmp_path), "2026-08-13",
    )

    assert [it["key"] for it in done] == ["K1"]
    zot.attach_pdf.assert_called_once()
    assert log.rows[-1]["source"] == "cache"


def test_cache_recovery_finds_a_mixed_case_doi(tmp_path) -> None:
    """The API path wrote cache filenames without lower-casing while the
    browser path lower-cased, so the same DOI mapped to two filenames
    and a cached PDF could be invisible to the other path."""
    doi = "10.1177/ABC123"
    (tmp_path / "10.1177_abc123.pdf").write_bytes(_pdf_bytes())

    done = enrich_pdfs._attach_from_cache(
        [_item("K1", doi)], MagicMock(), _Log(), _args(tmp_path), "2026-08-13",
    )
    assert len(done) == 1


def test_cache_recovery_ignores_a_truncated_cache_file(tmp_path) -> None:
    """A recovery pass that re-attached broken files would recreate the
    exact bug it exists to clean up."""
    doi = "10.1177/bad"
    (tmp_path / "10.1177_bad.pdf").write_bytes(_truncated_bytes())
    zot = MagicMock()

    done = enrich_pdfs._attach_from_cache(
        [_item("K1", doi)], zot, _Log(), _args(tmp_path), "2026-08-13",
    )
    assert done == []
    zot.attach_pdf.assert_not_called()


def test_cache_recovery_is_a_noop_with_an_empty_cache(tmp_path) -> None:
    zot = MagicMock()
    done = enrich_pdfs._attach_from_cache(
        [_item("K1", "10.1/nothing")], zot, _Log(), _args(tmp_path), "2026-08-13",
    )
    assert done == []
    zot.attach_pdf.assert_not_called()


def test_cache_recovery_reports_failures_without_claiming_success(tmp_path) -> None:
    doi = "10.1177/x"
    (tmp_path / "10.1177_x.pdf").write_bytes(_pdf_bytes())
    zot = MagicMock()
    zot.attach_pdf.side_effect = RuntimeError("still broken")

    done = enrich_pdfs._attach_from_cache(
        [_item("K1", doi)], zot, _Log(), _args(tmp_path), "2026-08-13",
    )
    assert done == []


# --- resume semantics -------------------------------------------------

def test_attached_no_text_does_not_count_as_done() -> None:
    """"No extractable text" must not be recorded as a finished item.

    The tempting reasoning — the file is attached, and re-fetching a
    scan just returns the same scan — rests on a diagnosis the evidence
    refuted. All five textless files in the incident came back intact
    from a different source (3 Wiley TDM, 2 Sage browser; 19-44 real
    pages). None was a scan.
    """
    assert "attached_no_text" not in enrich_pdfs.DONE_STATUSES
    assert "attached" in enrich_pdfs.DONE_STATUSES


@pytest.mark.parametrize(
    "status",
    ["upload_failed", "rejected_corrupt_pdf", "skipped_no_pdf",
     "attached_no_text"],
)
def test_unresolved_statuses_are_retried_next_run(status) -> None:
    assert status not in enrich_pdfs.DONE_STATUSES


def test_textless_detail_does_not_assert_it_is_a_scan(tmp_path) -> None:
    """The run log must not carry a cause the evidence contradicts."""
    path = tmp_path / "p.pdf"
    path.write_bytes(_pdf_bytes())
    log = _Log()

    enrich_pdfs._attach_and_log(
        MagicMock(), log, run_date="2026-08-13", item_key="K1",
        doi="10.1/x", title="T", source="openalex", pdf_path=path,
        check_text=True,
    )
    row = log.rows[-1]
    if row["status"] == "attached_no_text":
        assert "OCR" not in row["detail"]
        assert "scan" not in row["detail"].lower()
