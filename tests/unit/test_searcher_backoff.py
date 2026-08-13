"""The search stage must inherit `http_client`'s retry policy.

Before this, all three HTTP searchers called `requests.get` directly and
hand-rolled their own throttling. The worst case was
`semantic_scholar._fetch_all`: a `while True` that answered HTTP 429 with
a fixed `time.sleep(5); continue` — no cap, no jitter, no `Retry-After`.
Against a tier that keeps throttling, that loop never exits and the run
appears to hang.

These tests pin the replacement: every HTTP source goes through
`SearchContext.http()` (a `http_client.build_session()` with exponential
backoff on 429/5xx), and a request that fails after the session's retries
are exhausted raises instead of looping.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from searchers import (
    OpenAlexSearch,
    SearchContext,
    SemanticScholarSearch,
    WosSearch,
)

PIPELINES = Path(__file__).resolve().parents[2] / "scripts" / "pipelines"


def _ctx() -> SearchContext:
    return SearchContext(from_year=2000, to_year=2020, issns=["1234-5678"])


# ---------------------------------------------------------------------------
# SearchContext.http()
# ---------------------------------------------------------------------------


def test_http_returns_a_session_with_the_retry_adapter() -> None:
    session = _ctx().http()
    adapter = session.get_adapter("https://api.openalex.org/works")
    retries = adapter.max_retries
    assert retries.total == 5
    assert retries.backoff_factor == 1.0
    assert 429 in retries.status_forcelist
    assert retries.respect_retry_after_header is True


def test_http_is_built_once_and_reused() -> None:
    ctx = _ctx()
    assert ctx.http() is ctx.http()


def test_http_is_not_built_until_asked_for() -> None:
    """Constructing a context must not import requests or open a session.

    `credentials_error()` pre-flight builds a context and never makes a
    request; so do most tests.
    """
    assert _ctx().session is None


def test_http_passes_mailto_into_the_user_agent() -> None:
    ctx = SearchContext(
        from_year=2000, to_year=2020, issns=[], mailto="a@example.org",
    )
    assert "mailto:a@example.org" in ctx.http().headers["User-Agent"]


# ---------------------------------------------------------------------------
# Exhausted retries raise; they do not loop.
# ---------------------------------------------------------------------------


def test_semantic_scholar_raises_when_retries_are_exhausted(monkeypatch) -> None:
    """The regression this whole module exists for.

    `get_json` returns None once the session's bounded retries are spent.
    The old code treated a 429 as "sleep and go round again" forever.
    """
    import http_client

    calls = 0

    def fake_get_json(*_a, **_kw):
        nonlocal calls
        calls += 1
        return None

    monkeypatch.setattr(http_client, "get_json", fake_get_json)
    with pytest.raises(RuntimeError, match="Semantic Scholar"):
        SemanticScholarSearch()._fetch_all("q", _ctx(), api_key="")
    assert calls == 1, "must not retry in-process; the session owns retries"


def test_openalex_raises_when_retries_are_exhausted(monkeypatch) -> None:
    import http_client

    monkeypatch.setattr(http_client, "get_json", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="OpenAlex"):
        OpenAlexSearch()._fetch_page("q", "filter", 1, _ctx())


def test_wos_raises_when_retries_are_exhausted(monkeypatch) -> None:
    import http_client

    monkeypatch.setattr(http_client, "get_json", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="WoS"):
        WosSearch()._fetch_page(_ctx(), "key", "TS=test", first_record=1)


def test_semantic_scholar_paginates_through_the_shared_session(monkeypatch) -> None:
    """Happy path: the token loop still works, and every call is a
    `get_json` against the context's session."""
    import http_client

    pages = [
        {"data": [{"paperId": "a"}], "token": "t1"},
        {"data": [{"paperId": "b"}], "token": None},
    ]
    seen_sessions = []

    def fake_get_json(session, _url, **_kw):
        seen_sessions.append(session)
        return pages.pop(0)

    monkeypatch.setattr(http_client, "get_json", fake_get_json)
    monkeypatch.setattr("searchers.semantic_scholar.time.sleep", lambda _s: None)

    ctx = _ctx()
    papers = SemanticScholarSearch()._fetch_all("q", ctx, api_key="")
    assert [p["paperId"] for p in papers] == ["a", "b"]
    assert seen_sessions == [ctx.session, ctx.session]


# ---------------------------------------------------------------------------
# Guard: the pattern must not come back.
# ---------------------------------------------------------------------------

_HTTP_VERBS = {"get", "post", "put", "patch", "delete", "head", "request"}

# `http_client` is where the retrying session is defined, so it is the
# one module allowed to call `requests` directly.
_ALLOWED = {"http_client.py"}


def _bare_requests_calls(source: str) -> list[int]:
    """Line numbers of `requests.<verb>(...)` calls in `source`.

    Walks the AST rather than grepping, so a docstring that *mentions*
    `requests.patch()` (import_to_zotero.py does, describing code that
    was removed) is not mistaken for a call site.
    """
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _HTTP_VERBS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "requests"
    ]


def test_no_pipeline_module_calls_requests_directly() -> None:
    """Any new outbound HTTP goes through `http_client.build_session()`.

    A bare `requests.get` has no retry adapter, so it fails on the first
    transient 429 or 503 — or, worse, invites the hand-rolled
    `sleep`-and-retry loop this module was written to remove. A session
    built by `build_session()` carries the policy on every call, so
    `session.post(...)` is the sanctioned form.
    """
    offenders = [
        f"{p.relative_to(PIPELINES)}:{line}"
        for p in PIPELINES.rglob("*.py")
        if p.name not in _ALLOWED
        for line in _bare_requests_calls(p.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "These call `requests` directly instead of routing through "
        "`http_client.build_session()`, so they get no retry/backoff:\n  "
        + "\n  ".join(offenders)
    )


def test_search_scripts_declare_what_http_client_imports() -> None:
    """`http_client` imports urllib3 and tenacity.

    Every `uv run`-able script that reaches it must declare both in its
    PEP 723 block, or the import fails at runtime with no venv to fall
    back on. The searchers reach it via `SearchContext.http()`.
    """
    missing: list[str] = []
    for script in sorted(PIPELINES.glob("search*.py")):
        header = script.read_text(encoding="utf-8").split("# ///")[1]
        for dep in ("urllib3", "tenacity"):
            if dep not in header:
                missing.append(f"{script.name}: {dep}")
    assert not missing, (
        "PEP 723 block is missing a dependency http_client needs:\n  "
        + "\n  ".join(missing)
    )
