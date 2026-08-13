"""Tests for the shared DOI helpers.

The per-module wrappers (`import_to_zotero._normalize_doi_key`,
`enrich_dois._normalise_doi`, `doi_resolver._normalize_doi_key`) keep their
own tests — those are the behaviour-preservation guarantee for the
extraction. This module covers `doi_utils` directly, with emphasis on the
strict/lenient split, since flattening the two into one function is the
mistake this design exists to prevent.
"""

from __future__ import annotations

import doi_utils
import pytest

# ---------------------------------------------------------------------------
# strip_doi_prefixes — lenient
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("https://doi.org/10.1/x", "10.1/x"),
        ("http://doi.org/10.1/x", "10.1/x"),
        ("https://dx.doi.org/10.1/x", "10.1/x"),
        ("http://dx.doi.org/10.1/x", "10.1/x"),
        ("doi:10.1/x", "10.1/x"),
        ("DOI:10.1/x", "10.1/x"),
        ("  10.1/x  ", "10.1/x"),
        ("10.1/x", "10.1/x"),
    ],
)
def test_strip_doi_prefixes_cleans(raw: str, clean: str) -> None:
    assert doi_utils.strip_doi_prefixes(raw)[0] == clean


@pytest.mark.parametrize(
    ("raw", "changed"),
    [
        ("10.1/x", False),
        ("", False),
        ("doi:10.1/x", True),
        ("  10.1/x", True),
        ("10.1/x  ", True),
        ("https://doi.org/10.1/x", True),
    ],
)
def test_strip_doi_prefixes_reports_change(raw: str, changed: bool) -> None:
    """`enrich_dois --fix-malformed` PATCHes Zotero only when this is True."""
    assert doi_utils.strip_doi_prefixes(raw)[1] is changed


def test_strip_doi_prefixes_keeps_invalid_input() -> None:
    """Lenient: garbage comes back cleaned, not discarded. The repair path
    needs something to write back."""
    assert doi_utils.strip_doi_prefixes("doi: not-a-doi") == ("not-a-doi", True)


def test_strip_doi_prefixes_removes_only_one_prefix() -> None:
    assert doi_utils.strip_doi_prefixes("doi:doi:10.1/x")[0] == "doi:10.1/x"


# ---------------------------------------------------------------------------
# normalize_doi — strict
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10.1234/abc", "10.1234/abc"),
        ("doi:10.1234/abc", "10.1234/abc"),
        ("DOI: 10.1234/abc", "10.1234/abc"),
        ("https://doi.org/10.1234/abc", "10.1234/abc"),
        ("https://dx.doi.org/10.1234/abc", "10.1234/abc"),
        ("  10.1234/abc  ", "10.1234/abc"),
        # Trailing punctuation picked up from prose / reference lists.
        ("10.1234/abc.", "10.1234/abc"),
        ("(doi:10.1234/abc)", None),  # leading paren is not stripped
        ("10.1234/abc,", "10.1234/abc"),
        ("10.1234/abc);", "10.1234/abc"),
    ],
)
def test_normalize_doi(raw: str, expected: str | None) -> None:
    assert doi_utils.normalize_doi(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "not-a-doi",
        "10.1/",            # empty suffix
        "10.123/abc",       # registrant prefix too short
        "11.1234/abc",      # wrong directory indicator
        "https://example.com/10.1234/abc",  # not a doi.org URL
    ],
)
def test_normalize_doi_rejects_non_dois(raw: str | None) -> None:
    assert doi_utils.normalize_doi(raw) is None


# ---------------------------------------------------------------------------
# The split itself — this is the part that must not be "simplified"
# ---------------------------------------------------------------------------


def test_doi_key_collapses_invalid_input_to_empty() -> None:
    """Identity key: an unparseable DOI must match nothing, including
    another unparseable DOI."""
    assert doi_utils.doi_key("garbage") == ""
    assert doi_utils.doi_key("") == ""
    assert doi_utils.doi_key(None) == ""


def test_doi_key_is_case_insensitive() -> None:
    assert doi_utils.doi_key("https://doi.org/10.1234/ABC") == doi_utils.doi_key(
        "10.1234/abc"
    )


def test_doi_cache_key_keeps_distinct_invalid_inputs_distinct() -> None:
    """Cache key: the strict form would map both of these to `""`, so two
    unrelated lookups would share one cache entry and serve each other's
    results. This is why `doi_resolver` uses the lenient helper."""
    a = doi_utils.doi_cache_key("weird-identifier-one")
    b = doi_utils.doi_cache_key("weird-identifier-two")
    assert a and b and a != b
    assert doi_utils.doi_key("weird-identifier-one") == doi_utils.doi_key(
        "weird-identifier-two"
    ) == ""


def test_doi_cache_key_normalizes_url_and_bare_forms_together() -> None:
    assert doi_utils.doi_cache_key(
        "https://doi.org/10.1234/ABC"
    ) == doi_utils.doi_cache_key("10.1234/abc")


def test_doi_cache_key_empty_input() -> None:
    assert doi_utils.doi_cache_key("") == ""


# ---------------------------------------------------------------------------
# No heavy imports — doi_resolver sits below the orchestrators and
# enrich_dois does not declare zotero-mcp-server in its PEP 723 block.
# ---------------------------------------------------------------------------


def test_doi_utils_is_stdlib_only() -> None:
    source = (
        __import__("pathlib").Path(doi_utils.__file__).read_text(encoding="utf-8")
    )
    for banned in ("import zotero_mcp", "import requests", "import httpx"):
        assert banned not in source, (
            f"doi_utils must stay stdlib-only; found {banned!r}."
        )
