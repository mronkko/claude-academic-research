"""One DOI → cache-filename rule, safe on Windows.

Every fetcher replaced only `/` and `:`, so a SICI DOI kept its `<>` —
illegal on NTFS, so the write raised and the item was filed as a paywall
miss — and a `\\` in a crafted DOI is a path separator there.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fetchers import cache_names
from fetchers.browser.base import _diagnostic_stem, cache_path_for

SICI = "10.1002/(sici)1097-0266(199701)18:1<1::aid-smj894>3.0.co;2-d"
_WINDOWS_ILLEGAL = set('<>:"/\\|?*')


def test_ordinary_dois_keep_their_historical_names() -> None:
    """Caches written by earlier versions must still be found."""
    assert cache_names.doi_safe("10.1287/orsc.11.4.367.14601") == (
        "10.1287_orsc.11.4.367.14601")
    assert cache_names.doi_safe("10.1/a:b") == "10.1_a_b"


@pytest.mark.parametrize("doi", [SICI, "10.1/a\\..\\..\\evil", '10.1/"x"|y?*#%'])
def test_names_are_portable_single_components(doi) -> None:
    name = cache_names.doi_safe(doi)
    assert not (_WINDOWS_ILLEGAL & set(name)), name
    path = cache_names.cache_pdf_path("/cache", doi)
    assert path.parent == Path("/cache")


def test_dot_only_names_are_not_directory_references() -> None:
    assert cache_names.doi_safe("..") not in {".", ".."}


@pytest.mark.parametrize("module", [
    "fetchers.crossref", "fetchers.openalex", "fetchers.unpaywall",
    "fetchers.pmc", "fetchers.semantic_scholar", "fetchers.springer",
    "fetchers.sciencedirect", "fetchers.preprint", "fetchers.openaire",
    "fetchers.core", "fetchers.base_search",
])
def test_every_fetcher_uses_the_shared_rule(module) -> None:
    mod = importlib.import_module(module)
    assert mod._doi_safe(SICI) == cache_names.doi_safe(SICI)


def test_browser_cache_and_diagnostics_use_the_shared_rule(tmp_path) -> None:
    assert cache_path_for(tmp_path, SICI).name == f"{cache_names.doi_safe(SICI)}.pdf"
    assert not (_WINDOWS_ILLEGAL & set(_diagnostic_stem("sage", SICI)))


def test_all_cache_paths_covers_every_suffix_and_case(tmp_path) -> None:
    paths = {p.name for p in cache_names.all_cache_paths(tmp_path, "10.1/AB")}
    # Windows paths compare case-insensitively, so the two case variants
    # are one path (and one file) there and the dedup keeps only one.
    fold = sys.platform == "win32"
    if fold:
        paths = {name.lower() for name in paths}
    for stem in ("10.1_AB", "10.1_ab"):
        for suffix in cache_names.CACHE_SUFFIXES:
            want = f"{stem}{suffix}.pdf"
            assert (want.lower() if fold else want) in paths
    assert all(p.parent == tmp_path for p in cache_names.all_cache_paths(
        tmp_path, "10.1/AB"))
