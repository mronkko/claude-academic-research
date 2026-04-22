"""Browser-based PDF publisher registry.

Every publisher that needs a Playwright-driven browser session (usually
because Cloudflare blocks non-browser HTTP clients) has a handler class
in this sub-package.  ``all_handlers()`` returns one instance of each
leaf handler; ``resolve_by_doi(doi)`` picks the right handler for a
given DOI.

The driver in ``enrich_pdfs.py --sources browser`` iterates these
handlers and calls ``setup()`` once per publisher, then ``download()``
once per item.
"""

from __future__ import annotations

from pathlib import Path

from fetchers.base import PdfFetcher

from .aaa import AaaHandler
from .aom import AomHandler
from .apa import ApaHandler
from .base import (
    Counter,
    PageNavigationHandler,
    PublisherHandler,
    RequestHandler,
    cache_path_for,
    is_cached,
    launch_context,
    progress_tag,
)
from .connector import (
    ZoteroConnectorHandler,
    ping_zotero_desktop,
    resolve_connector_extension_path,
    wait_for_service_worker,
)
from .emerald import EmeraldHandler
from .informs import InformsHandler
from .oup import OupHandler
from .sage import SageHandler
from .tandf import TandfHandler
from .wiley import WileyHandler


class BrowserSource(PdfFetcher):
    """Marker fetcher that signals the browser-based flow to the main
    registry in `fetchers/__init__.py`.

    The `fetch_pdf(doi)` contract doesn't fit the browser flow (which
    is session-per-publisher, not per-DOI), so this class raises
    NotImplementedError if called directly.  The orchestrator checks
    `interactive=True` and routes around it — `enrich_pdfs.py --sources
    browser` instantiates the per-publisher handlers in this package
    rather than calling `fetch_pdf` on this class.
    """

    name = "browser"
    interactive = True

    def fetch_pdf(
        self, doi: str, *, cache_dir, bypass_prefix_filter: bool = False,
    ) -> tuple[Path, str] | None:
        del doi, cache_dir, bypass_prefix_filter
        raise NotImplementedError(
            "BrowserSource is interactive and session-per-publisher. "
            "Use enrich_pdfs.py --sources browser, which drives the "
            "per-publisher handlers in fetchers.browser directly."
        )


def all_handlers() -> list[PublisherHandler]:
    """Every registered handler, in a stable iteration order.

    The order determines which publisher gets processed first during a
    run — not critical, but keep alphabetical so the output is
    predictable across invocations. The three custom handlers
    (informs, oup, apa) are appended in step 3 of the refactor.
    """
    handlers: list[PublisherHandler] = [
        AaaHandler(),
        AomHandler(),
        ApaHandler(),
        EmeraldHandler(),
        InformsHandler(),
        OupHandler(),
        SageHandler(),
        TandfHandler(),
        WileyHandler(),
    ]
    return handlers


def resolve_by_doi(
    doi: str,
    handlers: list[PublisherHandler] | None = None,
) -> PublisherHandler | None:
    """First handler whose ``doi_prefixes`` matches ``doi``.

    Returns None if no handler claims the DOI — the caller should skip
    (or fall back to another source). ``handlers`` is injectable for
    tests; production code uses the default registry.
    """
    for h in handlers if handlers is not None else all_handlers():
        if h.matches_doi(doi):
            return h
    return None


def resolve_by_host(
    host: str,
    handlers: list[PublisherHandler] | None = None,
) -> PublisherHandler | None:
    """First handler whose ``direct_access_domains`` matches ``host``.

    Used by the browser pipeline's Pass 1 classification: the DOI is
    first resolved to its Crossref-registered URL, and the URL's host
    drives handler selection. Catches the case where a DOI's prefix
    is misleading (journal migrated publishers).

    Matching is suffix-based via the shared library_resolver helper,
    so ``onlinelibrary.wiley.com`` matches a handler whose domains
    include ``wiley.com``.
    """
    if not host:
        return None
    from fetchers.library_resolver import _target_matches_domains
    for h in handlers if handlers is not None else all_handlers():
        if not h.direct_access_domains:
            continue
        if _target_matches_domains(f"https://{host}/", h.direct_access_domains):
            return h
    return None


__all__ = [
    "AaaHandler",
    "AomHandler",
    "ApaHandler",
    "BrowserSource",
    "Counter",
    "EmeraldHandler",
    "InformsHandler",
    "OupHandler",
    "PageNavigationHandler",
    "PublisherHandler",
    "RequestHandler",
    "SageHandler",
    "TandfHandler",
    "WileyHandler",
    "ZoteroConnectorHandler",
    "all_handlers",
    "cache_path_for",
    "is_cached",
    "launch_context",
    "ping_zotero_desktop",
    "progress_tag",
    "resolve_by_doi",
    "resolve_by_host",
    "resolve_connector_extension_path",
    "wait_for_service_worker",
]
