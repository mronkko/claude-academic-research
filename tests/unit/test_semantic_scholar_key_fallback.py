"""Regression tests: a rejected SEMANTIC_SCHOLAR_API_KEY must not crash
either Semantic Scholar call site — it should warn once and fall back to
unauthenticated requests instead.

Found live: a revoked/invalid key returns 403 Forbidden on every Semantic
Scholar Graph API endpoint while anonymous calls to the same endpoints
succeed (lower rate limit, but they work) — so a 403-with-key is a
reliable signal to drop the key and retry, not a fatal error.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fetchers.semantic_scholar import SemanticScholarSource
from searchers.base import SearchContext
from searchers.semantic_scholar import SemanticScholarSearch


@pytest.fixture(autouse=True)
def _no_s2_env(monkeypatch):
    """Prevent the test runner's real SEMANTIC_SCHOLAR_API_KEY from
    leaking into tests — both call sites fall through to `os.environ`
    when no key is otherwise configured, which would pick up the
    developer's live (possibly dead) key and change test behaviour."""
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# searchers/semantic_scholar.py — formal-search bulk endpoint
# ---------------------------------------------------------------------------


class _Cfg:
    BLOCK_A_TERMS = ("entrepreneur",)
    BLOCK_B_TERMS = ("growth",)


def _resp(status_code: int, payload: dict | None = None) -> MagicMock:
    m = MagicMock()
    m.status_code = status_code
    m.json.return_value = payload or {}
    if status_code < 400:
        m.raise_for_status.return_value = None
    else:
        import requests
        m.raise_for_status.side_effect = requests.HTTPError(f"{status_code}")
    return m


def test_fetch_all_falls_back_to_unauthenticated_on_403(monkeypatch, capsys) -> None:
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(dict(headers or {}))
        if len(calls) == 1:
            return _resp(403)
        return _resp(200, {"data": [{"title": "Paper"}], "token": None})

    monkeypatch.setattr("searchers.semantic_scholar.requests.get", fake_get)

    src = SemanticScholarSearch()
    ctx = SearchContext(from_year=2019, to_year=2019, issns=[])
    papers = src._fetch_all("entrepreneur", ctx, "dead-key")

    assert [p["title"] for p in papers] == ["Paper"]
    assert len(calls) == 2
    assert calls[0].get("x-api-key") == "dead-key"
    assert "x-api-key" not in calls[1]
    assert src._key_rejected is True
    assert "rejected" in capsys.readouterr().out.lower()


def test_second_block_query_does_not_resend_a_rejected_key(monkeypatch) -> None:
    """run() calls _fetch_all once per block (block_a, block_b). Once the
    first block proves the key is dead, the second block must not resend
    it (no second round-trip wasted, no duplicate warning)."""
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(dict(headers or {}))
        if len(calls) == 1:
            return _resp(403)
        return _resp(200, {"data": [], "token": None})

    monkeypatch.setattr("searchers.semantic_scholar.requests.get", fake_get)
    monkeypatch.setattr(
        "searchers.semantic_scholar.resolve_credential",
        lambda *a, **k: ("dead-key", None),
    )

    src = SemanticScholarSearch()
    ctx = SearchContext(from_year=2019, to_year=2019, issns=[])
    src.run(_Cfg(), ctx)

    key_bearing_calls = [c for c in calls if "x-api-key" in c]
    assert len(key_bearing_calls) == 1, (
        "the dead key must only ever be sent once across both block queries"
    )


def test_fetch_all_still_raises_when_403_has_no_key(monkeypatch) -> None:
    """A 403 with no key attached is a real failure (e.g. IP block) —
    must not be silently swallowed."""
    def fake_get(url, params=None, headers=None, timeout=None):
        return _resp(403)

    monkeypatch.setattr("searchers.semantic_scholar.requests.get", fake_get)

    src = SemanticScholarSearch()
    ctx = SearchContext(from_year=2019, to_year=2019, issns=[])
    try:
        src._fetch_all("entrepreneur", ctx, "")
        raised = False
    except Exception:
        raised = True
    assert raised


# ---------------------------------------------------------------------------
# fetchers/semantic_scholar.py — abstract-enrichment fallback
# ---------------------------------------------------------------------------


class _FetcherConfig:
    semantic_scholar_api_key = "dead-key"


def _http_returning(*status_and_payload: tuple[int, dict]) -> MagicMock:
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(dict(headers or {}))
        idx = min(len(calls) - 1, len(status_and_payload) - 1)
        status, payload = status_and_payload[idx]
        return _resp(status, payload)

    http = MagicMock()
    http.get.side_effect = fake_get
    http.calls = calls
    return http


def test_fetch_abstract_falls_back_to_unauthenticated_on_403(capsys) -> None:
    http = _http_returning((403, {}), (200, {"abstract": "The abstract."}))
    src = SemanticScholarSource(http=http, config=_FetcherConfig())

    result = src.fetch_abstract("10.1/x")

    assert result == "The abstract."
    assert len(http.calls) == 2
    assert http.calls[0].get("x-api-key") == "dead-key"
    assert "x-api-key" not in http.calls[1]
    assert src._key_rejected is True
    assert "rejected" in capsys.readouterr().out.lower()


def test_second_lookup_on_same_instance_does_not_resend_rejected_key() -> None:
    """SemanticScholarSource instances are reused across every item in an
    enrichment run — once the key is known dead, subsequent DOI lookups
    must go straight to unauthenticated (one call, not a 403-then-retry
    round trip each time)."""
    http = _http_returning((403, {}), (200, {"abstract": "First."}),
                            (200, {"abstract": "Second."}))
    src = SemanticScholarSource(http=http, config=_FetcherConfig())

    first = src.fetch_abstract("10.1/x")
    second = src.fetch_abstract("10.1/y")

    assert first == "First."
    assert second == "Second."
    assert len(http.calls) == 3  # 403 + retry for the first lookup, then 1 for the second
    assert "x-api-key" not in http.calls[2]


def test_fetch_abstract_returns_none_when_403_has_no_key() -> None:
    http = _http_returning((403, {}))
    src = SemanticScholarSource(http=http, config=None)  # no key configured

    assert src.fetch_abstract("10.1/x") is None
    assert len(http.calls) == 1
