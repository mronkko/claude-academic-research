"""Regression tests: a rejected SEMANTIC_SCHOLAR_API_KEY must not crash
either Semantic Scholar call site — it should warn once and fall back to
unauthenticated requests instead.

Found live: a revoked/invalid key returns 403 Forbidden on every Semantic
Scholar Graph API endpoint while anonymous calls to the same endpoints
succeed (lower rate limit, but they work) — so a 403-with-key is a
reliable signal to drop the key and retry, not a fatal error.
"""

from __future__ import annotations

from types import SimpleNamespace
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


def _ctx_recording(responder) -> tuple[SearchContext, list]:
    """A SearchContext whose session is a stub, recording sent headers.

    The searcher reaches HTTP via `ctx.http()`, which returns `session`
    when one is already set. Injecting it here beats monkeypatching a
    module attribute: there is no `requests` symbol in the module to
    patch any more, and a patch that silently misses would let these
    tests fire real requests at Semantic Scholar.
    """
    calls: list[dict] = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(dict(headers or {}))
        return responder(len(calls))

    ctx = SearchContext(
        from_year=2019, to_year=2019, issns=[],
        session=SimpleNamespace(get=fake_get),
    )
    return ctx, calls


def test_fetch_all_falls_back_to_unauthenticated_on_403(capsys) -> None:
    def responder(n: int):
        if n == 1:
            return _resp(403)
        return _resp(200, {"data": [{"title": "Paper"}], "token": None})

    src = SemanticScholarSearch()
    ctx, calls = _ctx_recording(responder)
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
    def responder(n: int):
        if n == 1:
            return _resp(403)
        return _resp(200, {"data": [], "token": None})

    monkeypatch.setattr(
        "searchers.semantic_scholar.resolve_credential",
        lambda *a, **k: ("dead-key", None),
    )

    src = SemanticScholarSearch()
    ctx, calls = _ctx_recording(responder)
    src.run(_Cfg(), ctx)

    key_bearing_calls = [c for c in calls if "x-api-key" in c]
    assert len(key_bearing_calls) == 1, (
        "the dead key must only ever be sent once across both block queries"
    )


def test_fetch_all_still_raises_when_403_has_no_key() -> None:
    """A 403 with no key attached is a real failure (e.g. IP block) —
    must not be silently swallowed."""
    src = SemanticScholarSearch()
    ctx, _calls = _ctx_recording(lambda _n: _resp(403))
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


# ---------------------------------------------------------------------------
# fetchers/semantic_scholar.py — the openAccessPdf PDF path
# ---------------------------------------------------------------------------
#
# The class serves abstracts and PDFs from one key, one session, and one
# rejected-key fallback. These pin the PDF half, including that it obeys
# the same validate-before-serving rule as every other fetcher.


def _pdf_bytes() -> bytes:
    """A minimal structurally-valid PDF, per fetchers/_pdf_validate."""
    body = (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj\n"
    )
    body += b"%" + b"padding" * 200 + b"\n"
    return body + b"startxref\n9\n%%EOF\n"


def _pdf_response(content: bytes) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.content = content
    resp.headers = {"Content-Type": "application/pdf"}
    resp.raise_for_status.return_value = None
    return resp


def test_fetch_pdf_downloads_the_open_access_copy(tmp_path) -> None:
    http = MagicMock()
    http.get.side_effect = [
        _resp(200, {"openAccessPdf": {"url": "https://oa.example/x.pdf"}}),
        _pdf_response(_pdf_bytes()),
    ]
    src = SemanticScholarSource(http=http, config=None)

    result = src.fetch_pdf("10.1/x", cache_dir=tmp_path)

    assert result is not None
    path, url = result
    assert url == "https://oa.example/x.pdf"
    assert path.read_bytes().startswith(b"%PDF")


def test_fetch_pdf_returns_none_when_there_is_no_open_access_copy(tmp_path) -> None:
    """A missing `openAccessPdf` is a real negative, not a retry signal."""
    http = MagicMock()
    http.get.side_effect = [_resp(200, {})]
    src = SemanticScholarSource(http=http, config=None)

    assert src.fetch_pdf("10.1/x", cache_dir=tmp_path) is None
    assert http.get.call_count == 1     # no download attempted


def test_fetch_pdf_rejects_a_truncated_download(tmp_path) -> None:
    """S2 links out to repositories that sometimes serve a landing page
    or a partial file. Returning None lets the cascade try elsewhere."""
    http = MagicMock()
    http.get.side_effect = [
        _resp(200, {"openAccessPdf": {"url": "https://oa.example/x.pdf"}}),
        _pdf_response(b"<html>Sign in to continue</html>"),
    ]
    src = SemanticScholarSource(http=http, config=None)

    assert src.fetch_pdf("10.1/x", cache_dir=tmp_path) is None
    assert not list(tmp_path.glob("*.pdf")), "a rejected body must not be cached"


def test_fetch_pdf_discards_a_corrupt_cached_file(tmp_path) -> None:
    """Serving an unvalidated cache entry makes an earlier truncated
    download permanent — every later run short-circuits on it."""
    cached = tmp_path / "10.1_x.pdf"
    cached.write_bytes(b"%PDF-1.4\ntruncated")

    http = MagicMock()
    http.get.side_effect = [
        _resp(200, {"openAccessPdf": {"url": "https://oa.example/x.pdf"}}),
        _pdf_response(_pdf_bytes()),
    ]
    src = SemanticScholarSource(http=http, config=None)

    result = src.fetch_pdf("10.1/x", cache_dir=tmp_path)
    assert result is not None
    assert result[1] == "https://oa.example/x.pdf"   # re-fetched, not served


def test_fetch_pdf_serves_a_valid_cached_file_without_a_request(tmp_path) -> None:
    cached = tmp_path / "10.1_x.pdf"
    cached.write_bytes(_pdf_bytes())
    http = MagicMock()
    src = SemanticScholarSource(http=http, config=None)

    result = src.fetch_pdf("10.1/x", cache_dir=tmp_path)

    assert result == (cached, f"cache://{cached}")
    http.get.assert_not_called()


def test_fetch_pdf_shares_the_rejected_key_fallback(capsys) -> None:
    """The 403-drop-the-key logic lives in `_get`, so the PDF path
    inherits it rather than reimplementing it."""
    http = _http_returning(
        (403, {}),
        (200, {"openAccessPdf": {"url": "https://oa.example/x.pdf"}}),
    )
    src = SemanticScholarSource(http=http, config=_FetcherConfig())

    assert src._open_access_pdf_url("10.1/x") == "https://oa.example/x.pdf"
    assert src._key_rejected is True
    assert "rejected" in capsys.readouterr().out.lower()


def test_the_source_advertises_both_capabilities() -> None:
    """One class, both ABCs — the registry and the coverage guard walk
    the subclass tree, so this is what puts it in the PDF cascade."""
    from fetchers.base import AbstractFetcher, PdfFetcher

    assert issubclass(SemanticScholarSource, AbstractFetcher)
    assert issubclass(SemanticScholarSource, PdfFetcher)


# ---------------------------------------------------------------------------
# 429 advice must match what the failing request actually carried
# ---------------------------------------------------------------------------


def test_rate_limit_with_a_working_key_does_not_blame_the_key() -> None:
    """The message used to say "Set SEMANTIC_SCHOLAR_API_KEY" no matter
    what. Seen live with a freshly rotated, accepted key: it sends the
    operator to /setup to rotate a credential that is working, while the
    real answer is to wait or paginate less."""
    src = SemanticScholarSearch()
    ctx, calls = _ctx_recording(lambda _n: _resp(429))

    with pytest.raises(RuntimeError) as exc:
        src._fetch_all("entrepreneur", ctx, "live-key")

    msg = str(exc.value)
    assert calls[0].get("x-api-key") == "live-key", "precondition: key was sent"
    assert "Set SEMANTIC_SCHOLAR_API_KEY" not in msg, (
        "telling someone to set the key they already set is the bug"
    )
    assert "throttles per key" in msg


def test_rate_limit_without_a_key_still_recommends_setting_one() -> None:
    """The original advice is right in the case it was written for."""
    src = SemanticScholarSearch()
    ctx, _ = _ctx_recording(lambda _n: _resp(429))

    with pytest.raises(RuntimeError) as exc:
        src._fetch_all("entrepreneur", ctx, "")

    assert "SEMANTIC_SCHOLAR_API_KEY" in str(exc.value)


def test_rate_limit_after_a_rejected_key_gives_unauthenticated_advice() -> None:
    """403 drops the key, so the throttled request that follows really is
    unauthenticated — advise accordingly, not "your key is fine"."""
    def responder(n: int):
        return _resp(403) if n == 1 else _resp(429)

    src = SemanticScholarSearch()
    ctx, _ = _ctx_recording(responder)

    with pytest.raises(RuntimeError) as exc:
        src._fetch_all("entrepreneur", ctx, "dead-key")

    msg = str(exc.value)
    assert "unauthenticated tier" in msg
    assert "throttles per key" not in msg
