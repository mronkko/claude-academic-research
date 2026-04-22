"""Guard test: every publisher / KeySpec / source has a matching live test.

Runs on every `pytest` invocation (no marker). If a new entry is
added to `publishers.registry.DEFAULT_PUBLISHERS`, a new `KeySpec`
added to `scripts/setup/wizard.py:KEYS`, or a new `fetch_from_*`
function added to `scripts/pipelines/legacy/fetch_abstracts.py`
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
    """test_browser_publishers.py is parametrized from the registry; the
    corresponding DOI must exist in KNOWN_DOIS."""
    from publishers.registry import DEFAULT_PUBLISHERS

    conftest = _read(REPO / "tests" / "live" / "conftest.py")
    # Parse KNOWN_DOIS keys from conftest source (not importable without
    # pytest session setup). Accept both "key": and 'key':
    known_keys = set(re.findall(r"[\"']([a-zA-Z_0-9]+)[\"']\s*:\s*\"10\.", conftest))

    missing = []
    for pub_key in DEFAULT_PUBLISHERS:
        if pub_key not in known_keys:
            missing.append(pub_key)
    assert not missing, (
        f"Registry publishers without a KNOWN_DOIS entry: {missing}. "
        f"Add DOIs to tests/live/conftest.py so test_browser_publishers.py "
        f"can exercise them."
    )


# ---------------------------------------------------------------------------
# KeySpecs ↔ test_auth_workflows.py
# ---------------------------------------------------------------------------


def test_every_keyspec_has_an_auth_test() -> None:
    """Every KeySpec in wizard.py:KEYS has a test in test_auth_workflows.py."""
    wizard = _load_wizard()
    env_vars = {spec.env_var for spec in wizard.KEYS}

    auth_tests = _read(REPO / "tests" / "live" / "test_auth_workflows.py")

    missing = []
    for env_var in env_vars:
        # The auth test either references the env var name directly or in a
        # comment/docstring. Accept any mention as sufficient.
        if env_var not in auth_tests:
            missing.append(env_var)
    assert not missing, (
        f"KeySpecs without a matching test in test_auth_workflows.py: "
        f"{missing}. Add a test_auth_{{name}} function that calls the "
        f"matching _verify_* helper."
    )


# ---------------------------------------------------------------------------
# Abstract sources ↔ test_abstract_endpoints.py
# ---------------------------------------------------------------------------


def test_every_abstract_source_has_a_live_test() -> None:
    """Each `fetch_from_*` function in legacy/fetch_abstracts.py has a matching test.

    The legacy script moved to `scripts/pipelines/legacy/` in v0.3.1.
    When the refactored `fetchers/*.py` classes become the coverage
    source of truth, this function (and `test_every_pdf_source_...`
    below) should walk them instead — then the legacy/ directory can
    be deleted.
    """
    fetch_source = _read(
        REPO / "scripts" / "pipelines" / "legacy" / "fetch_abstracts.py"
    )
    sources = set(re.findall(r"^def (fetch_from_\w+)\s*\(", fetch_source, re.MULTILINE))

    abstract_tests = _read(REPO / "tests" / "live" / "test_abstract_endpoints.py")

    # Known aliases between source-function names and test names.
    # Update this mapping when you rename a source or its test.
    alias: dict[str, str] = {
        "fetch_from_crossref": "test_crossref_abstract",
        "fetch_from_semantic_scholar": "test_semantic_scholar_abstract",
        "fetch_from_semantic_scholar_by_title": "test_semantic_scholar_abstract",
        "fetch_from_scopus": "test_scopus_abstract",
        "fetch_from_sciencedirect": "test_sciencedirect_abstract",
        "fetch_from_openalex_grobid": "test_openalex_grobid_abstract",
    }

    missing = []
    for src in sorted(sources):
        expected_test = alias.get(src)
        if expected_test is None:
            missing.append(
                f"{src} (no alias — add one to test_live_coverage.py or "
                f"rename the source)"
            )
        elif expected_test not in abstract_tests:
            missing.append(f"{src} → expected {expected_test}")
    assert not missing, (
        f"Abstract sources without a matching live test: {missing}. "
        f"Add a corresponding test to tests/live/test_abstract_endpoints.py."
    )


# ---------------------------------------------------------------------------
# PDF sources ↔ test_pdf_endpoints.py
# ---------------------------------------------------------------------------


def test_every_pdf_source_has_a_live_test() -> None:
    """Each `fetch_*_pdf` function in legacy/attach_pdfs.py has a matching test.

    See note in `test_every_abstract_source_has_a_live_test` about the
    migration path; this function is the PDF-cascade counterpart.
    """
    attach_source = _read(
        REPO / "scripts" / "pipelines" / "legacy" / "attach_pdfs.py"
    )
    sources = set(re.findall(r"^def (fetch_\w+_pdf)\s*\(", attach_source, re.MULTILINE))

    pdf_tests = _read(REPO / "tests" / "live" / "test_pdf_endpoints.py")

    # Source-function → expected test name. The test may also cover the
    # source indirectly; we accept any test mentioning the source label.
    alias: dict[str, str] = {
        "fetch_elsevier_pdf": "test_elsevier_sciencedirect_reachable",
        "fetch_springer_pdf": "test_springer_reachable",  # not yet implemented
        "fetch_crossref_tdm_pdf": "test_crossref_tdm_link_present",
        "fetch_pmc_pdf": "test_pmc_doi_to_pmcid_resolves",
        "fetch_pdf_from_url": None,  # generic helper, not a source
        "fetch_unpaywall_pdf": "test_unpaywall_returns_pdf_url",
        "fetch_openalex_content_pdf": "test_openalex_content_api_returns_pdf_bytes",
        "fetch_openalex_pdf": "test_openalex_oa_url_present",
    }

    missing = []
    for src in sorted(sources):
        if src not in alias:
            missing.append(
                f"{src} (no alias — add one to test_live_coverage.py or "
                f"write a matching test)"
            )
            continue
        expected_test = alias[src]
        if expected_test is None:
            continue  # explicitly not a source
        if expected_test not in pdf_tests:
            missing.append(f"{src} → expected {expected_test}")
    assert not missing, (
        f"PDF sources without a matching live test: {missing}. "
        f"Add a corresponding test to tests/live/test_pdf_endpoints.py."
    )
