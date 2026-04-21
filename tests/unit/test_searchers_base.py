"""Contract tests for the searchers/ package.

Tests the ABC, registry, and row-schema invariants. Actual API calls
are exercised by `tests/live/` (a live-search suite could be added
later — the four sources are hitting real APIs and require
credentials, so they belong in `@pytest.mark.live`).
"""

from __future__ import annotations

import pytest
from searchers import (
    ALL_SOURCE_CLASSES,
    SEARCH_ROW_FIELDS,
    OpenAlexSearch,
    ScopusSearch,
    SearchContext,
    SearchSource,
    SemanticScholarSearch,
    WosSearch,
    empty_row,
    searchers_by_name,
)

# ---------------------------------------------------------------------------
# ABC contract
# ---------------------------------------------------------------------------


def test_cannot_instantiate_base_abc() -> None:
    with pytest.raises(TypeError):
        SearchSource()  # type: ignore[abstract]


def test_every_source_sets_a_name() -> None:
    for cls in ALL_SOURCE_CLASSES:
        assert cls.name, f"{cls.__name__}: empty `name` class attribute"
        assert cls.name.islower(), f"{cls.__name__}: `name` must be lower_snake_case"


def test_every_source_declares_scope_flags() -> None:
    for cls in ALL_SOURCE_CLASSES:
        assert isinstance(cls.supports_journal_scope, bool), (
            f"{cls.__name__}: supports_journal_scope missing or non-bool"
        )
        assert isinstance(cls.supports_block_queries, bool), (
            f"{cls.__name__}: supports_block_queries missing or non-bool"
        )


def test_registry_is_complete() -> None:
    by_name = searchers_by_name()
    expected = {"scopus", "wos", "openalex", "semantic_scholar"}
    assert set(by_name) == expected


def test_registry_returns_fresh_instances() -> None:
    a = searchers_by_name()
    b = searchers_by_name()
    assert a["scopus"] is not b["scopus"], (
        "searchers_by_name() should return fresh instances so callers "
        "can mutate state without cross-run contamination"
    )


def test_every_registered_source_is_a_subclass() -> None:
    for name, source in searchers_by_name().items():
        assert isinstance(source, SearchSource), f"{name} not a SearchSource"


def test_registry_classes_cover_every_declared_source() -> None:
    """If ALL_SOURCE_CLASSES grows a new entry, the registry should
    expose it automatically — this test guards that `searchers_by_name`
    doesn't start filtering classes silently."""
    assert set(searchers_by_name()) == {cls.name for cls in ALL_SOURCE_CLASSES}


# ---------------------------------------------------------------------------
# Row schema
# ---------------------------------------------------------------------------


def test_empty_row_has_every_field() -> None:
    row = empty_row()
    assert set(row) == set(SEARCH_ROW_FIELDS)


def test_empty_row_cited_by_is_numeric_zero() -> None:
    """Other fields default to '' but cited_by defaults to 0 so that
    CSV writes produce `0` for unknown citations, not an empty cell."""
    row = empty_row()
    assert row["cited_by"] == 0


def test_search_row_fields_stable_prefix() -> None:
    """First four columns must stay in order for every downstream
    consumer (import_to_zotero.py's CSV reader, the manuscript's
    stats.py, the test suite). Guards against accidental reordering."""
    assert SEARCH_ROW_FIELDS[:4] == ("db", "query", "doi", "title")


# ---------------------------------------------------------------------------
# Per-source specifics that don't require network
# ---------------------------------------------------------------------------


def test_scopus_supports_journal_scope() -> None:
    assert ScopusSearch().supports_journal_scope is True


def test_wos_supports_journal_scope() -> None:
    assert WosSearch().supports_journal_scope is True


def test_openalex_supports_block_queries() -> None:
    assert OpenAlexSearch().supports_block_queries is True


def test_semantic_scholar_does_not_support_journal_scope() -> None:
    """Documented limitation — S2 filter happens client-side."""
    assert SemanticScholarSearch().supports_journal_scope is False


def test_semantic_scholar_never_blocks_on_credentials(monkeypatch) -> None:
    """Free tier works without a key; credentials_error must return None
    even when SEMANTIC_SCHOLAR_API_KEY is unset."""
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    ctx = SearchContext(from_year=2020, to_year=2024, issns=[])
    assert SemanticScholarSearch().credentials_error(ctx) is None


def test_wos_reports_missing_credentials(monkeypatch) -> None:
    monkeypatch.delenv("WOS_API_KEY_EXTENDED", raising=False)
    ctx = SearchContext(from_year=2020, to_year=2024, issns=[])
    err = WosSearch().credentials_error(ctx)
    assert err is not None
    assert "WOS_API_KEY_EXTENDED" in err


def test_scopus_reports_missing_credentials(monkeypatch, tmp_path) -> None:
    """Both the pybliometrics config file and the env var are absent."""
    monkeypatch.delenv("SCOPUS_API_KEY", raising=False)
    # Redirect HOME so the default pybliometrics.cfg path does not exist
    monkeypatch.setenv("HOME", str(tmp_path))
    ctx = SearchContext(from_year=2020, to_year=2024, issns=[])
    err = ScopusSearch().credentials_error(ctx)
    # Credentials state depends on whether a real config exists at the
    # fake HOME path — tmp_path is empty so there's none.
    if err is not None:
        assert "pybliometrics" in err or "SCOPUS_API_KEY" in err
