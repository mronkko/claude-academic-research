"""Fetcher-level contract for rejecting broken PDF downloads.

Regression cover for a live incident: OpenAlex served truncated copies
of five articles. The pipeline's only validation was
`status_code == 200 and content[:4] == b"%PDF"`, which a half-downloaded
file passes, so the corrupt bytes were cached, attached, and recorded as
successes. Retrying produced byte-identical files — the copy stored at
OpenAlex was itself broken — while the publisher's own TDM route
returned the articles intact.

Two behaviours follow, and both are asserted here for every HTTP
fetcher:

1. A broken download yields `None`, so `_try_cascade` moves on to the
   next source instead of accepting the first thing with a `%PDF`
   prefix.
2. A cache entry is validated before it is served. Without this, the
   truncated file written by an earlier run short-circuits every later
   run and the damage is permanent.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fetchers.crossref import CrossrefSource
from fetchers.openalex import OpenAlexContentSource, OpenAlexSource
from fetchers.springer import SpringerSource
from fetchers.unpaywall import UnpaywallSource

DOI = "10.1002/hrm.21999"


def _good_pdf() -> bytes:
    return b"%PDF-1.4\n" + b"0" * 3000 + b"\nstartxref\n12\n%%EOF\n"


def _truncated_pdf() -> bytes:
    """Header intact, xref offset past EOF — the incident's signature."""
    return b"%PDF-1.4\n" + b"0" * 3000 + b"\nstartxref\n1744085\n%%EOF\n"


def _cache_name(doi: str) -> str:
    return doi.replace("/", "_").replace(":", "_") + ".pdf"


# ---------------------------------------------------------------------
# OpenAlex — where the incident happened.
# ---------------------------------------------------------------------

# Both OpenAlex sources download bytes and so both must validate them.
# Parametrized rather than tested once: the paid Content API is where the
# truncation incident actually happened, but the free OA tier writes to
# the same cache path, so an unvalidated write on either side would
# poison the other's cache hit.
_OPENALEX_SOURCES = pytest.mark.parametrize(
    "source_cls", [OpenAlexSource, OpenAlexContentSource],
    ids=["free_oa_tier", "paid_content_api"],
)


def _openalex(monkeypatch, tmp_path, content: bytes, source_cls=OpenAlexSource):
    cfg = MagicMock()
    cfg.openalex_api_key = "key"
    cfg.crossref_mailto = "a@b.c"
    cfg.openalex_use_paid_content_api = True
    src = source_cls(MagicMock(), config=cfg)
    src._ensure_configured = lambda: None

    resp = MagicMock()
    resp.status_code = 200
    resp.content = content
    resp.headers = {}
    src.http.get.return_value = resp

    # Carries both routes' metadata so one fake work serves either
    # source: `has_content.pdf` drives the paid Content API, and
    # `open_access.oa_url` drives the free OA tier.
    fake_pyalex = MagicMock()
    fake_pyalex.Works.return_value = {
        f"doi:{DOI}": {
            "id": "https://openalex.org/W1",
            "has_content": {"pdf": True},
            "open_access": {"oa_url": "https://repo.example.org/paper.pdf"},
        },
    }
    monkeypatch.setitem(__import__("sys").modules, "pyalex", fake_pyalex)
    return src


@_OPENALEX_SOURCES
def test_openalex_rejects_truncated_download(
    monkeypatch, tmp_path, source_cls,
) -> None:
    src = _openalex(monkeypatch, tmp_path, _truncated_pdf(), source_cls)
    assert src.fetch_pdf(DOI, cache_dir=str(tmp_path)) is None
    # Nothing cached — a broken file must not become tomorrow's cache hit.
    assert not (tmp_path / _cache_name(DOI)).exists()


@_OPENALEX_SOURCES
def test_openalex_accepts_intact_download(
    monkeypatch, tmp_path, source_cls,
) -> None:
    src = _openalex(monkeypatch, tmp_path, _good_pdf(), source_cls)
    result = src.fetch_pdf(DOI, cache_dir=str(tmp_path))
    assert result is not None
    assert result[0].read_bytes() == _good_pdf()


@_OPENALEX_SOURCES
def test_openalex_discards_a_poisoned_cache_entry(
    monkeypatch, tmp_path, source_cls,
) -> None:
    """A truncated file left by an earlier, unvalidated run must not be
    served — and must be removed so the next source gets a chance."""
    cached = tmp_path / _cache_name(DOI)
    cached.write_bytes(_truncated_pdf())

    src = _openalex(monkeypatch, tmp_path, _truncated_pdf(), source_cls)
    assert src.fetch_pdf(DOI, cache_dir=str(tmp_path)) is None
    assert not cached.exists()


@_OPENALEX_SOURCES
def test_openalex_serves_a_valid_cache_entry_without_network(
    monkeypatch, tmp_path, source_cls,
) -> None:
    cached = tmp_path / _cache_name(DOI)
    cached.write_bytes(_good_pdf())

    src = _openalex(monkeypatch, tmp_path, _good_pdf(), source_cls)
    src.http.get.side_effect = AssertionError("must not hit the network")

    result = src.fetch_pdf(DOI, cache_dir=str(tmp_path))
    assert result is not None
    assert result[0] == cached
    assert result[1].startswith("cache://")


# ---------------------------------------------------------------------
# The same contract across the other plain-HTTP fetchers.
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "factory, doi",
    [
        (lambda: SpringerSource(MagicMock()), "10.1007/s10551-020-04463-y"),
        (lambda: CrossrefSource(MagicMock()), "10.1016/j.jbusvent.2019.01.001"),
    ],
)
def test_fetcher_discards_poisoned_cache_entry(factory, doi, tmp_path) -> None:
    src = factory()
    cached = tmp_path / _cache_name(doi)
    cached.write_bytes(_truncated_pdf())

    # Force the post-cache path to bail out cheaply; the assertion is
    # about the cache no longer being trusted, not about the network.
    src.http.get.side_effect = RuntimeError("network disabled in this test")
    try:
        src.fetch_pdf(doi, cache_dir=str(tmp_path))
    except RuntimeError:
        pass

    assert not cached.exists(), "truncated cache entry was served or kept"


def test_unpaywall_discards_poisoned_cache_entry(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CROSSREF_MAILTO", "a@b.c")
    src = UnpaywallSource(MagicMock())
    doi = "10.1371/journal.pone.0012345"
    cached = tmp_path / _cache_name(doi)
    cached.write_bytes(_truncated_pdf())

    src.http.get.side_effect = RuntimeError("network disabled in this test")
    try:
        src.fetch_pdf(doi, cache_dir=str(tmp_path))
    except RuntimeError:
        pass

    assert not cached.exists()


def test_valid_cache_entry_is_still_served(tmp_path) -> None:
    """The validation must not throw away good cache entries — that
    would turn every run into a full re-download."""
    src = SpringerSource(MagicMock())
    doi = "10.1007/s10551-020-04463-y"
    cached = tmp_path / _cache_name(doi)
    cached.write_bytes(_good_pdf())
    src.http.get.side_effect = AssertionError("must not hit the network")

    result = src.fetch_pdf(doi, cache_dir=str(tmp_path))
    assert result is not None
    assert result[0] == Path(cached)
