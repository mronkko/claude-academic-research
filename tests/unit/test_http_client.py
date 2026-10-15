"""Tests for scripts/pipelines/http_client.py."""

from __future__ import annotations

import http_client
import pytest
import requests


def test_build_session_returns_requests_session() -> None:
    s = http_client.build_session()
    assert isinstance(s, requests.Session)


def test_build_session_adds_mailto_to_ua() -> None:
    s = http_client.build_session(mailto="me@example.com")
    assert "mailto:me@example.com" in s.headers["User-Agent"]


def test_build_session_omits_mailto_when_none() -> None:
    s = http_client.build_session()
    assert "mailto:" not in s.headers["User-Agent"]


def test_build_session_mounts_retry_adapter() -> None:
    s = http_client.build_session()
    https_adapter = s.get_adapter("https://example.com")
    http_adapter = s.get_adapter("http://example.com")
    assert https_adapter is http_adapter
    # urllib3 Retry is on max_retries; assert it's a Retry with our policy.
    retry = https_adapter.max_retries
    assert retry.total == 5
    assert 429 in retry.status_forcelist
    assert 503 in retry.status_forcelist


def test_get_json_returns_parsed_body(monkeypatch) -> None:
    s = http_client.build_session()

    class FakeResponse:
        status_code = 200
        def json(self) -> dict: return {"ok": True, "value": 42}
        def raise_for_status(self) -> None: pass

    monkeypatch.setattr(s, "get", lambda *a, **kw: FakeResponse())
    assert http_client.get_json(s, "https://example.com/x") == {"ok": True, "value": 42}


def test_get_json_returns_none_on_4xx(monkeypatch) -> None:
    s = http_client.build_session()

    class FakeResponse:
        status_code = 404
        def raise_for_status(self) -> None: raise AssertionError("must not raise on 4xx")
        def json(self) -> dict: raise AssertionError("must not parse 4xx body")

    monkeypatch.setattr(s, "get", lambda *a, **kw: FakeResponse())
    assert http_client.get_json(s, "https://example.com/missing") is None


def test_get_json_retries_on_connection_error(monkeypatch) -> None:
    s = http_client.build_session()
    calls = {"n": 0}

    class FakeResponse:
        status_code = 200
        def json(self) -> dict: return {"hit": calls["n"]}
        def raise_for_status(self) -> None: pass

    def flaky(*_a, **_kw):
        calls["n"] += 1
        if calls["n"] < 2:
            raise requests.ConnectionError("transient")
        return FakeResponse()

    # Collapse the tenacity waits so the test is fast.
    monkeypatch.setattr(http_client.get_json.retry, "wait",
                        __import__("tenacity").wait_none())
    monkeypatch.setattr(s, "get", flaky)
    result = http_client.get_json(s, "https://example.com/x")
    assert calls["n"] == 2
    assert result == {"hit": 2}


def test_get_bytes_returns_body_and_content_type(monkeypatch) -> None:
    s = http_client.build_session()

    class FakeResponse:
        status_code = 200
        content = b"%PDF-1.7 fake"
        headers = {"Content-Type": "application/pdf"}
        def raise_for_status(self) -> None: pass

    monkeypatch.setattr(s, "get", lambda *a, **kw: FakeResponse())
    result = http_client.get_bytes(s, "https://example.com/x.pdf")
    assert result is not None
    body, ct = result
    assert body == b"%PDF-1.7 fake"
    assert ct == "application/pdf"


# --- non-idempotent writes are retried on 429 only ---------------------


def _retry_policy():
    return http_client.build_session().get_adapter("https://x").max_retries


@pytest.mark.parametrize("method", ["POST", "PATCH"])
def test_post_is_not_retried_on_5xx(method) -> None:
    """A replayed `POST /items` after a 5xx re-created every item of the
    batch: the server may have applied the write before failing."""
    retry = _retry_policy()
    for status in (500, 502, 503, 504):
        assert not retry.is_retry(method, status, has_retry_after=True)


@pytest.mark.parametrize("method", ["POST", "PATCH"])
def test_post_is_still_retried_on_429(method) -> None:
    """A 429 is a refusal — nothing was applied — so backing off and
    replaying is safe, and Zotero asks clients to do exactly that."""
    assert _retry_policy().is_retry(method, 429)


def test_post_is_not_retried_on_a_read_error() -> None:
    from urllib3.exceptions import ReadTimeoutError

    retry = _retry_policy()
    err = ReadTimeoutError(None, "https://x/items", "read timed out")
    with pytest.raises(ReadTimeoutError):
        retry.increment("POST", "https://x/items", error=err)


def test_get_keeps_its_5xx_and_read_error_retries() -> None:
    from urllib3.exceptions import ReadTimeoutError

    retry = _retry_policy()
    assert retry.is_retry("GET", 503)
    err = ReadTimeoutError(None, "https://x/y", "read timed out")
    assert retry.increment("GET", "https://x/y", error=err).total == retry.total - 1
