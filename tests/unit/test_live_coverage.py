"""Guard test: every publisher / KeySpec / source has a matching live test.

Runs on every `pytest` invocation (no marker). If a new browser
handler is added to `fetchers.browser.all_handlers()`, a new `KeySpec`
added to `scripts/setup/wizard.py:KEYS`, or a new `AbstractFetcher` /
`PdfFetcher` subclass added under `scripts/pipelines/fetchers/`
without a matching live test being added at the same time, this
test fails with an actionable message.

The policy is documented in the project memory at
`feedback_every_source_has_a_test.md`:

> Every publisher / source / API key has a live test — adding a new
> registry entry, KeySpec, or source module must ship with a matching
> live test; a default-run guard test enforces the invariant at PR
> time.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS_ROOT = REPO / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))


def _leaf_sources() -> list[type]:
    """Every concrete fetcher class registered via `fetchers/__init__.py`.

    Walks `Source.__subclasses__()` recursively (importing `fetchers`
    registers every source module); a class counts as a leaf when it
    declares `name` in its own `__dict__`. Interactive sources
    (BrowserSource) are covered by the browser-publisher guard instead.

    Deliberately NOT based on `fetchers.pdf_sources()` — that function
    default-excludes `wiley` and `browser`, and a coverage guard must
    see every source, selected or not.
    """
    import fetchers  # noqa: F401 — importing registers all source modules
    from fetchers.base import Source

    seen: set[type] = set()
    stack = list(Source.__subclasses__())
    while stack:
        cls = stack.pop()
        if cls in seen:
            continue  # dual-ABC classes are reached via both parents
        seen.add(cls)
        stack.extend(cls.__subclasses__())
    return [
        cls for cls in seen
        if cls.__dict__.get("name") and not cls.interactive
    ]


def _load_wizard():
    spec = importlib.util.spec_from_file_location(
        "wizard", SCRIPTS_ROOT / "setup" / "wizard.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["wizard"] = mod
    spec.loader.exec_module(mod)
    return mod


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Publishers ↔ test_browser_publishers.py
# ---------------------------------------------------------------------------


def test_every_publisher_in_registry_has_a_known_doi() -> None:
    """test_browser_publishers.py is parametrized from the handler
    registry; the corresponding DOI must exist in KNOWN_DOIS."""
    from fetchers.browser import all_handlers

    conftest = _read(REPO / "tests" / "live" / "conftest.py")
    # Parse KNOWN_DOIS keys from conftest source (not importable without
    # pytest session setup). Accept both "key": and 'key':
    known_keys = set(re.findall(r"[\"']([a-zA-Z_0-9]+)[\"']\s*:\s*\"10\.", conftest))

    missing = []
    for handler in all_handlers():
        if handler.name not in known_keys:
            missing.append(handler.name)
    assert not missing, (
        f"Browser handlers without a KNOWN_DOIS entry: {missing}. "
        f"Add DOIs to tests/live/conftest.py so test_browser_publishers.py "
        f"can exercise them."
    )


# ---------------------------------------------------------------------------
# KeySpecs ↔ test_auth_workflows.py
# ---------------------------------------------------------------------------


def test_every_keyspec_has_an_auth_test() -> None:
    """Every KeySpec in wizard.py:KEYS has a test in test_auth_workflows.py."""
    wizard = _load_wizard()
    auth_tests = _read(REPO / "tests" / "live" / "test_auth_workflows.py")

    missing = []
    for spec in wizard.KEYS:
        if spec.env_var:
            # The auth test references the env var name directly or in a
            # comment/docstring. Accept any mention as sufficient.
            needle = spec.env_var
        else:
            # A config-only spec has no variable to search for, and
            # `"" in text` is vacuously true — which would silently
            # exempt the specs least likely to be covered.
            #
            # Match a declared marker rather than a `require_config(...)`
            # call: how a test fetches the value is an implementation
            # detail that legitimately changes (the gateway key moved to
            # `llm_provider._api_key_for` so it would follow the
            # `api_key_env` indirection), and a guard that breaks on a
            # refactor of the thing it guards teaches people to edit the
            # guard. The marker states the intent instead.
            needle = f"[{spec.toml_section}].{spec.toml_key}"
        if needle not in auth_tests:
            missing.append(spec.env_var or f"[{spec.toml_section}].{spec.toml_key}")
    assert not missing, (
        f"KeySpecs without a matching test in test_auth_workflows.py: "
        f"{missing}. Add a test_auth_{{name}} function that calls the "
        f"matching _verify_* helper. A config-only spec (no env var) is "
        f"matched by a `covers: [section].key` marker in that file, "
        f"since it has no variable name to grep for."
    )


# ---------------------------------------------------------------------------
# Abstract sources ↔ test_abstract_endpoints.py
# ---------------------------------------------------------------------------


# Source `name` → live-test function names in test_abstract_endpoints.py.
# Update this mapping when you add or rename a source or its test.
ABSTRACT_LIVE_TESTS: dict[str, list[str]] = {
    "crossref": ["test_crossref_abstract"],
    "semantic_scholar": ["test_semantic_scholar_abstract"],
    "scopus": ["test_scopus_abstract"],
    "wos": ["test_wos_abstract_direct_doi", "test_wos_title_fallback_on_doi_alias"],
    "sciencedirect": ["test_sciencedirect_abstract"],
    "openalex": ["test_openalex_grobid_abstract"],
}


def test_every_abstract_source_has_a_live_test() -> None:
    """Each `AbstractFetcher` subclass has a matching live test."""
    from fetchers.base import AbstractFetcher

    sources = sorted(
        cls.name for cls in _leaf_sources() if issubclass(cls, AbstractFetcher)
    )
    abstract_tests = _read(REPO / "tests" / "live" / "test_abstract_endpoints.py")

    missing = []
    for name in sources:
        expected_tests = ABSTRACT_LIVE_TESTS.get(name)
        if expected_tests is None:
            missing.append(
                f"{name} (no entry in ABSTRACT_LIVE_TESTS — add one plus a "
                f"live test)"
            )
            continue
        missing.extend(
            f"{name} → expected {t}" for t in expected_tests
            if t not in abstract_tests
        )
    assert not missing, (
        f"Abstract sources without a matching live test: {missing}. "
        f"Add a corresponding test to tests/live/test_abstract_endpoints.py."
    )


# ---------------------------------------------------------------------------
# PDF sources ↔ test_pdf_endpoints.py
# ---------------------------------------------------------------------------


# Source `name` → live-test function names in test_pdf_endpoints.py.
# The two OpenAlex endpoints are two separate sources: `openalex_content`
# is the paid Content API (version of record, stage 2 of the cascade) and
# `openalex` is the free OA-metadata tier (stage 3). They were a single
# class with one pair of live tests until the tiers were split apart, so
# the two existing tests carry over one apiece.
PDF_LIVE_TESTS: dict[str, list[str]] = {
    "sciencedirect": ["test_elsevier_sciencedirect_reachable"],
    "springer": ["test_springer_reachable"],
    "crossref": ["test_crossref_tdm_link_present"],
    "pubmed_central": ["test_pmc_doi_to_pmcid_resolves"],
    "openalex": ["test_openalex_oa_url_present"],
    "openalex_content": ["test_openalex_content_api_returns_pdf_bytes"],
    "unpaywall": ["test_unpaywall_returns_pdf_url"],
    "semantic_scholar": ["test_semantic_scholar_open_access_pdf_url"],
    "core": ["test_core_search_returns_a_download_url"],
    "openaire": ["test_openaire_search_returns_a_record"],
    "base": ["test_base_search_is_reachable_or_ip_gated"],
    "preprint": ["test_preprint_discovery_finds_a_preprint_hosted_pdf"],
    "wiley": ["test_wiley_tdm_downloads_pdf"],
}


def test_every_pdf_source_has_a_live_test() -> None:
    """Each non-interactive `PdfFetcher` subclass has a matching live test.

    `BrowserSource` (interactive) is excluded here; its per-publisher
    handlers are covered by `test_every_publisher_in_registry_has_a_known_doi`.
    """
    from fetchers.base import PdfFetcher

    sources = sorted(
        cls.name for cls in _leaf_sources() if issubclass(cls, PdfFetcher)
    )
    pdf_tests = _read(REPO / "tests" / "live" / "test_pdf_endpoints.py")

    missing = []
    for name in sources:
        expected_tests = PDF_LIVE_TESTS.get(name)
        if expected_tests is None:
            missing.append(
                f"{name} (no entry in PDF_LIVE_TESTS — add one plus a "
                f"live test)"
            )
            continue
        missing.extend(
            f"{name} → expected {t}" for t in expected_tests
            if t not in pdf_tests
        )
    assert not missing, (
        f"PDF sources without a matching live test: {missing}. "
        f"Add a corresponding test to tests/live/test_pdf_endpoints.py."
    )
