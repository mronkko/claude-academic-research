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
from types import SimpleNamespace

import pytest
import requests
from searchers import (
    OpenAlexSearch,
    SearchContext,
    SemanticScholarSearch,
    WosSearch,
)

PIPELINES = Path(__file__).resolve().parents[2] / "scripts" / "pipelines"


def _ctx() -> SearchContext:
    return SearchContext(from_year=2000, to_year=2020, issns=["1234-5678"])


def _ctx_with(responses: list) -> tuple[SearchContext, list]:
    """A context whose session replays `responses`, recording each call.

    Semantic Scholar reaches the session with `ctx.http().get(...)`
    rather than `http_client.get_json`, because it has to tell a 403
    (rejected API key — drop it and retry unauthenticated) apart from
    every other 4xx, and `get_json` collapses them all to None. So its
    tests inject a session instead of patching `get_json`. Pre-seeding
    `session` also keeps `http()` from building a real one, which is
    what stops a monkeypatch miss from turning into a live request.
    """
    calls: list[dict] = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append({"url": url, "params": params or {},
                      "headers": dict(headers or {})})
        return responses[min(len(calls) - 1, len(responses) - 1)]

    session = SimpleNamespace(get=fake_get)
    ctx = SearchContext(
        from_year=2000, to_year=2020, issns=["1234-5678"], session=session,
    )
    return ctx, calls


def _resp(status_code: int, payload: dict | None = None) -> SimpleNamespace:
    def raise_for_status() -> None:
        if status_code >= 400:
            raise requests.HTTPError(f"{status_code}")

    return SimpleNamespace(
        status_code=status_code,
        json=lambda: payload or {},
        raise_for_status=raise_for_status,
    )


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


def test_semantic_scholar_raises_when_retries_are_exhausted() -> None:
    """The regression this whole module exists for.

    A 429 reaching the source means the session's bounded retries are
    already spent. The old code answered it with `sleep(5); continue`,
    forever. It must raise instead — and must not re-request in-process,
    because the session owns retries.
    """
    ctx, calls = _ctx_with([_resp(429)])
    with pytest.raises(RuntimeError, match="Semantic Scholar"):
        SemanticScholarSearch()._fetch_all("q", ctx, api_key="")
    assert len(calls) == 1, "must not retry in-process; the session owns retries"


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
    """Happy path: the token loop still works, and every call goes to the
    context's session rather than a module-level `requests`."""
    monkeypatch.setattr("searchers.semantic_scholar.time.sleep", lambda _s: None)

    ctx, calls = _ctx_with([
        _resp(200, {"data": [{"paperId": "a"}], "token": "t1"}),
        _resp(200, {"data": [{"paperId": "b"}], "token": None}),
    ])
    papers = SemanticScholarSearch()._fetch_all("q", ctx, api_key="")
    assert [p["paperId"] for p in papers] == ["a", "b"]
    assert len(calls) == 2
    assert calls[1]["params"]["token"] == "t1"



# ---------------------------------------------------------------------------
# Guard: the pattern must not come back.
# ---------------------------------------------------------------------------

_HTTP_VERBS = {"get", "post", "put", "patch", "delete", "head", "options", "request"}

#: Fully-qualified callables that make (or build a client for) an
#: outbound request without `http_client`'s retry adapter.
_UNRETRIED = (
    {f"requests.{verb}" for verb in _HTTP_VERBS}
    | {f"requests.api.{verb}" for verb in _HTTP_VERBS}
    | {"requests.Session", "requests.session", "requests.sessions.Session",
       "urllib.request.urlopen"}
)

# `http_client` is where the retrying session is defined, so it is the
# one module allowed to call `requests` directly.
_ALLOWED_FILES = {"http_client.py"}

#: Existing call sites that predate this guard's wider net:
#: (path relative to scripts/pipelines, fully-qualified callee) ->
#: (number of call sites, reason). Each is a known gap owned by the
#: module's workstream; the entry is here so the guard is strict for *new*
#: code today. The count is exact, so a new call in an allow-listed file
#: still fails, and fixing one fails `test_allow_list_has_no_stale_entries`
#: until the count is lowered.
_KNOWN_SITES: dict[tuple[str, str], tuple[int, str]] = {
    ("enrich_pdfs.py", "requests.Session"): (2, (
        "deliberately bare, each with a comment at the call site: the "
        "Zotero-desktop ping (a 3 s localhost probe the retry policy would "
        "turn into a ~30 s stall when Zotero is closed) and the serial "
        "resolver pre-flight (retries would turn one resolver outage into "
        "hours; a failed query already falls back to trying the item)"
    )),
    ("zotero_io.py", "urllib.request.urlopen"): (3, (
        "api.zotero.org group listing (from_config) plus two local Zotero "
        "desktop endpoints — stdlib, no retry"
    )),
    ("bbt_client.py", "urllib.request.urlopen"): (2, (
        "Better BibTeX JSON-RPC on localhost:23119 — stdlib on purpose, no retry"
    )),
}


def _qualified_aliases(tree: ast.AST) -> dict[str, str]:
    """Local name -> fully-qualified module/attribute it is bound to.

    Covers `import requests as _requests`, `import urllib.request`,
    `from requests import Session`, `from urllib import request` and the
    like, anywhere in the file — a lazy import inside a function binds a
    name just as well, which is exactly how enrich_pdfs.py's
    `_requests.Session()` slipped past the old `requests.<verb>` match.
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    aliases[a.asname] = a.name
                else:
                    # `import urllib.request` binds `urllib`.
                    top = a.name.split(".")[0]
                    aliases[top] = top
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for a in node.names:
                aliases[a.asname or a.name] = f"{node.module}.{a.name}"
    return aliases


def _qualified_name(func: ast.AST, aliases: dict[str, str]) -> str | None:
    parts: list[str] = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if not isinstance(func, ast.Name) or func.id not in aliases:
        return None
    return ".".join([aliases[func.id], *reversed(parts)])


def _unretried_calls(source: str) -> list[tuple[int, str]]:
    """(line, qualified callee) for every unretried HTTP call in `source`.

    Walks the AST rather than grepping, so a docstring that *mentions*
    `requests.patch()` (import_to_zotero.py does, describing code that
    was removed) is not mistaken for a call site.
    """
    tree = ast.parse(source)
    aliases = _qualified_aliases(tree)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _qualified_name(node.func, aliases)
            if name in _UNRETRIED:
                found.append((node.lineno, name))
    return found


def _all_sites() -> list[tuple[str, int, str]]:
    return [
        (p.relative_to(PIPELINES).as_posix(), line, name)
        for p in sorted(PIPELINES.rglob("*.py"))
        if p.name not in _ALLOWED_FILES
        for line, name in _unretried_calls(p.read_text(encoding="utf-8"))
    ]


@pytest.mark.parametrize("snippet, expected", [
    ("import requests\nrequests.get('u')", "requests.get"),
    ("import requests as _r\n_r.post('u')", "requests.post"),
    ("from requests import get\nget('u')", "requests.get"),
    ("from requests import get as fetch\nfetch('u')", "requests.get"),
    ("import requests\nrequests.Session()", "requests.Session"),
    ("def f():\n    import requests as _requests\n    _requests.Session()", "requests.Session"),
    ("from requests import Session\nSession()", "requests.Session"),
    ("import urllib.request\nurllib.request.urlopen('u')", "urllib.request.urlopen"),
    ("import urllib.request as ur\nur.urlopen('u')", "urllib.request.urlopen"),
    ("from urllib.request import urlopen\nurlopen('u')", "urllib.request.urlopen"),
    ("from urllib import request\nrequest.urlopen('u')", "urllib.request.urlopen"),
])
def test_the_guard_sees_every_spelling(snippet: str, expected: str) -> None:
    assert [name for _, name in _unretried_calls(snippet)] == [expected]


@pytest.mark.parametrize("snippet", [
    '"""Uses requests.patch() historically."""',
    "import http_client\ns = http_client.build_session()\ns.get('u')",
    "from urllib.parse import urlparse\nurlparse('u')",
    "import requests\nx: requests.Session | None = None",
])
def test_the_guard_ignores_sanctioned_and_non_calls(snippet: str) -> None:
    assert _unretried_calls(snippet) == []


def _site_counts() -> dict[tuple[str, str], list[int]]:
    counts: dict[tuple[str, str], list[int]] = {}
    for path, line, name in _all_sites():
        counts.setdefault((path, name), []).append(line)
    return counts


def test_no_pipeline_module_calls_requests_directly() -> None:
    """Any new outbound HTTP goes through `http_client.build_session()`.

    A bare `requests.get` — or a bare `requests.Session()`, or
    `urllib.request.urlopen` — has no retry adapter, so it fails on the
    first transient 429 or 503, or invites the hand-rolled
    `sleep`-and-retry loop this module was written to remove. A session
    built by `build_session()` carries the policy on every call, so
    `session.post(...)` is the sanctioned form.
    """
    offenders = []
    for (path, name), lines in sorted(_site_counts().items()):
        allowed = _KNOWN_SITES.get((path, name), (0, ""))[0]
        if len(lines) > allowed:
            offenders.append(
                f"{path}:{','.join(map(str, sorted(lines)))}  {name} "
                f"({len(lines)} sites, {allowed} allow-listed)"
            )
    assert not offenders, (
        "These make HTTP requests without `http_client.build_session()`, "
        "so they get no retry/backoff:\n  " + "\n  ".join(offenders)
    )


def test_allow_list_has_no_stale_entries() -> None:
    counts = _site_counts()
    stale = sorted(
        f"{key}: allow-lists {allowed}, found {len(counts.get(key, []))}"
        for key, (allowed, _reason) in _KNOWN_SITES.items()
        if len(counts.get(key, [])) < allowed
    )
    assert not stale, (
        "_KNOWN_SITES allows more call sites than exist — lower the count "
        "(or drop the entry) so the guard covers them again:\n  " + "\n  ".join(stale)
    )


# The PEP 723 half of this guard — every `uv run` script that reaches
# `http_client` declares urllib3 and tenacity — lives with the other
# inline-metadata checks in test_pep723_script_deps.py, where it now
# covers every script rather than only search*.py.
