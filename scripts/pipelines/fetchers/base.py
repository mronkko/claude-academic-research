"""Abstract base classes for abstract-fetching and PDF-fetching sources.

A source is a class that knows how to fetch data for a given DOI from
one provider. A source may advertise one or both capabilities:

    - AbstractFetcher: exposes `fetch_abstract(doi, ...)`
    - PdfFetcher:      exposes `fetch_pdf(doi, ...)`

Crossref, OpenAlex, and ScienceDirect each provide both — a single
class inherits from both ABCs. PMC and Wiley only serve PDFs; Scopus
only serves abstracts.

The orchestrator iterates priority-ordered source lists from
`sources.__init__` until one returns a result.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import requests


class Source(ABC):  # noqa: B024  # marker base; abstractmethods live on AbstractFetcher / PdfFetcher
    """Root base class. Subclasses MUST set `name` as a class attribute.

    `interactive = True` signals to the orchestrator that this source
    cannot run alongside others in a thread pool — it needs exclusive
    stdin/stdout (Playwright browser) or holds a stateful session that
    must serialise across items.
    """

    name: str = ""
    interactive: bool = False

    def __init__(
        self,
        http: requests.Session | None = None,
        config: Any = None,
    ) -> None:
        self.http = http
        self.config = config


class AbstractFetcher(Source, ABC):
    """A source that can fetch an abstract string for a DOI."""

    @abstractmethod
    def fetch_abstract(
        self,
        doi: str,
        *,
        title: str | None = None,
        cache_dir: str | Path | None = None,
    ) -> str | None:
        """Return the abstract text, or None if the source has nothing."""


class PdfFetcher(Source, ABC):
    """A source that can fetch a PDF and write it to `cache_dir`.

    Subclasses that prefix-filter (Wiley, ScienceDirect, Springer)
    may declare `direct_access_domains` so the Pass 2 routing layer
    in the browser pipeline can decide whether to invoke this source
    for a DOI whose prefix doesn't match but whose Crossref-resolved
    URL lives on one of this publisher's hosts. In that case the
    driver passes `bypass_prefix_filter=True`.
    """

    # Hostnames (suffix-match) that identify PDFs this source can
    # retrieve. Empty tuple = the source handles any DOI (no
    # prefix filtering) and is excluded from Pass 2 API retry.
    direct_access_domains: tuple[str, ...] = ()

    @abstractmethod
    def fetch_pdf(
        self,
        doi: str,
        *,
        cache_dir: str | Path,
        bypass_prefix_filter: bool = False,
    ) -> tuple[Path, str] | None:
        """Return (pdf_path_on_disk, source_url) or None.

        The path must be inside `cache_dir`. Returning a path (not
        bytes) lets the orchestrator hand the file straight to
        `ZoteroClient.attach_pdf`, which pyzotero expects as a path.

        `bypass_prefix_filter=True` tells sources that prefix-filter
        (Wiley, Elsevier, Springer) to attempt the download even when
        the DOI's prefix doesn't match their own list — used by Pass 2
        when Crossref resolution reveals a migrated journal. Sources
        that don't prefix-filter ignore the flag.
        """
