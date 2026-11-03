"""`max_records`: a pilot takes each database's first page and its hit total.

Without a cap a search fetches everything; with one it must stop after the
first page and still report the total the database gave, because that
total (not the page) is what a pilot is for.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from searchers.base import SearchContext
from searchers.openalex import OpenAlexSearch
from searchers.scopus import ScopusSearch
from searchers.semantic_scholar import SemanticScholarSearch
from searchers.wos import WosSearch


def _ctx(cap: int = 0, **kw) -> SearchContext:
    return SearchContext(from_year=2023, to_year=2025, issns=[],
                         max_records=cap, **kw)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("searchers.openalex.time.sleep", lambda _s: None)
    monkeypatch.setattr("searchers.wos.time.sleep", lambda _s: None)
    monkeypatch.setattr("searchers.semantic_scholar.time.sleep", lambda _s: None)


def test_record_total_sums_queries_and_ignores_non_integers():
    src = OpenAlexSearch()
    assert src.total_hits is None
    src.record_total("a", 10)
    src.record_total("b", 5)
    src.record_total("c", None)
    assert src.total_hits == 15


def test_openalex_stops_after_one_small_page_and_reports_the_total():
    class _Src(OpenAlexSearch):
        def __init__(self):
            super().__init__()
            self.calls = []

        def _fetch_page(self, query, filter_str, position, ctx, per_page=200):
            self.calls.append(per_page)
            return {"results": [{"id": f"W{i}"} for i in range(per_page)],
                    "meta": {"count": 4321, "next_cursor": "next"}}

    src = _Src()
    works, total = src._fetch_all('"x"', "f", _ctx(), limit=7)
    assert len(works) == 7 and total == 4321
    assert src.calls == [7]


def test_openalex_without_a_cap_is_unchanged():
    class _Src(OpenAlexSearch):
        def _fetch_page(self, query, filter_str, position, ctx):
            return {"results": [{"id": "W1"}], "meta": {"count": 1}}

    works, total = _Src()._fetch_all('"x"', "f", _ctx())
    assert len(works) == 1 and total == 1


def test_wos_fetches_only_the_first_page():
    pages = []

    class _Src(WosSearch):
        def _fetch_page(self, ctx, api_key, query, first_record, count=100):
            pages.append((first_record, count))
            recs = [{"UID": f"U{first_record + i}"} for i in range(count)]
            return {"QueryResult": {"RecordsFound": 5000},
                    "Data": {"Records": {"records": {"REC": recs}}}}

        def _extract_record(self, rec, label):
            return {"db": "wos", "doi": rec["UID"]}

    src = _Src()
    config = SimpleNamespace(QUERY_DEFS=[("Q", "s", "w")])
    import os
    os.environ["WOS_API_KEY_EXTENDED"] = "k"
    try:
        rows = src.run(config, _ctx(cap=12))
    finally:
        del os.environ["WOS_API_KEY_EXTENDED"]
    assert len(rows) == 12
    assert src.total_hits == 5000
    assert pages == [(1, 1), (1, 12)]


def test_semantic_scholar_stops_after_one_page_and_keeps_the_total():
    class _Resp:
        status_code = 200

        def json(self):
            return {"data": [{"paperId": str(i)} for i in range(50)],
                    "total": 999, "token": "more"}

        def raise_for_status(self):
            pass

    class _Session:
        n = 0

        def get(self, *_a, **_k):
            _Session.n += 1
            return _Resp()

    src = SemanticScholarSearch()
    papers = src._fetch_all("q", _ctx(cap=10, session=_Session()), api_key="")
    assert len(papers) == 10
    assert _Session.n == 1
    assert src._last_total == 999


def test_scopus_first_page_is_two_requests_and_does_not_download_everything(
        monkeypatch):
    class _Fake:
        def __init__(self, query, download=True):
            assert download is False  # the full download is exactly what a pilot avoids
            self._view = "COMPLETE"
            self._json = []

        def get_results_size(self):
            return 300

        @property
        def results(self):
            return list(self._json)

    class _Resp:
        def json(self):
            return {"search-results": {"entry": [{"n": i} for i in range(15)]}}

    asked = {}

    def _get_content(url, api, params):
        asked.update(params)
        return _Resp()

    monkeypatch.setattr("pybliometrics.utils.get_content", _get_content)
    rows, total = ScopusSearch._first_page(_Fake, "q", 10)
    assert total == 300 and len(rows) == 10
    assert asked["count"] == 10 and asked["start"] == 0


def test_scopus_empty_result_makes_no_second_request():
    class _Fake:
        def __init__(self, query, download=True):
            self._view = "COMPLETE"

        def get_results_size(self):
            return 0

    assert ScopusSearch._first_page(_Fake, "q", 10) == ([], 0)
