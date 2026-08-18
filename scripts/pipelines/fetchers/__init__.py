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
from .openalex import OpenAlexContentSource, OpenAlexSource
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

    The order is a descent through versions of the same paper, and it is
    ranked by **version quality first, cost second**. Full retrieval
    sequence, of which this function covers stages 1–3:

        Stage 1 — free version of record (institutional subscription or
                  free API; the published article, correctly paginated)
            ScienceDirect (Elsevier) → Springer → Wiley TDM
            → Crossref TDM → PMC
        Stage 2 — paid version of record
            OpenAlex Content API ($0.01/PDF, opt-in)
        Stage 3 — open access, often the author's accepted manuscript
            OpenAlex OA tier → Unpaywall → Semantic Scholar → CORE
            → [preprint, only with `allow_preprints`]
        Stage 4 — browser handlers for Cloudflare/SSO-gated publishers
                  (APA, Sage, AOM, T&F, OUP, Emerald, INFORMS, …)
        Stage 5 — Zotero Connector via the institutional link resolver

    Stages 4–5 are separate passes, not part of this list: they open a
    real browser and may need a human to solve a challenge. Select them
    with `names=["browser"]` / `names=["connector"]`, or run both
    automated and browser passes in one go with `enrich_pdfs.py --all`.

    Stage 2 sits *above* the free stage-3 aggregators on purpose. A paid
    Content API download is the publisher's own file, so it carries real
    page numbers; the OA aggregators frequently return an author
    accepted manuscript whose pagination does not match the published
    article. Where the downstream job is quoting text and citing pages,
    a correct version of record is worth $0.01 more than a free
    manuscript. It is the only per-item cost in the cascade — every
    other source here is free or already covered by an institutional
    subscription — and it is skippable via
    `[openalex] use_paid_content_api = false`.

    CORE sits last among the OA sources deliberately: it indexes
    institutional repositories, so what it returns is usually the
    accepted manuscript rather than the published article — right for
    screening and coding, wrong for page numbers, and therefore only
    worth taking when nothing else answered. Attachments from it carry
    `pdf:repository-copy` so that distinction survives.

    `preprint` is last of all and, unlike every other source here, off
    unless asked for. What it returns is the manuscript *before* peer
    review, which is a different paper in every way that a systematic
    review cares about — see `fetchers/preprint.py` for why the opt-in
    and the `pdf:preprint-version` tag are both load-bearing.

    Browser is in the registry but excluded from the default selection —
    it runs interactively, opening a real window a human may have to
    click. Use `names=["browser"]` to select it explicitly.

    **Wiley used to be excluded here too, and should not have been.**
    The stated reason was that it "requires a specific auth contract",
    but `WileySource.fetch_pdf` already returns None on a non-Wiley DOI
    prefix, on a missing token, on a missing `wiley_tdm` import, and on
    any exception — it self-disables exactly as safely as
    ScienceDirect, which is token-gated too and was never excluded. The
    cost of the asymmetry was silent: `--all` builds its cascade from
    this function, so the documented one-shot "run everything" path
    skipped Wiley entirely, and only a reader who found `--sources
    wiley` in a table deep in the skill would ever run it. Measured on a
    live 1,895-item library pass: 248 Wiley-prefix items, of which the
    cascade found 39; a separate `--sources wiley` pass then recovered
    **47 more** that no default invocation would ever have asked for.
    """
    if http is None:
        return []
    all_sources = cast("list[PdfFetcher]", [
        ScienceDirectSource(http, config),
        SpringerSource(http, config),
        WileySource(http, config),
        CrossrefSource(http, config),
        PmcSource(http, config),
        # Stage 2 — the cascade's only per-item cost, ranked here rather
        # than last because what it returns is the version of record.
        OpenAlexContentSource(http, config),
        OpenAlexSource(http, config),
        UnpaywallSource(http, config),
        SemanticScholarSource(http, config),
        CoreSource(http, config),
        PreprintSource(http, config),
        BrowserSource(http, config),
    ])
    if names:
        name_set = set(names)
        return [s for s in all_sources if s.name in name_set]
    return [
        s for s in all_sources
        if not s.interactive
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
    "OpenAlexContentSource",
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
