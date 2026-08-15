"""Tests for `ZoteroClient.attach_pdf`'s retry behaviour.

Motivated by a live run that downloaded 68 Sage PDFs behind a solved
Cloudflare challenge, attached 20, and lost 48 to unretried upload
failures. The attachment upload is three network round-trips (register →
auth → PUT bytes), so a transport blip anywhere in the chain used to be
terminal for an item whose PDF was already sitting on disk.

The distinction that matters: retry the transient failures, and do NOT
retry a `failure` payload — that is the API accepting the request and
explicitly rejecting the file, which reproduces identically on retry.

These mock `pyzotero._upload.Zupload`, not `cloud.attachment_simple`.
`attach_pdf` stopped calling the latter on 2026-08-15: that helper sets
the attachment's `filename` to the whole path it is handed, and Zotero
rejects a stored-file filename containing a directory separator, so every
upload from a cache directory failed at item creation. Mocking the old
helper would now assert on a call that never happens, which is worse than
no test - it would pass while the upload path went entirely unexercised.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest
import zotero_io


def _client() -> zotero_io.ZoteroClient:
    zc = zotero_io.ZoteroClient(api_key="fake-key", group_id="12345")
    cloud = MagicMock()
    # A real dict: attach_pdf assigns title/filename into it, and Zupload
    # reads `templt["filename"]` off it. A MagicMock here silently yields
    # a MagicMock filename and the failure surfaces as a confusing
    # FileDoesNotExistError from deep inside pyzotero.
    cloud._attachment_template.return_value = {
        "itemType": "attachment", "linkMode": "imported_file",
        "title": "", "filename": "", "md5": None, "mtime": None,
    }
    zc._cloud = cloud
    return zc


def _patch_upload(monkeypatch, *, side_effect=None, return_value=None):
    """Patch the Zupload seam attach_pdf actually drives.

    Returns the mock standing in for `.upload()`, so tests assert on the
    number of upload attempts.
    """
    upload = MagicMock(side_effect=side_effect, return_value=return_value)
    factory = MagicMock(return_value=MagicMock(upload=upload))
    monkeypatch.setattr("pyzotero._upload.Zupload", factory)
    return upload


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.zotero.org/items")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


def _ok() -> dict:
    return {"success": [{"key": "NEWATT1"}], "failure": [], "unchanged": []}


@pytest.fixture
def pdf(tmp_path):
    """A real file on disk.

    `attach_pdf` refuses to upload a missing or empty file — Zotero
    accepts a zero-byte upload and the resulting attachment still
    carries an md5, which `pdf_map()` reads as a real PDF. So these
    tests need genuine bytes, not a path that never existed.
    """
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"%PDF-1.4\n" + b"0" * 2000 + b"\n%%EOF\n")
    return path


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_attach_pdf_retries_transient_http_errors(status, pdf, monkeypatch) -> None:
    """A 429/5xx on the first attempt must not lose the PDF."""
    zc = _client()
    upload = _patch_upload(monkeypatch, side_effect=[_http_error(status), _ok()])

    assert zc.attach_pdf("PARENT1", pdf) == "NEWATT1"
    assert upload.call_count == 2


def test_attach_pdf_retries_transport_errors(pdf, monkeypatch) -> None:
    """Connection resets mid-upload are the classic 'lost 48 PDFs' case."""
    zc = _client()
    upload = _patch_upload(
        monkeypatch, side_effect=[httpx.ConnectError("connection reset"), _ok()],
    )

    assert zc.attach_pdf("PARENT1", pdf) == "NEWATT1"
    assert upload.call_count == 2


def test_attach_pdf_gives_up_after_three_attempts(pdf, monkeypatch) -> None:
    zc = _client()
    upload = _patch_upload(monkeypatch, side_effect=_http_error(503))

    with pytest.raises(httpx.HTTPStatusError):
        zc.attach_pdf("PARENT1", pdf)
    assert upload.call_count == 3


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413])
def test_attach_pdf_does_not_retry_client_errors(status, pdf, monkeypatch) -> None:
    """A 403 or 404 will fail identically on retry — retrying just
    triples the time to the same answer."""
    zc = _client()
    upload = _patch_upload(monkeypatch, side_effect=_http_error(status))

    with pytest.raises(httpx.HTTPStatusError):
        zc.attach_pdf("PARENT1", pdf)
    assert upload.call_count == 1


def test_attach_pdf_refuses_an_empty_file(tmp_path, monkeypatch) -> None:
    """Found by a live run against a real library.

    Zotero accepts a zero-byte upload without complaint, and the
    resulting attachment still carries an md5 — of nothing. `pdf_map()`
    reads any non-empty md5 as "this item has a real PDF", so the item
    is marked complete and skipped by every future run while holding an
    attachment with no content. Refuse before it reaches the API.
    """
    zc = _client()
    upload = _patch_upload(monkeypatch, return_value=_ok())

    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")

    with pytest.raises(RuntimeError, match="empty file"):
        zc.attach_pdf("PARENT1", empty)
    upload.assert_not_called()


def test_attach_pdf_refuses_a_missing_file(tmp_path, monkeypatch) -> None:
    zc = _client()
    upload = _patch_upload(monkeypatch, return_value=_ok())

    with pytest.raises(RuntimeError, match="cannot read"):
        zc.attach_pdf("PARENT1", tmp_path / "nope.pdf")
    upload.assert_not_called()


def test_attach_pdf_does_not_retry_reported_failure(pdf, monkeypatch) -> None:
    """The API accepted the request and put the file in `failure` —
    a deterministic rejection, not a transient fault.

    This is the bucket the path-as-filename bug landed in: the server
    rejected the item before any bytes moved, and because pyzotero's
    `_create_prelim` discards the server's `failed` map, the only symptom
    was a failure entry echoing the payload. Retrying reproduces it
    exactly, so one attempt is correct — but the reason must reach the
    caller, which is why attach_pdf raises rather than returning None.
    """
    zc = _client()
    upload = _patch_upload(monkeypatch, return_value={
        "success": [], "failure": [{"title": "Bad File"}], "unchanged": [],
    })

    with pytest.raises(RuntimeError):
        zc.attach_pdf("PARENT1", pdf)
    assert upload.call_count == 1
