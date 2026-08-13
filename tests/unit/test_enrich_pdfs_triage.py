"""Publisher / browser-handler triage in the API cascade.

The 244-item run that motivated this: 76 of the 119 "failures" were
Sage and Academy of Management articles that the plain-HTTP cascade
structurally cannot reach — Cloudflare blocks it before any API key
matters — and the pipeline reported them as `UNAVAILABLE`, whose
suggested action is an FE6 exclusion.

`_triage_context` is what turns that into "Sage, browser handler
available, not yet run". It reads only what is already on disk (the
Crossref resolver cache) plus a pure registry lookup, so it adds no
network calls to a cascade that has just failed.
"""

from __future__ import annotations

import json
from pathlib import Path

import enrich_pdfs
import pytest

# Real prefixes from the run described above.
SAGE_DOI = "10.1177/0149206320901565"
AOM_DOI = "10.5465/amj.2019.0090"
SPRINGER_DOI = "10.1007/s11187-019-00281-3"
ETAP_DOI = "10.1111/etap.12345"  # Wiley prefix, migrated to Sage


@pytest.fixture(scope="module")
def enrich():
    return enrich_pdfs


def _seed_cache(cache_dir: Path, entries: dict[str, dict]) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "doi_resolver_cache.json").write_text(
        json.dumps(entries), encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Handler resolution
# ---------------------------------------------------------------------------


def test_sage_doi_finds_the_sage_handler(enrich, tmp_path) -> None:
    publisher, handler = enrich._triage_context(SAGE_DOI, str(tmp_path))
    assert handler == "sage"
    assert publisher == "Sage"


def test_aom_doi_finds_the_aom_handler(enrich, tmp_path) -> None:
    publisher, handler = enrich._triage_context(AOM_DOI, str(tmp_path))
    assert handler == "aom"
    assert publisher == "Academy of Management"


def test_springer_doi_has_no_browser_handler(enrich, tmp_path) -> None:
    """The 15 Springer items really were unreachable.

    No handler means the cascade's verdict stands, and the item is a
    genuine FE6 candidate rather than a "try harder" one.
    """
    _seed_cache(tmp_path, {
        SPRINGER_DOI: {
            "url": "https://link.springer.com/article/10.1007/s11187-019-00281-3",
            "publisher": "Springer Science and Business Media LLC",
        },
    })
    publisher, handler = enrich._triage_context(SPRINGER_DOI, str(tmp_path))
    assert handler == ""
    assert publisher == "Springer"


def test_resolved_host_beats_a_misleading_doi_prefix(enrich, tmp_path) -> None:
    """ETAP kept its Wiley prefix after moving to Sage.

    Prefix-only matching sends it to the Wiley handler, which cannot
    fetch it. The Crossref-resolved host is the ground truth.
    """
    _seed_cache(tmp_path, {
        ETAP_DOI: {
            "url": "https://journals.sagepub.com/doi/10.1111/etap.12345",
            "publisher": "SAGE Publications",
        },
    })
    _publisher, handler = enrich._triage_context(ETAP_DOI, str(tmp_path))
    assert handler == "sage"


def test_doi_prefix_is_used_when_no_url_is_cached(enrich, tmp_path) -> None:
    """A cold cache still gets the common case right."""
    _publisher, handler = enrich._triage_context(SAGE_DOI, str(tmp_path))
    assert handler == "sage"


# ---------------------------------------------------------------------------
# Robustness — triage metadata must never break a fetch run
# ---------------------------------------------------------------------------


def test_corrupt_cache_does_not_raise(enrich, tmp_path) -> None:
    (tmp_path / "doi_resolver_cache.json").write_text("{not json", encoding="utf-8")
    publisher, handler = enrich._triage_context(SAGE_DOI, str(tmp_path))
    assert handler == "sage"  # registry lookup is independent of the cache
    assert isinstance(publisher, str)


def test_unknown_doi_yields_empty_triage(enrich, tmp_path) -> None:
    assert enrich._triage_context("10.9999/nope", str(tmp_path)) == ("", "")


# ---------------------------------------------------------------------------
# Publisher tidying — grouping is only useful if the labels match
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("SAGE Publications", "SAGE"),
        ("SAGE Publications Ltd", "SAGE"),
        ("SAGE Publications Inc", "SAGE"),
        ("Springer Science and Business Media LLC", "Springer"),
        ("Wiley", "Wiley"),
        ("", ""),
    ],
)
def test_publisher_names_collapse_to_one_label(enrich, raw, expected) -> None:
    assert enrich._tidy_publisher(raw) == expected


def test_tidying_never_empties_a_name(enrich) -> None:
    """A publisher literally called "Publishing Group" keeps its name."""
    assert enrich._tidy_publisher("Publishing Group") == "Publishing Group"


# ---------------------------------------------------------------------------
# Resume set
# ---------------------------------------------------------------------------


def test_connector_successes_count_as_done(enrich, tmp_path) -> None:
    """`attached_via_connector` was missing from the resume statuses, so
    every Connector success was re-queued on the next run."""
    log = tmp_path / "pdf_attach_log.csv"
    log.write_text(
        "run_date,item_key,doi,title,status,source\n"
        "2026-08-13,A,10.1/a,A,attached,crossref\n"
        "2026-08-13,B,10.1/b,B,attached_via_connector,connector\n"
        "2026-08-13,C,10.1/c,C,skipped_no_pdf,connector\n",
        encoding="utf-8",
    )
    assert enrich._load_done_dois(str(log)) == {"10.1/a", "10.1/b"}
