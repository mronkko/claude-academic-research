"""Tests for fetchers.base ABCs and the fetchers registry shape."""

from __future__ import annotations

import pytest

import fetchers
from fetchers.base import AbstractFetcher, PdfFetcher


def test_abstract_fetcher_requires_fetch_abstract() -> None:
    class IncompleteSource(AbstractFetcher):
        name = "incomplete"
    with pytest.raises(TypeError):
        IncompleteSource(http=None, config=None)       # fetch_abstract not implemented


def test_pdf_fetcher_requires_fetch_pdf() -> None:
    class IncompleteSource(PdfFetcher):
        name = "incomplete"
    with pytest.raises(TypeError):
        IncompleteSource(http=None, config=None)       # fetch_pdf not implemented


def test_source_may_implement_both_capabilities() -> None:
    class DualSource(AbstractFetcher, PdfFetcher):
        name = "dual"
        def fetch_abstract(self, doi, **kw) -> str | None:
            return f"abstract for {doi}"
        def fetch_pdf(self, doi, **kw):
            return None

    s = DualSource(http=None, config=None)
    assert s.fetch_abstract("10.1/x") == "abstract for 10.1/x"
    assert s.fetch_pdf("10.1/x", cache_dir="/tmp") is None
    assert isinstance(s, AbstractFetcher)
    assert isinstance(s, PdfFetcher)


def test_abstract_sources_registry_is_empty_before_step_4() -> None:
    """Registry is populated in step 4 of the refactor. Until then it's
    empty — orchestrators still call the legacy module-level functions."""
    assert fetchers.abstract_sources(http=None, config=None) == []


def test_pdf_sources_registry_is_empty_before_step_4() -> None:
    assert fetchers.pdf_sources(http=None, config=None) == []


def test_pdf_sources_accepts_names_filter() -> None:
    """API shape check: `names` filter exists and doesn't blow up on an
    empty registry."""
    assert fetchers.pdf_sources(http=None, config=None, names=["anything"]) == []


def test_source_has_name_and_interactive_class_attributes() -> None:
    """Every concrete source must set `name`. `interactive` defaults False."""
    class TinySource(PdfFetcher):
        name = "tiny"
        def fetch_pdf(self, doi, **kw): return None

    s = TinySource(http=None, config=None)
    assert s.name == "tiny"
    assert s.interactive is False


def test_source_can_be_marked_interactive() -> None:
    class Inter(PdfFetcher):
        name = "inter"
        interactive = True
        def fetch_pdf(self, doi, **kw): return None

    s = Inter(http=None, config=None)
    assert s.interactive is True
