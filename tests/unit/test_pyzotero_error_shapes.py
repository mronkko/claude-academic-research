"""Reading an HTTP status off whatever pyzotero raises this version.

Through 1.14, pyzotero let httpx's own `HTTPStatusError` out of every
non-2xx call, and `zotero_io` read `.response.status_code` off it to
tell a 412 version conflict (retry after re-fetching) from a real
failure (do not).

1.15 raises its own typed errors instead — `PreConditionFailedError`,
`TooManyRequestsError`, and so on. They are NOT httpx subclasses and
they carry NO response object, so `except httpx.HTTPStatusError` stops
matching and `.response.status_code` stops existing. Every 412 retry in
this module silently became dead code the moment the floor moved to
1.15.1, and a version conflict — the ordinary outcome of two writers on
one library — would surface as an uncaught exception instead of a
retry.

This was caught by the live local-write round trip rather than by any
mock: a stale-version PATCH really does 412, and the assertion that it
raised an httpx error was the thing that failed.

Statuses pyzotero maps to a class (`errors.ERROR_CODES`) are recovered
from the exception's type. Everything else — 5xx especially, which has
no class and matters for the upload retry — is recovered from the
message, which `_err_msg` always formats with a `Code: NNN` line.
"""

from __future__ import annotations

import httpx
import pytest
from pyzotero import errors as ze
from zotero_io import _http_status_of, _is_retryable_upload_error


def _httpx_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("PATCH", "https://api.zotero.org/x")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


def _pyzotero_error(cls, status: int) -> Exception:
    """Shaped like pyzotero's own `_err_msg` output."""
    return cls(f"\nCode: {status}\nURL: http://localhost:23119/api/x\n")


# --- the old shape still works ---------------------------------------

def test_an_httpx_status_error_still_reports_its_status():
    assert _http_status_of(_httpx_error(412)) == 412


# --- the new shape -----------------------------------------------------

@pytest.mark.parametrize(
    ("cls", "status"),
    [
        (ze.PreConditionFailedError, 412),
        (ze.TooManyRequestsError, 429),
        (ze.ResourceNotFoundError, 404),
        (ze.UnsupportedParamsError, 400),
        (ze.PreConditionRequiredError, 428),
    ],
)
def test_a_typed_pyzotero_error_reports_its_status(cls, status):
    assert _http_status_of(_pyzotero_error(cls, status)) == status


def test_a_412_is_recognised_from_the_type_even_with_no_code_line():
    """The type alone is enough for the statuses pyzotero classifies."""
    assert _http_status_of(ze.PreConditionFailedError("version mismatch")) == 412


def test_a_server_id_mismatch_counts_as_a_412():
    """`ServerIDMismatchError` subclasses `PreConditionFailedError`."""
    assert _http_status_of(ze.ServerIDMismatchError("mismatch")) == 412


def test_a_5xx_has_no_pyzotero_class_and_is_read_from_the_message():
    """503 maps to the generic `HTTPError`, so only the text carries it."""
    assert _http_status_of(_pyzotero_error(ze.HTTPError, 503)) == 503


def test_an_unrelated_exception_has_no_status():
    assert _http_status_of(ValueError("nope")) is None
    assert _http_status_of(ze.PyZoteroError("no code here")) is None


# --- what the upload retry does with it -------------------------------

@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_upload_failures_retry_in_the_new_shape(status):
    cls = ze.TooManyRequestsError if status == 429 else ze.HTTPError
    assert _is_retryable_upload_error(_pyzotero_error(cls, status)) is True


def test_a_permanent_upload_failure_does_not_retry():
    assert _is_retryable_upload_error(
        _pyzotero_error(ze.ResourceNotFoundError, 404),
    ) is False
    assert _is_retryable_upload_error(RuntimeError("reported in failure bucket")) is False


def test_a_transport_error_still_retries():
    assert _is_retryable_upload_error(httpx.ConnectError("reset")) is True
