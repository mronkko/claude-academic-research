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
                         search_fields=kw.get("search_fields", "all"))


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
    works, total = src._fetch_all('"x"', "f", _ctx())
    assert len(works) == 12_000
    assert total == 12_000
    assert src.calls[0]["cursor"] == "*"
    assert src.calls[1]["cursor"] == "c1"


def test_run_prints_the_api_total(capsys) -> None:
    src = _Paged(n_pages=2, per_page=3)
    src.run(SimpleNamespace(BLOCK_A_TERMS=["x"]), _ctx())
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
