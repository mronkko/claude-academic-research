"""Semantic Scholar: the 10k cap is announced; unscopable configs fail early.

- The bulk search stops at 10,000 records, *before* the client-side
  journal filter. It used to stop silently and print the truncated count
  as the whole answer. It now warns, with the API's own total.
- A JOURNALS map with ISSNs and no titles cannot scope S2 (it returns no
  ISSN), so every paper was fetched and then rejected. It now raises
  before the first request.
- The `/citations` URL percent-encodes the seed DOI.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from searchers.base import SearchContext
from searchers.semantic_scholar import BULK_MAX, SemanticScholarSearch


class _Resp:
    def __init__(self, status: int, body: dict):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def _ctx_serving(pages: list[dict]):
    calls: list[str] = []

    class _Session:
        def get(self, url, **_kw):
            calls.append(url)
            return _Resp(200, pages[min(len(calls) - 1, len(pages) - 1)])

    ctx = SearchContext(from_year=2015, to_year=2018, issns=[])
    ctx.session = _Session()
    return ctx, calls


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("searchers.semantic_scholar.time.sleep", lambda _s: None)


def test_hitting_the_bulk_cap_warns_with_the_api_total(capsys) -> None:
    page = {"data": [{"paperId": str(i)} for i in range(1000)],
            "token": "more", "total": 25_000}
    ctx, _ = _ctx_serving([page])
    papers = SemanticScholarSearch()._fetch_all("q", ctx, api_key="")
    assert len(papers) == BULK_MAX
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert f"{BULK_MAX} of 25000" in out


def test_no_cap_warning_under_the_cap(capsys) -> None:
    ctx, _ = _ctx_serving([{"data": [{"paperId": "a"}], "token": None,
                            "total": 1}])
    SemanticScholarSearch()._fetch_all("q", ctx, api_key="")
    assert "WARNING" not in capsys.readouterr().out


def test_issn_only_journals_fail_before_any_request() -> None:
    ctx = SearchContext(from_year=2015, to_year=2018, issns=["0021-9010"],
                        journal_titles=[])

    class _NoNetwork:
        def get(self, *_a, **_kw):
            raise AssertionError("must not search when scope is impossible")

    ctx.session = _NoNetwork()
    with pytest.raises(RuntimeError, match="no journal titles"):
        SemanticScholarSearch().run(SimpleNamespace(BLOCK_A_TERMS=["x"]), ctx)


def test_citations_url_percent_encodes_the_doi() -> None:
    ctx, calls = _ctx_serving([{"data": []}])
    SemanticScholarSearch()._fetch_citations(
        "10.1002/(SICI)1097-0266(199601)17:1<1::AID-SMJ1>3.0.CO;2-#", ctx, "",
    )
    url = calls[0]
    assert "#" not in url and "<" not in url and ";" not in url
    assert url.endswith("/citations")
