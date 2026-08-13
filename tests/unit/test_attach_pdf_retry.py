"""Tests for `ZoteroClient.attach_pdf`'s retry behaviour.

Motivated by a live run that downloaded 68 Sage PDFs behind a solved
Cloudflare challenge, attached 20, and lost 48 to unretried upload
failures. The attachment upload is three network round-trips (register →
auth → PUT bytes), so a transport blip anywhere in the chain used to be
terminal for an item whose PDF was already sitting on disk.

The distinction that matters: retry the transient failures, and do NOT
retry a `failure` payload — that is the API accepting the request and
explicitly rejecting the file, which reproduces identically on retry.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest
import zotero_io


def _client() -> zotero_io.ZoteroClient:
    return zotero_io.ZoteroClient(api_key="fake-key", group_id="12345")


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.zotero.org/items")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


def _ok() -> dict:
    return {"success": [{"key": "NEWATT1"}], "failure": [], "unchanged": []}


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_attach_pdf_retries_transient_http_errors(status) -> None:
    """A 429/5xx on the first attempt must not lose the PDF."""
    zc = _client()
    fake_cloud = MagicMock()
    fake_cloud.attachment_simple.side_effect = [_http_error(status), _ok()]
    zc._cloud = fake_cloud

    assert zc.attach_pdf("PARENT1", "/tmp/paper.pdf") == "NEWATT1"
    assert fake_cloud.attachment_simple.call_count == 2


def test_attach_pdf_retries_transport_errors() -> None:
    """Connection resets mid-upload are the classic 'lost 48 PDFs' case."""
    zc = _client()
    fake_cloud = MagicMock()
    fake_cloud.attachment_simple.side_effect = [
        httpx.ConnectError("connection reset"), _ok(),
    ]
    zc._cloud = fake_cloud

    assert zc.attach_pdf("PARENT1", "/tmp/paper.pdf") == "NEWATT1"
    assert fake_cloud.attachment_simple.call_count == 2


def test_attach_pdf_gives_up_after_three_attempts() -> None:
    zc = _client()
    fake_cloud = MagicMock()
    fake_cloud.attachment_simple.side_effect = _http_error(503)
    zc._cloud = fake_cloud

    with pytest.raises(httpx.HTTPStatusError):
        zc.attach_pdf("PARENT1", "/tmp/paper.pdf")
    assert fake_cloud.attachment_simple.call_count == 3


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413])
def test_attach_pdf_does_not_retry_client_errors(status) -> None:
    """A 403 or 404 will fail identically on retry — retrying just
    triples the time to the same answer."""
    zc = _client()
    fake_cloud = MagicMock()
    fake_cloud.attachment_simple.side_effect = _http_error(status)
    zc._cloud = fake_cloud

    with pytest.raises(httpx.HTTPStatusError):
        zc.attach_pdf("PARENT1", "/tmp/paper.pdf")
    assert fake_cloud.attachment_simple.call_count == 1


def test_attach_pdf_does_not_retry_reported_failure() -> None:
    """The API accepted the request and put the file in `failure` —
    a deterministic rejection, not a transient fault."""
    zc = _client()
    fake_cloud = MagicMock()
    fake_cloud.attachment_simple.return_value = {
        "success": [], "failure": [{"title": "Bad File"}], "unchanged": [],
    }
    zc._cloud = fake_cloud

    with pytest.raises(RuntimeError):
        zc.attach_pdf("PARENT1", "/tmp/bad.pdf")
    assert fake_cloud.attachment_simple.call_count == 1
