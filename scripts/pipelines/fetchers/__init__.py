"""Fetcher registry.

`abstract_sources()` and `pdf_sources()` return priority-ordered lists
of instantiated fetcher classes. The orchestrators (enrich_abstracts,
enrich_pdfs) iterate these lists until one fetcher returns a result.

Named `fetchers` rather than `sources` to avoid a collision with the
existing `scripts/sources/` package (predatory-journal data).
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING, cast

from .base import AbstractFetcher, PdfFetcher, Source
from .browser import BrowserSource
from .crossref import CrossrefSource
from .openalex import OpenAlexSource
from .pmc import PmcSource
from .sciencedirect import ScienceDirectSource
from .scopus import ScopusSource
from .semantic_scholar import SemanticScholarSource
from .springer import SpringerSource
from .unpaywall import UnpaywallSource
from .wiley import WileySource
from .wos import WosSource

if TYPE_CHECKING:
    import requests


def abstract_sources(
    http: "requests.Session | None" = None,
    config: Any = None,
) -> list[AbstractFetcher]:
    """Priority-ordered abstract sources.

    Order matches the cascade in fetch_abstracts.py:12-18:
        Crossref → Semantic Scholar → Scopus → ScienceDirect → OpenAlex GROBID
    """
    if http is None:
        return []
    return cast("list[AbstractFetcher]", [
        CrossrefSource(http, config),
        SemanticScholarSource(http, config),
        ScopusSource(http, config),
        WosSource(http, config),
        ScienceDirectSource(http, config),
        OpenAlexSource(http, config),
    ])


def pdf_sources(
    http: "requests.Session | None" = None,
    config: Any = None,
    names: list[str] | None = None,
) -> list[PdfFetcher]:
    """Priority-ordered PDF sources.

    Default order matches the cascade in attach_pdfs.py:13-19:
        ScienceDirect (Elsevier) → Springer → Crossref TDM → PMC
        → OpenAlex (Content + OA) → Unpaywall

    Wiley and Browser are included in the registry but excluded by the
    default selection — they require a specific auth contract (Wiley)
    or run interactively (Browser). Use `names=["wiley"]` or
    `names=["browser"]` to select them explicitly.
    """
    if http is None:
        return []
    all_sources = cast("list[PdfFetcher]", [
        ScienceDirectSource(http, config),
        SpringerSource(http, config),
        CrossrefSource(http, config),
        PmcSource(http, config),
        OpenAlexSource(http, config),
        UnpaywallSource(http, config),
        WileySource(http, config),
        BrowserSource(http, config),
    ])
    if names:
        name_set = set(names)
        return [s for s in all_sources if s.name in name_set]
    return [
        s for s in all_sources
        if not s.interactive and s.name != "wiley"
    ]


__all__ = [
    "AbstractFetcher",
    "PdfFetcher",
    "Source",
    "abstract_sources",
    "pdf_sources",
    "BrowserSource",
    "CrossrefSource",
    "OpenAlexSource",
    "PmcSource",
    "ScienceDirectSource",
    "ScopusSource",
    "SemanticScholarSource",
    "SpringerSource",
    "UnpaywallSource",
    "WileySource",
    "WosSource",
]
