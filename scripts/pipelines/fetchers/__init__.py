"""Fetcher registry.

`abstract_sources()` and `pdf_sources()` return priority-ordered lists
of instantiated fetcher classes. The orchestrators (enrich_abstracts,
enrich_pdfs) iterate these lists until one fetcher returns a result.

Named `fetchers` rather than `sources` to avoid a collision with the
existing `scripts/sources/` package (predatory-journal data).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from .base import AbstractFetcher, PdfFetcher, Source
from .browser import BrowserSource
from .core import (
    REPOSITORY_COPY_TAG,
    CoreSource,
    is_repository_copy_path,
)
from .crossref import CrossrefSource
from .openalex import OpenAlexSource
from .pmc import PmcSource
from .preprint import PREPRINT_VERSION_TAG, PreprintSource, is_preprint_path
from .sciencedirect import TDM_RECOVERED_TAG, ScienceDirectSource, is_tdm_recovered_path
from .scopus import ScopusSource
from .semantic_scholar import SemanticScholarSource
from .springer import SpringerSource
from .unpaywall import UnpaywallSource
from .wiley import WileySource
from .wos import WosSource

if TYPE_CHECKING:
    import requests


def abstract_sources(
    http: requests.Session | None = None,
    config: Any = None,
) -> list[AbstractFetcher]:
    """Priority-ordered abstract sources.

    Cascade order:
        Crossref → Semantic Scholar → Scopus → WoS → ScienceDirect
        → OpenAlex GROBID
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
    http: requests.Session | None = None,
    config: Any = None,
    names: list[str] | None = None,
    *,
    allow_preprints: bool = False,
) -> list[PdfFetcher]:
    """Priority-ordered PDF sources.

    Default cascade order:
        ScienceDirect (Elsevier) → Springer → Crossref TDM → PMC
        → OpenAlex (Content + OA) → Unpaywall → Semantic Scholar → CORE
        → [preprint, only with `allow_preprints`]

    The order is a descent through versions of the same paper. Publisher-
    direct sources come first because they serve the version of record.
    Aggregators follow, widest-net last. CORE sits after them
    deliberately: it indexes institutional repositories, so what it
    returns is usually the accepted manuscript rather than the published
    article — right for screening and coding, wrong for page numbers, and
    therefore only worth taking when nothing else answered. Attachments
    from it carry `pdf:repository-copy` so that distinction survives.

    `preprint` is last of all and, unlike every other source here, off
    unless asked for. What it returns is the manuscript *before* peer
    review, which is a different paper in every way that a systematic
    review cares about — see `fetchers/preprint.py` for why the opt-in
    and the `pdf:preprint-version` tag are both load-bearing.

    Wiley and Browser are in the registry but excluded from the default
    selection too — they require a specific auth contract (Wiley) or run
    interactively (Browser). Use `names=["wiley"]` or `names=["browser"]`
    to select them explicitly.
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
        SemanticScholarSource(http, config),
        CoreSource(http, config),
        PreprintSource(http, config),
        WileySource(http, config),
        BrowserSource(http, config),
    ])
    if names:
        name_set = set(names)
        return [s for s in all_sources if s.name in name_set]
    return [
        s for s in all_sources
        if not s.interactive
        and s.name != "wiley"
        and (s.name != "preprint" or allow_preprints)
    ]


__all__ = [
    "AbstractFetcher",
    "PdfFetcher",
    "Source",
    "PREPRINT_VERSION_TAG",
    "REPOSITORY_COPY_TAG",
    "TDM_RECOVERED_TAG",
    "abstract_sources",
    "pdf_sources",
    "is_preprint_path",
    "is_repository_copy_path",
    "is_tdm_recovered_path",
    "BrowserSource",
    "CoreSource",
    "CrossrefSource",
    "OpenAlexSource",
    "PmcSource",
    "PreprintSource",
    "ScienceDirectSource",
    "ScopusSource",
    "SemanticScholarSource",
    "SpringerSource",
    "UnpaywallSource",
    "WileySource",
    "WosSource",
]
