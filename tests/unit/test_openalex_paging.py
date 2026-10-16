"""OpenAlex keyword paging, seed resolution and filter construction.

- The keyword stream paged with `page=` and stopped at 10,000 results
  without a word, printing the truncated count as the answer. It now
  pages by cursor, like the citation stream, and prints the API total.
- Resolving a citation seed read *any* non-2xx (a 429 that outlasted the
  retries, a 403, a 5xx) as "seed not found", silently dropping that
  seed's whole citation stream. Only a 404 means "not found" now.
- The seed DOI is percent-encoded into the URL path.
- An empty `JOURNALS` produced `primary_location.source.issn:,` (a 400);
  a comma inside a title/abstract term ended the filter early.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from searchers import OpenAlexSearch
from searchers.base import SearchContext


def _ctx(**kw) -> SearchContext:
    return SearchContext(from_year=2011, to_year=2020,
                         issns=kw.get("issns", ["0021-9010"]),
                         search_fields=kw.get("search_fields", "all"),
                         openalex_api_key=kw.get("key", ""))


class _Paged(OpenAlexSearch):
    """Serves `n_pages` full pages by cursor; records what was asked."""

    def __init__(self, n_pages: int, per_page: int = 200, total: int | None = None):
        super().__init__()
        self.n_pages = n_pages
        self.per_page = per_page
        self.total = n_pages * per_page if total is None else total
        self.calls: list[dict] = []

    def _fetch_page(self, query, filter_str, cursor, ctx):
        self.calls.append({"query": query, "filter": filter_str, "cursor": cursor})
        i = len(self.calls) - 1
        if i >= self.n_pages:
            return {"results": [], "meta": {"count": self.total, "next_cursor": None}}
        results = [{"id": f"W{i}-{j}"} for j in range(self.per_page)]
        nxt = f"c{i + 1}" if i + 1 < self.n_pages else None
        return {"results": results, "meta": {"count": self.total, "next_cursor": nxt}}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("searchers.openalex.time.sleep", lambda _s: None)


def test_keyword_paging_is_not_capped_at_ten_thousand() -> None:
    src = _Paged(n_pages=60)  # 12,000 works
    works, total = src._fetch_all('"x"', "f", _ctx(key="k"))
    assert len(works) == 12_000
    assert total == 12_000
    assert src.calls[0]["cursor"] == "*"
    assert src.calls[1]["cursor"] == "c1"


def test_run_prints_the_api_total(capsys) -> None:
    src = _Paged(n_pages=2, per_page=3)
    src.run(SimpleNamespace(BLOCK_A_TERMS=["x"]), _ctx(key="k"))
    assert "6 results (API total: 6)" in capsys.readouterr().out


def test_run_warns_when_fewer_works_arrive_than_the_api_reported(capsys) -> None:
    src = _Paged(n_pages=1, per_page=3, total=10)
    src.run(SimpleNamespace(BLOCK_A_TERMS=["x"]), _ctx())
    out = capsys.readouterr().out
    assert "3 results (API total: 10)" in out
    assert "WARNING" in out


# --- seed resolution ---------------------------------------------------

class _Resp:
    def __init__(self, status: int, body: dict | None = None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._body = body or {}

    def json(self):
        return self._body


def _ctx_answering(status: int, body: dict | None = None):
    calls: list[str] = []

    class _Session:
        def get(self, url, **_kw):
            calls.append(url)
            return _Resp(status, body)

    ctx = _ctx()
    ctx.session = _Session()
    return ctx, calls


def test_seed_404_is_not_found() -> None:
    ctx, _ = _ctx_answering(404)
    assert OpenAlexSearch()._resolve_work_id("10.1/x", ctx) == ""


@pytest.mark.parametrize("status", [429, 403, 500, 503])
def test_seed_throttling_or_server_error_is_a_failure_not_a_miss(status) -> None:
    ctx, _ = _ctx_answering(status)
    with pytest.raises(RuntimeError, match=f"HTTP {status}"):
        OpenAlexSearch()._resolve_work_id("10.1/x", ctx)


def test_seed_resolves_to_the_short_id() -> None:
    ctx, _ = _ctx_answering(200, {"id": "https://openalex.org/W42"})
    assert OpenAlexSearch()._resolve_work_id("10.1/x", ctx) == "W42"


def test_seed_doi_is_percent_encoded_in_the_path() -> None:
    ctx, calls = _ctx_answering(200, {"id": "https://openalex.org/W1"})
    OpenAlexSearch()._resolve_work_id(
        "10.1002/(SICI)1097-0266(199601)17:1<1::AID-SMJ1>3.0.CO;2-#", ctx,
    )
    url = calls[0]
    assert "#" not in url and "<" not in url and ">" not in url and ";" not in url
    assert url.startswith("https://api.openalex.org/works/doi:10.1002/")


# --- filter construction -----------------------------------------------

def test_empty_journals_omits_the_issn_filter() -> None:
    f = OpenAlexSearch()._build_filter([], 2011, 2020)
    assert "issn" not in f
    assert f == "publication_year:2011-2020,type:article"


def test_title_abstract_filter_survives_a_comma_in_a_term() -> None:
    src = _Paged(n_pages=0)
    src.run(SimpleNamespace(BLOCK_A_TERMS=["Smith, Jones", "a|b"]),
            _ctx(search_fields="title_abstract"))
    filt = src.calls[0]["filter"]
    search_part = filt.split("title_and_abstract.search:", 1)[1]
    assert "," not in search_part
    assert "|" not in search_part
    assert search_part == '"Smith Jones" OR "a b"'


# --- API key and paging mode ------------------------------------------
#
# OpenAlex answers page 2 of a keyless `cursor=` query with HTTP 401
# {"error": "API key required for cursor paging", ...} (seen live,
# 2026-10-15), and the searcher never sent the key the wizard stores, so
# every search with more than one page of results failed. With a key it
# now rides on every request and paging is by cursor; without one, by
# `page=` up to OpenAlex's 10,000-result ceiling.

KEY = "oa-secret-key-1234"


class _HttpResp:
    def __init__(self, status: int, body: dict | None = None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._body = body or {}
        self.text = str(self._body)

    def json(self):
        return self._body


def _session_serving(pages: list[dict] | None = None, *,
                     status: int = 200, body: dict | None = None,
                     total: int | None = None):
    """A fake session: `/works` answers from `pages` (or with `status`),
    a seed lookup answers W1. Records every request's params."""
    calls: list[dict] = []

    class _Session:
        def get(self, url, params=None, **_kw):
            calls.append({"url": url, "params": dict(params or {})})
            if "/works/doi:" in url:
                return _HttpResp(200, {"id": "https://openalex.org/W1"})
            if status != 200:
                return _HttpResp(status, body)
            served = sum(1 for c in calls if "/works/doi:" not in c["url"])
            i = served - 1
            results = pages[i] if pages and i < len(pages) else []
            nxt = f"c{i + 1}" if pages and i + 1 < len(pages) else None
            return _HttpResp(200, {"results": results, "meta": {
                "count": total if total is not None
                else sum(len(p) for p in pages or []),
                "next_cursor": nxt,
            }})

    return _Session(), calls


def _full_page(tag: str) -> list[dict]:
    return [{"id": f"{tag}-{j}"} for j in range(200)]


def _keyed_ctx(session, key: str = "") -> SearchContext:
    ctx = _ctx(key=key)
    ctx.session = session
    return ctx


def test_the_configured_key_rides_on_every_request() -> None:
    session, calls = _session_serving([_full_page("a"), [{"id": "b"}]])
    OpenAlexSearch().run_citations(["10.1/x"], _keyed_ctx(session, KEY))
    assert len(calls) == 3  # seed lookup + two pages
    assert all(c["params"].get("api_key") == KEY for c in calls)


def test_with_a_key_paging_is_by_cursor() -> None:
    session, calls = _session_serving([_full_page("a"), [{"id": "b"}]])
    works, _ = OpenAlexSearch()._fetch_all('"x"', "f", _keyed_ctx(session, KEY))
    assert len(works) == 201
    assert [c["params"].get("cursor") for c in calls] == ["*", "c1"]
    assert all("page" not in c["params"] for c in calls)


def test_without_a_key_paging_is_by_page_number_and_sends_no_key() -> None:
    session, calls = _session_serving([_full_page("a"), [{"id": "b"}]])
    works, total = OpenAlexSearch()._fetch_all('"x"', "f", _keyed_ctx(session))
    assert len(works) == 201 and total == 201
    assert [c["params"].get("page") for c in calls] == [1, 2]
    assert all("cursor" not in c["params"] and "api_key" not in c["params"]
               for c in calls)


def test_keyless_paging_stops_at_the_cap_and_says_why(capsys) -> None:
    pages = [_full_page(str(i)) for i in range(60)]  # 12,000 on offer
    session, calls = _session_serving(pages, total=12_000)
    OpenAlexSearch().run(SimpleNamespace(BLOCK_A_TERMS=["x"]),
                         _keyed_ctx(session))
    assert len(calls) == 50, "page 51 is a 400 from OpenAlex — never ask for it"
    out = capsys.readouterr().out
    assert "10000 results (API total: 12000)" in out
    assert "without an API key" in out
    assert "https://openalex.org/settings/api" in out
    assert "OPENALEX_API_KEY" in out


def test_no_cap_warning_when_the_query_fits(capsys) -> None:
    session, _ = _session_serving([[{"id": "a"}]])
    OpenAlexSearch().run(SimpleNamespace(BLOCK_A_TERMS=["x"]),
                         _keyed_ctx(session))
    assert "without an API key" not in capsys.readouterr().out


def test_a_key_required_401_says_how_to_get_one() -> None:
    session, _ = _session_serving(status=401, body={
        "error": "API key required for cursor paging",
        "message": "Paging past the first page with cursor= needs a free "
                   "API key.",
    })
    with pytest.raises(RuntimeError) as err:
        OpenAlexSearch()._fetch_page("", "f", "c1", _keyed_ctx(session))
    msg = str(err.value)
    assert "API key required for cursor paging" in msg
    assert "OPENALEX_API_KEY" in msg
    assert "https://openalex.org/settings/api" in msg


def test_a_rejected_key_is_named_and_never_quoted() -> None:
    session, _ = _session_serving(status=401, body={
        "error": "Invalid or missing API key",
        "message": f"API key {KEY} not found",
    })
    with pytest.raises(RuntimeError) as err:
        OpenAlexSearch()._fetch_page("", "f", "*", _keyed_ctx(session, KEY))
    msg = str(err.value)
    assert "refused the configured API key" in msg
    assert KEY not in msg


def test_a_transport_error_quoting_the_url_is_redacted() -> None:
    class _Boom:
        def get(self, url, params=None, **_kw):
            raise ConnectionError(
                f"Max retries exceeded with url: /works?api_key={KEY}&page=1",
            )

    with pytest.raises(RuntimeError) as err:
        OpenAlexSearch()._fetch_page("", "f", 1, _keyed_ctx(_Boom(), KEY))
    assert KEY not in str(err.value)
    assert "api_key=***" in str(err.value)


def test_a_context_is_keyless_unless_given_a_key() -> None:
    """`search.py` resolves the key into the context; one built anywhere
    else (a test, a pre-flight) must not pick it up from the machine."""
    assert _ctx().openalex_api_key == ""
    assert KEY not in repr(_ctx(key=KEY)), "the key must stay out of repr()"


def test_the_paid_pdf_fetcher_returns_its_url_without_the_key(
    tmp_path, monkeypatch,
) -> None:
    """`fetch_pdf`'s source URL is logged and recorded as provenance."""
    from fetchers.openalex import OpenAlexContentSource

    pdf = b"%PDF-1.4\n" + b"x" * 2048 + b"\n%%EOF"

    class _Pdf:
        status_code = 200
        ok = True
        content = pdf
        headers = {"Content-Type": "application/pdf"}

    fetcher = OpenAlexContentSource.__new__(OpenAlexContentSource)
    fetcher.config = SimpleNamespace(openalex_api_key=KEY,
                                     openalex_use_paid_content_api=True,
                                     crossref_mailto="")
    fetcher.http = SimpleNamespace(get=lambda url, **_kw: _Pdf())
    monkeypatch.setattr(fetcher, "_ensure_configured", lambda: None)
    monkeypatch.setattr(fetcher, "_work", lambda doi, **_kw: {
        "id": "https://openalex.org/W9", "has_content": {"pdf": True},
    })
    result = fetcher.fetch_pdf("10.1/x", cache_dir=str(tmp_path))
    assert result is not None
    assert KEY not in result[1]
