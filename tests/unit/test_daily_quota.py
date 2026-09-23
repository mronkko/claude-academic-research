"""A spent daily quota stops the source for the run instead of retrying.

Live 2026-09-23: Web of Science Expanded answered 25 of 39 lookups with
429 carrying `x-req-reqperday-remaining: 0` and no Retry-After. Each item
then sat through five transport retries and failed anyway.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import enrich_abstracts
import pytest
from fetchers.base import QuotaExhausted, answered
from http_client import VerboseRetry
from rate_limits import daily_quota_exhausted
from urllib3.exceptions import MaxRetryError

SPENT = {"X-REQ-ReqPerDay-Remaining": "0", "x-req-reqpersec-remaining": "3"}


@pytest.mark.parametrize("headers,want", [
    (SPENT, True),
    ({"x-ratelimit-remaining-day": "0"}, True),
    ({"x-req-reqperday-remaining": "12"}, False),
    ({"x-req-reqpersec-remaining": "0"}, False),   # per-second: backoff helps
    ({"x-req-reqperday-remaining": "n/a"}, False),
    ({}, False),
    (None, False),
])
def test_daily_quota_exhausted(headers, want) -> None:
    assert daily_quota_exhausted(headers) is want


def _resp(status: int, headers: dict) -> MagicMock:
    resp = MagicMock()
    resp.status = resp.status_code = status
    resp.headers = headers
    return resp


def test_the_transport_does_not_retry_a_spent_quota() -> None:
    retry = VerboseRetry(total=5, status_forcelist=(429,), raise_on_status=False)
    with pytest.raises(MaxRetryError):
        retry.increment("GET", "https://wos.example/x", response=_resp(429, SPENT))


def test_the_transport_still_retries_ordinary_throttling() -> None:
    retry = VerboseRetry(total=5, status_forcelist=(429,), raise_on_status=False)
    nxt = retry.increment(
        "GET", "https://wos.example/x",
        response=_resp(429, {"x-req-reqpersec-remaining": "0"}),
    )
    assert nxt.total == 4


def test_answered_names_a_spent_quota() -> None:
    with pytest.raises(QuotaExhausted, match="daily request quota"):
        answered(_resp(429, SPENT), "wos")
    with pytest.raises(RuntimeError) as plain:
        answered(_resp(429, {}), "wos")
    assert not isinstance(plain.value, QuotaExhausted)


def test_the_cascade_stops_asking_an_exhausted_source(monkeypatch, capsys) -> None:
    monkeypatch.setattr(enrich_abstracts, "_QUOTA_EXHAUSTED", {})
    calls: list[str] = []

    class _Wos:
        name = "wos"

        def fetch_abstract(self, doi, **kw):
            calls.append(doi)
            raise QuotaExhausted("wos: daily request quota exhausted")

    items = [{"data": {"DOI": f"10.1/{i}", "title": "T"}} for i in range(3)]
    results = [enrich_abstracts._try_cascade(it, [_Wos()], "/tmp") for it in items]

    assert calls == ["10.1/0"], "asked once, then never again this run"
    for r in results:
        assert r.status() == "lookup_failed"
        assert "daily request quota exhausted" in r.detail()
    assert capsys.readouterr().out.count("Not asking wos") == 1
