"""Tests for `fetchers._pdf_validate`.

The reference case throughout is the live incident this module exists
for: OpenAlex served five PDFs whose header and image content were
intact but whose final ~135KB — including the page tree — was missing.
Every check the pipeline made at the time passed, the files were
attached as clean successes, and they were first misdiagnosed as scans
needing OCR.

The `test_rejects_the_openalex_incident_signature` case reproduces that
file's exact defect: a trailer declaring `startxref 1744085` in a file
of 1,608,714 bytes.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fetchers import _pdf_validate


def _pdf(body: bytes = b"content", *, size: int = 2000, eof: bool = True,
         startxref: int | None = None) -> bytes:
    out = b"%PDF-1.4\n" + body + b"\n" + b"0" * size
    if startxref is not None:
        out += b"\nstartxref\n" + str(startxref).encode() + b"\n"
    if eof:
        out += b"%%EOF\n"
    return out


# --- valid ------------------------------------------------------------

def test_accepts_a_well_formed_pdf() -> None:
    data = _pdf()
    assert _pdf_validate.pdf_defect(data) is None
    assert _pdf_validate.is_valid_pdf(data) is True


def test_accepts_a_pdf_whose_startxref_is_in_bounds() -> None:
    data = _pdf(startxref=10)
    assert _pdf_validate.pdf_defect(data) is None


def test_accepts_a_pdf_with_no_startxref_at_all() -> None:
    """Conservative by design: reject only what is provably broken.

    A missing `startxref` is unusual but not proof of truncation, and a
    false rejection throws away a PDF the user may have no other route
    to.
    """
    assert _pdf_validate.pdf_defect(_pdf(startxref=None)) is None


def test_accepts_trailing_whitespace_after_eof() -> None:
    assert _pdf_validate.pdf_defect(_pdf() + b"\n\n   \n") is None


# --- the incident -----------------------------------------------------

def test_rejects_the_openalex_incident_signature() -> None:
    """xref offset past EOF — the exact defect in the live failure."""
    data = _pdf(size=3000, startxref=1_744_085)
    defect = _pdf_validate.pdf_defect(data)
    assert defect is not None
    assert "truncated" in defect
    assert "1744085" in defect


def test_rejects_a_body_cut_short_before_its_trailer() -> None:
    """The generic truncation case: bytes simply stop."""
    full = _pdf(size=5000, startxref=100)
    defect = _pdf_validate.pdf_defect(full[: len(full) // 2])
    assert defect is not None
    assert "%%EOF" in defect


def test_rejects_on_content_length_mismatch() -> None:
    """The most direct evidence available: the server said how many
    bytes it was sending and sent fewer."""
    data = _pdf()
    defect = _pdf_validate.pdf_defect(data, expected_length=len(data) + 5000)
    assert defect is not None
    assert "truncated" in defect
    assert str(len(data)) in defect


# --- other rejections -------------------------------------------------

def test_rejects_html_masquerading_as_pdf() -> None:
    defect = _pdf_validate.pdf_defect(b"<html>" + b"x" * 3000 + b"</html>")
    assert defect is not None
    assert "not a PDF" in defect


def test_rejects_empty_body() -> None:
    assert _pdf_validate.pdf_defect(b"") == "empty response"


def test_rejects_a_body_too_small_to_be_an_article() -> None:
    """A bare `%PDF` magic check passed 18-byte stub responses."""
    defect = _pdf_validate.pdf_defect(b"%PDF-1.4 mock body")
    assert defect is not None
    assert "too small" in defect


# --- response wrapper -------------------------------------------------

def _response(content: bytes, *, status: int = 200,
              headers: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.content = content
    resp.headers = headers if headers is not None else {}
    return resp


def test_response_defect_accepts_a_good_download() -> None:
    data = _pdf()
    resp = _response(data, headers={"Content-Length": str(len(data))})
    assert _pdf_validate.response_defect(resp) is None


def test_response_defect_catches_short_read_against_content_length() -> None:
    data = _pdf()
    resp = _response(data, headers={"Content-Length": str(len(data) + 1000)})
    defect = _pdf_validate.response_defect(resp)
    assert defect is not None and "truncated" in defect


@pytest.mark.parametrize("status", [301, 403, 404, 500])
def test_response_defect_reports_non_200(status) -> None:
    assert _pdf_validate.response_defect(
        _response(_pdf(), status=status)
    ) == f"HTTP {status}"


def test_response_defect_ignores_content_length_when_compressed() -> None:
    """A gzipped transfer declares the compressed size, which
    legitimately differs from the decoded body — comparing them would
    reject every good compressed download."""
    data = _pdf()
    resp = _response(data, headers={
        "Content-Length": "512", "Content-Encoding": "gzip",
    })
    assert _pdf_validate.response_defect(resp) is None


def test_response_defect_tolerates_a_non_numeric_content_length() -> None:
    resp = _response(_pdf(), headers={"Content-Length": "not-a-number"})
    assert _pdf_validate.response_defect(resp) is None


# --- file wrapper -----------------------------------------------------

def test_file_defect_accepts_a_good_file(tmp_path) -> None:
    path = tmp_path / "ok.pdf"
    path.write_bytes(_pdf())
    assert _pdf_validate.file_defect(path) is None


def test_file_defect_rejects_a_truncated_cache_entry(tmp_path) -> None:
    """The cache-poisoning half of the bug: a truncated file written by
    an earlier unvalidated run was served forever, because the only
    check was `path.exists()`."""
    path = tmp_path / "bad.pdf"
    path.write_bytes(_pdf(size=3000, startxref=9_999_999))
    assert _pdf_validate.file_defect(path) is not None


def test_file_defect_reports_a_missing_file(tmp_path) -> None:
    defect = _pdf_validate.file_defect(tmp_path / "nope.pdf")
    assert defect is not None and "unreadable" in defect
