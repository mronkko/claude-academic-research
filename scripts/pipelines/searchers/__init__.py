"""Bibliographic search sources registry.

Exposes a base `SearchSource` ABC and four concrete implementations:
Scopus, Web of Science Expanded, OpenAlex, Semantic Scholar. Driven
by the orchestrator `search.py`; each source can also be run
standalone via the `search_<name>.py` single-DB wrappers.

Add a new database by subclassing `SearchSource`, implementing
`run(config, ctx)`, and adding the class to `ALL_SOURCE_CLASSES`.
"""

from __future__ import annotations

from .base import SEARCH_ROW_FIELDS, SearchContext, SearchSource, empty_row
from .openalex import OpenAlexSearch
from .scopus import ScopusSearch
from .semantic_scholar import SemanticScholarSearch
from .wos import WosSearch

# Class tuple — instantiate once in `searchers_by_name()`.
ALL_SOURCE_CLASSES: tuple[type[SearchSource], ...] = (
    ScopusSearch, WosSearch, OpenAlexSearch, SemanticScholarSearch,
)


def searchers_by_name() -> dict[str, SearchSource]:
    """Return a fresh dict mapping each source's `name` to an instance."""
    return {cls.name: cls() for cls in ALL_SOURCE_CLASSES}


__all__ = (
    "SEARCH_ROW_FIELDS",
    "SearchContext",
    "SearchSource",
    "empty_row",
    "OpenAlexSearch",
    "ScopusSearch",
    "SemanticScholarSearch",
    "WosSearch",
    "ALL_SOURCE_CLASSES",
    "searchers_by_name",
)
