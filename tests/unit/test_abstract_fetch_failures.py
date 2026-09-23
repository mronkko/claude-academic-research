"""None from `fetch_abstract` means "asked, and it has no abstract".

`not_found` in the abstract log licenses a "no abstract exists" ruling
downstream, so it may rest only on sources that answered. Every
abstract fetcher used to swallow timeouts, 429s and 5xx and return
None, and one without a key returned None too — so a transient failure,
or a source never asked, was logged as evidence of absence. 6I9S5R8C
was logged not_found with WoS in its "no abstract at" list although WoS
holds it and says "withheld" when asked again.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import enrich_abstracts
import pytest
from fetchers import CrossrefSource, OpenAlexSource, ScopusSource
from fetchers.base import (
    AbstractWithheld,
    SourceUnavailable,
    answered,
    is_not_found,
)

ITEM = {"data": {"DOI": "10.1/x", "title": "T"}}


class _Src:
    def __init__(self, name, result):
        self.name, self.result = name, result

    def fetch_abstract(self, doi, **kw):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _http_error(status: int) -> Exception:
    e = Exception(f"HTTP {status}")
    e.response = SimpleNamespace(status_code=status)
    return e


# -- the contract ----------------------------------------------------------


def test_an_unavailable_source_is_not_counted_as_asked(capsys) -> None:
    enrich_abstracts._UNAVAILABLE_REPORTED.clear()
    got = enrich_abstracts._try_cascade(ITEM, [
        _Src("openalex", SourceUnavailable("no key")),
        _Src("crossref", None),
    ], "/tmp")
    assert got.asked == ["crossref"]
    assert got.status() == "not_found"
    assert got.detail() == "no abstract at: crossref"
    # Said once per run, not once per item.
    enrich_abstracts._try_cascade(ITEM, [_Src("openalex", SourceUnavailable("no key"))], "/tmp")
    assert capsys.readouterr().out.count("Not asking openalex") == 1


def test_answered() -> None:
    assert answered(SimpleNamespace(status_code=200), "s") is True
    assert answered(SimpleNamespace(status_code=404), "s") is False
    for status in (403, 429, 500, 503):
        with pytest.raises(RuntimeError, match=str(status)):
            answered(SimpleNamespace(status_code=status), "s")


def test_is_not_found() -> None:
    assert is_not_found(_http_error(404))
    assert not is_not_found(_http_error(429))
    assert not is_not_found(TimeoutError())
    scopus_404 = type("Scopus404Error", (Exception,), {})
    assert is_not_found(scopus_404())


# -- Crossref ---------------------------------------------------------------


def _crossref(raises: Exception) -> CrossrefSource:
    src = CrossrefSource(MagicMock(), None)
    src._cr = MagicMock()
    src._cr.works.side_effect = raises
    return src


def test_crossref_404_is_a_miss_and_a_503_is_a_failure() -> None:
    assert _crossref(_http_error(404)).fetch_abstract("10.1/x") is None
    with pytest.raises(Exception, match="503"):
        _crossref(_http_error(503)).fetch_abstract("10.1/x")


# -- Scopus -----------------------------------------------------------------


def _scopus(monkeypatch, full: Exception, meta: Exception | None) -> ScopusSource:
    """Stub pybliometrics: the FULL view raises `full`; the META view
    raises `meta`, or succeeds when it is None."""
    def retrieval(doi, id_type, view, **kw):
        if view == "FULL":
            raise full
        if meta is not None:
            raise meta
        return SimpleNamespace()

    startup = SimpleNamespace(init=lambda: None)
    scopus = SimpleNamespace(AbstractRetrieval=retrieval)
    monkeypatch.setitem(sys.modules, "pybliometrics.utils.startup", startup)
    monkeypatch.setitem(sys.modules, "pybliometrics.scopus", scopus)
    return ScopusSource(MagicMock(), None)


def _scopus_err(name: str) -> Exception:
    return type(name, (Exception,), {})(name)


def test_scopus_401_on_a_doi_it_does_not_hold_is_a_miss(monkeypatch) -> None:
    """FULL answers 401 even for an unknown DOI (10.9999/not-a-doi-xyz,
    2026-09-23); META's 404 shows it is simply not there."""
    src = _scopus(monkeypatch, _scopus_err("Scopus401Error"), _scopus_err("Scopus404Error"))
    assert src.fetch_abstract("10.9999/not-a-doi-xyz") is None


def test_scopus_401_on_a_record_it_holds_is_withheld(monkeypatch) -> None:
    src = _scopus(monkeypatch, _scopus_err("Scopus401Error"), None)
    with pytest.raises(AbstractWithheld):
        src.fetch_abstract("10.1/x")


def test_scopus_429_is_a_failure(monkeypatch) -> None:
    src = _scopus(monkeypatch, _scopus_err("Scopus429Error"), None)
    with pytest.raises(Exception, match="Scopus429Error"):
        src.fetch_abstract("10.1/x")


# -- OpenAlex ---------------------------------------------------------------


def _openalex(api_key: str = "SECRET-KEY") -> OpenAlexSource:
    cfg = SimpleNamespace(
        openalex_api_key=api_key, crossref_mailto="",
        openalex_use_paid_content_api=True,
    )
    src = OpenAlexSource(MagicMock(), cfg)
    src._ensure_configured = lambda: None
    return src


def test_openalex_without_a_key_is_unavailable(monkeypatch) -> None:
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    with pytest.raises(SourceUnavailable):
        _openalex(api_key="").fetch_abstract("10.1/x")


def test_openalex_failures_do_not_leak_the_key(monkeypatch) -> None:
    """pyalex's and requests' messages quote the URL, and the GROBID URL
    carries `api_key=`; the cascade prints the message and logs it."""
    src = _openalex()
    fake = MagicMock()
    fake.Works.return_value.__getitem__.side_effect = _http_error(500)
    fake.Works.return_value.__getitem__.side_effect.args = (
        "500 for url: https://api.openalex.org/works/doi:10.1/x?api_key=SECRET-KEY",
    )
    monkeypatch.setitem(sys.modules, "pyalex", fake)
    with pytest.raises(RuntimeError) as lookup:
        src.fetch_abstract("10.1/x")
    assert "SECRET-KEY" not in str(lookup.value)

    src.http.get.side_effect = ConnectionError(
        "https://content.openalex.org/works/W1.grobid-xml?api_key=SECRET-KEY",
    )
    with pytest.raises(RuntimeError) as download:
        src._download_grobid_xml("W1", None)
    assert "SECRET-KEY" not in str(download.value)


def test_openalex_unknown_doi_is_a_miss(monkeypatch) -> None:
    src = _openalex()
    fake = MagicMock()
    fake.Works.return_value.__getitem__.side_effect = _http_error(404)
    monkeypatch.setitem(sys.modules, "pyalex", fake)
    assert src.fetch_abstract("10.1/x") is None
