"""Tests for scripts/pipelines/fetchers/doi_resolver.py.

Exercises the Crossref lookup and the on-disk cache. `Crossref` is
always mocked — no real `api.crossref.org` traffic in the unit suite.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from fetchers.doi_resolver import (
    DoiResolution,
    DoiResolverCache,
    _extract_resolution,
    resolve_doi,
)

# ---------------------------------------------------------------------------
# _extract_resolution — pure function
# ---------------------------------------------------------------------------


def test_extract_resolution_prefers_primary_url_over_top_level_url() -> None:
    """Crossref stores the canonical URL in `resource.primary.URL`
    when it's more specific than the top-level `URL`. Prefer primary."""
    msg = {
        "URL": "https://doi.org/10.1111/etap.12254",
        "resource": {
            "primary": {"URL": "https://journals.sagepub.com/doi/10.1111/etap.12254"},
        },
        "publisher": "SAGE Publications",
        "ISSN": ["1042-2587", "1540-6520"],
    }
    r = _extract_resolution(msg)
    assert r.url == "https://journals.sagepub.com/doi/10.1111/etap.12254"
    assert r.publisher == "SAGE Publications"
    assert r.issn == "1042-2587"       # first ISSN wins


def test_extract_resolution_falls_back_to_top_level_url() -> None:
    msg = {
        "URL": "https://onlinelibrary.wiley.com/doi/10.1002/x",
        "publisher": "Wiley",
    }
    r = _extract_resolution(msg)
    assert r.url == "https://onlinelibrary.wiley.com/doi/10.1002/x"
    assert r.publisher == "Wiley"
    assert r.issn == ""


def test_extract_resolution_handles_missing_fields() -> None:
    r = _extract_resolution({})
    assert r == DoiResolution(
        url="", publisher="", issn="",
        title="", author_surnames=[], issued_year="",
    )


# ---------------------------------------------------------------------------
# _extract_resolution — validation fields (title / authors / year)
# ---------------------------------------------------------------------------


def test_extract_resolution_populates_validation_fields() -> None:
    """Full Crossref payload yields title + author surnames + year."""
    msg = {
        "URL": "https://example/x",
        "title": ["Putting Framing in Perspective: A Review"],
        "author": [
            {"family": "Cornelissen", "given": "Joep P."},
            {"family": "Werner", "given": "Mirjam D."},
        ],
        "issued": {"date-parts": [[2014, 6, 15]]},
    }
    r = _extract_resolution(msg)
    assert r.title == "Putting Framing in Perspective: A Review"
    assert r.author_surnames == ["Cornelissen", "Werner"]
    assert r.issued_year == "2014"


def test_extract_resolution_handles_missing_author_family() -> None:
    """Corporate / malformed author records (no `family` key) are
    dropped — we preserve only surnames we can compare against."""
    msg = {
        "author": [
            {"name": "Some Organisation"},         # no family
            {"family": "Smith", "given": "J."},
            {"given": "Anonymous"},                # no family
        ],
    }
    r = _extract_resolution(msg)
    assert r.author_surnames == ["Smith"]


def test_extract_resolution_handles_year_only_issued() -> None:
    """Some Crossref records have year-only date-parts
    (`[[2000]]`) — still yields the year."""
    r = _extract_resolution({"issued": {"date-parts": [[2000]]}})
    assert r.issued_year == "2000"


def test_extract_resolution_handles_missing_issued_date() -> None:
    """`issued.date-parts` can be missing or malformed — no crash."""
    assert _extract_resolution({}).issued_year == ""
    assert _extract_resolution(
        {"issued": {"date-parts": []}},
    ).issued_year == ""
    assert _extract_resolution(
        {"issued": {"date-parts": [[None]]}},
    ).issued_year == ""


def test_extract_resolution_handles_empty_title_list() -> None:
    """Crossref `title` is always a list but may be empty for some
    stub records."""
    assert _extract_resolution({"title": []}).title == ""


def test_cache_round_trips_validation_fields(tmp_path) -> None:
    """The new fields survive a put→get cycle on disk."""
    c = DoiResolverCache(tmp_path)
    c.put("10.1/x", DoiResolution(
        url="https://example/x",
        title="Some Title",
        author_surnames=["Smith", "Jones"],
        issued_year="2014",
    ))
    reloaded = DoiResolverCache(tmp_path).get("10.1/x")
    assert reloaded is not None
    assert reloaded.title == "Some Title"
    assert reloaded.author_surnames == ["Smith", "Jones"]
    assert reloaded.issued_year == "2014"


def test_cache_legacy_entry_without_validation_fields_loads(tmp_path: Path) -> None:
    """v0.4.0 cache entries only had url/publisher/issn. Loading them
    into the v0.5.0 DoiResolution must not crash; new fields default
    to empty so validation-callers treat the entry as 'no title to
    compare'."""
    import json
    legacy = tmp_path / "doi_resolver_cache.json"
    legacy.write_text(json.dumps({
        "10.1/x": {
            "url": "https://old",
            "publisher": "Old",
            "issn": "1234-5678",
        }
    }))
    got = DoiResolverCache(tmp_path).get("10.1/x")
    assert got is not None
    assert got.url == "https://old"
    assert got.title == ""
    assert got.author_surnames == []
    assert got.issued_year == ""


# ---------------------------------------------------------------------------
# resolve_doi — integration with a mocked Crossref client
# ---------------------------------------------------------------------------


def _crossref(message: dict, *, status: str = "ok") -> MagicMock:
    cr = MagicMock()
    cr.works.return_value = {"status": status, "message": message}
    return cr


def test_resolve_doi_returns_resolution_on_ok_response() -> None:
    cr = _crossref({
        "URL": "https://journals.sagepub.com/doi/10.1111/etap.12254",
        "publisher": "SAGE Publications",
        "ISSN": ["1042-2587"],
    })
    r = resolve_doi("10.1111/etap.12254", crossref=cr)
    assert r is not None
    assert r.url.startswith("https://journals.sagepub.com")
    assert r.publisher == "SAGE Publications"


def test_resolve_doi_returns_none_on_empty_doi() -> None:
    cr = MagicMock()
    cr.works.side_effect = AssertionError("must not be called")
    assert resolve_doi("", crossref=cr) is None
    assert resolve_doi("   ", crossref=cr) is None


def test_resolve_doi_returns_none_when_crossref_raises() -> None:
    """Network / API errors never propagate — routing falls back to
    prefix-matching instead of crashing the whole run."""
    cr = MagicMock()
    cr.works.side_effect = RuntimeError("api.crossref.org down")
    assert resolve_doi("10.1/x", crossref=cr) is None


def test_resolve_doi_returns_none_when_status_not_ok() -> None:
    cr = _crossref({}, status="error")
    assert resolve_doi("10.1/x", crossref=cr) is None


def test_resolve_doi_returns_resolution_even_when_url_is_missing() -> None:
    """Crossref has the DOI but no URL metadata → we still return the
    resolution because validation (v0.5.0) cares about title even
    when URL is absent. Callers that need URL (pdf routing) check
    `resolution.url` at the callsite."""
    cr = _crossref({
        "publisher": "Some Publisher",
        "title": ["A Paper Without URL"],
    })
    result = resolve_doi("10.1/x", crossref=cr)
    assert result is not None
    assert result.url == ""
    assert result.publisher == "Some Publisher"
    assert result.title == "A Paper Without URL"


# ---------------------------------------------------------------------------
# DoiResolverCache — round-trip on disk
# ---------------------------------------------------------------------------


def test_cache_round_trips_a_resolution(tmp_path: Path) -> None:
    c = DoiResolverCache(tmp_path)
    c.put("10.1/x", DoiResolution(
        url="https://sagepub.com/x", publisher="Sage", issn="1234-5678",
    ))
    # Reload from disk via a fresh instance.
    c2 = DoiResolverCache(tmp_path)
    got = c2.get("10.1/x")
    assert got is not None
    assert got.url == "https://sagepub.com/x"
    assert got.publisher == "Sage"
    assert got.issn == "1234-5678"


def test_cache_get_returns_none_for_unknown_doi(tmp_path: Path) -> None:
    c = DoiResolverCache(tmp_path)
    assert c.get("10.1/never-added") is None


def test_cache_recovers_from_corrupt_json(tmp_path: Path) -> None:
    (tmp_path / "doi_resolver_cache.json").write_text("{not valid json")
    c = DoiResolverCache(tmp_path)
    assert c.get("10.1/x") is None
    c.put("10.1/x", DoiResolution(url="https://a/"))
    assert DoiResolverCache(tmp_path).get("10.1/x") is not None


# ---------------------------------------------------------------------------
# resolve_doi + cache — second call reads from disk, no network
# ---------------------------------------------------------------------------


def test_resolve_doi_uses_cache_on_second_call(tmp_path: Path) -> None:
    cache = DoiResolverCache(tmp_path)
    cr1 = _crossref({
        "URL": "https://journals.sagepub.com/doi/10.1111/etap.12254",
    })
    first = resolve_doi("10.1111/etap.12254", crossref=cr1, cache=cache)
    assert first is not None

    # Second call — Crossref must not be hit.
    cr2 = MagicMock()
    cr2.works.side_effect = AssertionError("must not re-query Crossref")
    second = resolve_doi("10.1111/etap.12254", crossref=cr2, cache=cache)
    assert second is not None
    assert second.url == first.url


def test_resolve_doi_normalises_doi_to_lowercase_for_cache(tmp_path: Path) -> None:
    """DOIs are case-insensitive; cache key uses the lowercased form so
    `10.1111/ETAP.12254` and `10.1111/etap.12254` share one entry."""
    cache = DoiResolverCache(tmp_path)
    cr = _crossref({"URL": "https://journals.sagepub.com/x"})
    resolve_doi("10.1111/ETAP.12254", crossref=cr, cache=cache)

    cr2 = MagicMock()
    cr2.works.side_effect = AssertionError("must not re-query Crossref")
    assert resolve_doi(
        "10.1111/etap.12254", crossref=cr2, cache=cache,
    ) is not None
