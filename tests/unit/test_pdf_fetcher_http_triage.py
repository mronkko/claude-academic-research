"""P22: API PDF fetchers raise on an unanswered request instead of
returning None.

OpenAIRE, BASE, arXiv, ScienceDirect and OpenAlex used to turn every
401 / 403 / 429 / 5xx and every timeout into "no PDF". `_try_cascade`
takes its HTTP status only from a raised exception, so the status
triage (ACCESS_BLOCKED vs NETWORK_ERROR) never fired for them — a 429
storm at Elsevier was logged as a plain miss. Each source's own server
answer now goes through `fetchers.base.pdf_answered`: 200 proceeds,
401/403/429/5xx raise `PdfHttpError` carrying the status, anything
else (404, 410, 400) is still a miss.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests
from fetchers.arxiv import ArxivSource
from fetchers.base import PdfHttpError, pdf_answered
from fetchers.base_search import BaseSearchSource
from fetchers.openaire import OpenAireSource
from fetchers.openalex import OpenAlexContentSource, OpenAlexSource
from fetchers.sciencedirect import ScienceDirectSource

KEY = "SECRETKEY123"


def _resp(status: int, *, url: str = "https://api.example/x"):
    return SimpleNamespace(status_code=status, headers={}, content=b"", url=url,
                           json=lambda: {})


def _http(*, status: int | None = None, exc: BaseException | None = None):
    http = MagicMock()
    if exc is not None:
        http.get.side_effect = exc
    else:
        http.get.return_value = _resp(status)
    return http


def _openaire(http):
    return OpenAireSource(http, None), "10.2307/2666999"


def _base(http):
    return BaseSearchSource(http, None), "10.2307/2666999"


def _arxiv(http):
    return ArxivSource(http, None), "10.48550/arXiv.2007.05443"


def _sciencedirect(http):
    cfg = SimpleNamespace(elsevier_api_key=KEY, elsevier_render_xml_to_pdf=False)
    return ScienceDirectSource(http, cfg), "10.1016/j.jbusvent.2020.106000"


def _openalex_content(http):
    cfg = SimpleNamespace(openalex_api_key=KEY, openalex_use_paid_content_api=True,
                          crossref_mailto="")
    src = OpenAlexContentSource(http, cfg)
    src._ensure_configured = lambda: None
    src._work = lambda doi, **_kw: {
        "id": "https://openalex.org/W9", "has_content": {"pdf": True},
    }
    return src, "10.1038/x"


FETCHERS = {
    "openaire": _openaire,
    "base": _base,
    "arxiv": _arxiv,
    "sciencedirect": _sciencedirect,
    "openalex_content": _openalex_content,
}


# ---------------------------------------------------------------------------
# The shared helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502, 503])
def test_pdf_answered_raises_with_the_status(status) -> None:
    with pytest.raises(PdfHttpError) as info:
        pdf_answered(_resp(status, url=f"https://x/?api_key={KEY}"), "src")
    assert info.value.response.status_code == status
    assert str(info.value) == f"src answered HTTP {status}"
    assert KEY not in str(info.value)


@pytest.mark.parametrize("status", [400, 404, 410])
def test_pdf_answered_reads_other_statuses_as_a_miss(status) -> None:
    assert pdf_answered(_resp(status), "src") is False


def test_pdf_answered_accepts_a_200() -> None:
    assert pdf_answered(_resp(200), "src") is True


# ---------------------------------------------------------------------------
# Per fetcher
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(FETCHERS))
@pytest.mark.parametrize("status", [403, 429, 503])
def test_an_unanswered_request_raises_with_its_status(name, status, tmp_path) -> None:
    src, doi = FETCHERS[name](_http(status=status))
    with pytest.raises(PdfHttpError) as info:
        src.fetch_pdf(doi, cache_dir=tmp_path)
    assert info.value.response.status_code == status
    assert src.name in str(info.value)


@pytest.mark.parametrize("name", sorted(FETCHERS))
def test_a_404_is_still_a_miss(name, tmp_path) -> None:
    src, doi = FETCHERS[name](_http(status=404))
    assert src.fetch_pdf(doi, cache_dir=tmp_path) is None


@pytest.mark.parametrize("name", sorted(FETCHERS))
def test_a_timeout_raises(name, tmp_path) -> None:
    exc = requests.Timeout(f"Read timed out: https://h/x?api_key={KEY}")
    src, doi = FETCHERS[name](_http(exc=exc))
    with pytest.raises(RuntimeError) as info:
        src.fetch_pdf(doi, cache_dir=tmp_path)
    assert "Timeout" in str(info.value)
    assert KEY not in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__


def test_a_third_party_repository_refusal_is_not_blamed_on_openaire(tmp_path) -> None:
    """A candidate URL is a repository host, not OpenAIRE; its 403 says
    nothing about the article's access status, so the fetcher moves on."""
    payload = {"response": {"results": {"result": {"metadata": {"oaf:entity": {
        "oaf:result": {"fulltext": {"$": "https://repo.example.edu/a.pdf"}},
    }}}}}}
    lookup = SimpleNamespace(status_code=200, headers={}, json=lambda: payload)
    http = MagicMock()
    http.get.side_effect = [lookup, _resp(403)]
    src = OpenAireSource(http, None)
    assert src.fetch_pdf("10.2307/2666999", cache_dir=tmp_path) is None


# ---------------------------------------------------------------------------
# OpenAlex: the metadata lookup, and the API key
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_pyalex(monkeypatch):
    """A stand-in `pyalex` whose `Works()[...]` raises what it is told to."""
    state: dict = {}

    class _Works:
        def __getitem__(self, _key):
            if "raise" in state:
                raise state["raise"]
            return state.get("work")

    mod = types.ModuleType("pyalex")
    mod.Works = _Works
    mod.config = SimpleNamespace(email=None, api_key=None)
    monkeypatch.setitem(sys.modules, "pyalex", mod)
    return state


def _lookup_error(status: int) -> requests.HTTPError:
    resp = _resp(status, url=f"https://api.openalex.org/works/doi:x?api_key={KEY}")
    return requests.HTTPError(f"{status} Error for url: {resp.url}", response=resp)


@pytest.mark.parametrize("cls", [OpenAlexSource, OpenAlexContentSource])
@pytest.mark.parametrize("status", [403, 429, 503])
def test_an_openalex_lookup_refusal_raises_without_the_key(
    fake_pyalex, cls, status, tmp_path,
) -> None:
    fake_pyalex["raise"] = _lookup_error(status)
    cfg = SimpleNamespace(openalex_api_key=KEY, openalex_use_paid_content_api=True,
                          crossref_mailto="")
    src = cls(MagicMock(), cfg)
    with pytest.raises(PdfHttpError) as info:
        src.fetch_pdf("10.1038/x", cache_dir=tmp_path)
    assert info.value.response.status_code == status
    assert KEY not in str(info.value)
    src.http.get.assert_not_called()


@pytest.mark.parametrize("cls", [OpenAlexSource, OpenAlexContentSource])
def test_an_openalex_lookup_404_is_a_miss(fake_pyalex, cls, tmp_path) -> None:
    fake_pyalex["raise"] = _lookup_error(404)
    cfg = SimpleNamespace(openalex_api_key=KEY, openalex_use_paid_content_api=True,
                          crossref_mailto="")
    assert cls(MagicMock(), cfg).fetch_pdf("10.1038/x", cache_dir=tmp_path) is None


@pytest.mark.parametrize("cls", [OpenAlexSource, OpenAlexContentSource])
def test_an_openalex_lookup_timeout_raises_without_the_key(
    fake_pyalex, cls, tmp_path,
) -> None:
    fake_pyalex["raise"] = requests.ConnectionError(
        f"Max retries exceeded with url: /works?api_key={KEY}",
    )
    cfg = SimpleNamespace(openalex_api_key=KEY, openalex_use_paid_content_api=True,
                          crossref_mailto="")
    with pytest.raises(RuntimeError) as info:
        cls(MagicMock(), cfg).fetch_pdf("10.1038/x", cache_dir=tmp_path)
    assert "ConnectionError" in str(info.value)
    assert KEY not in str(info.value)


def test_the_content_api_key_never_reaches_the_message(tmp_path) -> None:
    """The download URL is `…/W9.pdf?api_key=…`; whatever the cascade
    prints (`str(e)`) must not carry it, for a status or a transport
    fault alike."""
    for http in (
        _http(status=429),
        _http(exc=requests.ConnectionError(
            f"HTTPSConnectionPool: /works/W9.pdf?api_key={KEY}")),
    ):
        src, doi = _openalex_content(http)
        with pytest.raises(RuntimeError) as info:
            src.fetch_pdf(doi, cache_dir=tmp_path)
        assert KEY not in str(info.value)
        assert KEY not in repr(info.value)


# ---------------------------------------------------------------------------
# Through the cascade
# ---------------------------------------------------------------------------


class _Silent:
    def __init__(self, name: str) -> None:
        self.name = name

    def fetch_pdf(self, doi, *, cache_dir):
        return None


def _cascade_row(tmp_path, sources, doi):
    import enrich_pdfs
    import pdf_fetch_log

    log = tmp_path / "pdf_fetch_log.csv"
    result = enrich_pdfs._try_cascade(
        {"key": "ITEM01", "data": {"DOI": doi, "itemType": "journalArticle"}},
        sources, str(tmp_path), failure_log_path=str(log),
    )
    assert result is None
    rows = pdf_fetch_log.read_failures(log)
    assert len(rows) == 1
    return rows[0]


def test_a_403_from_an_api_source_is_access_blocked(tmp_path) -> None:
    src, doi = _openaire(_http(status=403))
    row = _cascade_row(tmp_path, [_Silent("crossref"), src, _Silent("core")], doi)
    assert row["source"] == "openaire"
    assert row["http_status"] == "403"
    assert row["cause"] == "ACCESS_BLOCKED"


def test_a_429_at_elsevier_is_not_logged_as_a_plain_miss(tmp_path) -> None:
    """The BACKLOG case. A plain miss is blamed on the cascade as a whole
    with no status; a 429 names ScienceDirect and carries the status."""
    import enrich_pdfs

    src, doi = _sciencedirect(_http(status=429))
    row = _cascade_row(tmp_path, [_Silent("crossref"), src, _Silent("core")], doi)
    assert row["source"] == "sciencedirect"
    assert row["source"] != enrich_pdfs._API_CASCADE_SOURCE
    assert row["http_status"] == "429"


def test_a_503_from_an_api_source_is_a_network_error(tmp_path) -> None:
    src, doi = _arxiv(_http(status=503))
    row = _cascade_row(tmp_path, [src, _Silent("core")], doi)
    assert row["source"] == "arxiv"
    assert row["http_status"] == "503"
    assert row["cause"] == "NETWORK_ERROR"
