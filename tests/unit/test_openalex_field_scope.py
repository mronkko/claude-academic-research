"""Which fields the search actually searches, chosen rather than inherited.

OpenAlex's `search=` parameter covers full text, not just title and
abstract. Scopus `TITLE-ABS-KEY` and WoS `TS=` cover title, abstract and
keywords only. So a two-database run has always searched different fields
in each database, and nothing said so — not the config, not the metadata,
not the skill.

That is not a footnote. Measured across six management journals, 2011+:
"three-way interaction" appears in the title or abstract of 17 articles
and in the full text of 113. A review whose target papers are identified
by a method phrase therefore has a recall ceiling near 15% on the
abstract-limited databases, and no term list gets past it. One review
measured 16.2% against its own ground truth.

The asymmetry is not a bug to remove — full-text reach is a real
advantage of OpenAlex, and the option here defaults to keeping it. What
was wrong is that it was invisible: PRISMA requires reporting the fields
searched, and a protocol cannot report what it was never told.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from searchers import OpenAlexSearch
from searchers.base import SearchContext


class _Fake(OpenAlexSearch):
    def __init__(self):
        super().__init__()
        self.params: list[dict] = []

    def _fetch_page(self, query, filter_str, page, ctx):
        self.params.append({"query": query, "filter": filter_str})
        return {"results": [], "meta": {"count": 0}}


def _ctx(**kw) -> SearchContext:
    return SearchContext(
        from_year=2011, to_year=2026, issns=["0021-9010"],
        search_fields=kw.get("search_fields", "all"),
    )


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(BLOCK_A_TERMS=["three-way interaction"],
                           BLOCK_B_TERMS=[])


def test_the_default_searches_full_text() -> None:
    """OpenAlex's reach is a reason to use it; the default keeps it."""
    src = _Fake()
    src.run(_cfg(), _ctx())
    assert "title_and_abstract.search" not in src.params[0]["filter"]


def test_title_abstract_scope_moves_the_term_into_a_filter() -> None:
    """OpenAlex expresses field-limited search as a filter, not as
    `search=`, so the term has to move out of the query parameter."""
    src = _Fake()
    src.run(_cfg(), _ctx(search_fields="title_abstract"))
    assert "title_and_abstract.search:" in src.params[0]["filter"]
    assert src.params[0]["query"] == ""


def test_title_abstract_scope_keeps_the_other_filters() -> None:
    src = _Fake()
    src.run(_cfg(), _ctx(search_fields="title_abstract"))
    filt = src.params[0]["filter"]
    assert "publication_year:2011-2026" in filt
    assert "issn:0021-9010" in filt


def test_the_context_defaults_to_all_fields() -> None:
    """Backward compatible with every construction that predates this."""
    ctx = SearchContext(from_year=2011, to_year=2026, issns=[])
    assert ctx.search_fields == "all"


def test_an_unknown_field_scope_is_rejected() -> None:
    """A typo must not silently fall back to a different search."""
    import search as search_mod
    with pytest.raises(SystemExit):
        search_mod._validate_search_fields("titles")


def test_the_valid_scopes_are_accepted() -> None:
    import search as search_mod
    assert search_mod._validate_search_fields("all") == "all"
    assert search_mod._validate_search_fields("title_abstract") == "title_abstract"


def test_the_flag_appears_in_help() -> None:
    import subprocess
    import sys
    from pathlib import Path
    repo = Path(__file__).resolve().parents[2]
    out = subprocess.run(
        [sys.executable, str(repo / "scripts" / "pipelines" / "search.py"),
         "--help"], capture_output=True, text=True, check=True,
    ).stdout
    assert "--search-fields" in out
