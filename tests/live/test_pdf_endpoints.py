"""Live tests for direct-HTTP PDF retrieval endpoints.

Opt in with `pytest -m live`. Each test skips cleanly if the required
API key is not configured.

Pass criterion: response body starts with `%PDF-` magic bytes (or, for
endpoints that return metadata with a PDF URL, that URL is present).
"""

from __future__ import annotations

import json
import urllib.parse

import pytest

from tests.live.conftest import (
    KNOWN_DOIS,
    classify_non_pdf_body,
    http_get,
    require_config,
)

pytestmark = pytest.mark.live


def test_crossref_tdm_link_present() -> None:
    """Crossref records a TDM-intended `link` for many publishers."""
    mailto = require_config("crossref", "mailto", env="CROSSREF_MAILTO")
    doi = KNOWN_DOIS["crossref_tdm"]
    status, body, _ = http_get(
        f"https://api.crossref.org/works/{urllib.parse.quote(doi, safe='/:')}",
        headers={"User-Agent": f"academic-research-live-tests (mailto:{mailto})"},
    )
    assert status == 200, f"Crossref returned {status}"
    data = json.loads(body).get("message", {})
    links = data.get("link") or []
    tdm_links = [link for link in links
                 if link.get("intended-application") == "text-mining"]
    if not tdm_links:
        pytest.skip(
            f"DOI {doi} has no text-mining link on Crossref (has "
            f"{[lnk.get('intended-application') for lnk in links]}). This is "
            f"a publisher deposit gap, not a Crossref failure. Try a different "
            f"DOI in KNOWN_DOIS['crossref_tdm'] — most Elsevier DOIs do deposit."
        )


def test_pmc_doi_to_pmcid_resolves() -> None:
    """PMC's id converter resolves a DOI to a PMC ID — no auth required."""
    doi = KNOWN_DOIS["pmc"]
    status, body, _ = http_get(
        f"https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/?ids={doi}&format=json"
    )
    assert status == 200, f"PMC converter returned {status}"
    records = json.loads(body).get("records") or []
    assert records, f"PMC has no record for DOI {doi}"
    assert records[0].get("pmcid"), f"PMC returned no pmcid for DOI {doi}: {records[0]}"


def test_elsevier_sciencedirect_reachable() -> None:
    """Elsevier ScienceDirect key accepted for a DOI lookup (200 or 404 both OK)."""
    key = require_config("elsevier", "api_key", env="ELSEVIER_API_KEY")
    doi = KNOWN_DOIS["elsevier"]
    status, _, _ = http_get(
        f"https://api.elsevier.com/content/article/doi/{doi}",
        headers={"X-ELS-APIKey": key, "Accept": "application/json"},
    )
    # 401/403 would mean the key is bad; 404 means DOI not in Elsevier's index
    # but the key is fine; 200 means success.
    assert status in (200, 404), (
        f"Elsevier returned {status}. 200 or 404 expected; 401/403 means "
        f"the key is rejected."
    )


def test_openalex_content_api_returns_pdf_bytes() -> None:
    """OpenAlex Content API returns a PDF for a paid-tier key."""
    key = require_config("openalex", "api_key", env="OPENALEX_API_KEY")
    doi = KNOWN_DOIS["openalex_content"]
    # First fetch the work to locate its PDF URL
    status, body, _ = http_get(
        f"https://api.openalex.org/works/https://doi.org/{doi}?api_key={key}",
    )
    if status == 404:
        pytest.skip(f"OpenAlex has no record for DOI {doi}; update KNOWN_DOIS")
    assert status == 200, f"OpenAlex returned {status}"
    work = json.loads(body)
    pdf_url = (work.get("best_oa_location") or {}).get("pdf_url")
    if not pdf_url:
        pytest.skip(f"No PDF URL for DOI {doi} in OpenAlex; update KNOWN_DOIS")
    # Fetch first 1KB of the PDF to verify %PDF- magic without full download
    status, pdf_bytes, _ = http_get(pdf_url, headers={"Range": "bytes=0-1023"})
    assert status in (200, 206), f"PDF fetch returned {status}"
    assert pdf_bytes.startswith(b"%PDF-"), (
        f"OpenAlex content URL did not return a PDF. "
        f"Body classification: {classify_non_pdf_body(pdf_bytes)}"
    )


def test_springer_reachable() -> None:
    """SpringerLink direct PDF works on institutional networks (no API key).

    Springer has no public API; PDFs come from
    `link.springer.com/content/pdf/{doi}.pdf`. Returns 200 + %PDF- when
    the caller's IP is recognised as institutional; 200 HTML landing
    page or 403 otherwise. The test skips if not on an institutional
    network rather than failing.
    """
    doi = "10.1007/s11187-016-9771-4"  # stable Small Business Economics paper
    status, body, _ = http_get(
        f"https://link.springer.com/content/pdf/{urllib.parse.quote(doi, safe='')}.pdf",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    if status == 0:
        pytest.skip("Network unreachable for SpringerLink")
    if status != 200 or not body.startswith(b"%PDF-"):
        pytest.skip(
            f"SpringerLink returned {status}, body starts with {body[:8]!r}. "
            f"Likely not on an institutional network. "
            f"Body classification: {classify_non_pdf_body(body)}"
        )
    assert body.startswith(b"%PDF-")


def test_unpaywall_returns_pdf_url() -> None:
    """Unpaywall returns an OA PDF URL for an open-access DOI."""
    mailto = require_config("crossref", "mailto", env="CROSSREF_MAILTO")
    doi = KNOWN_DOIS["unpaywall"]
    status, body, _ = http_get(
        f"https://api.unpaywall.org/v2/{doi}?email={mailto}",
    )
    assert status == 200, f"Unpaywall returned {status}"
    data = json.loads(body)
    pdf_url = (data.get("best_oa_location") or {}).get("url_for_pdf")
    assert pdf_url, f"Unpaywall has no PDF URL for DOI {doi}"


def test_openalex_oa_url_present() -> None:
    """OpenAlex metadata (free, no key) exposes an OA URL for OA papers."""
    doi = KNOWN_DOIS["openalex_oa"]
    status, body, _ = http_get(f"https://api.openalex.org/works/https://doi.org/{doi}")
    if status == 404:
        pytest.skip(f"OpenAlex has no record for DOI {doi}; update KNOWN_DOIS")
    assert status == 200, f"OpenAlex returned {status}"
    work = json.loads(body)
    oa = work.get("open_access") or {}
    oa_url = oa.get("oa_url") or (work.get("best_oa_location") or {}).get("url")
    assert oa_url, (
        f"OpenAlex returned no OA URL for DOI {doi}. "
        f"is_oa={oa.get('is_oa')}; this may mean the paper isn't OA."
    )


def test_wiley_tdm_downloads_pdf() -> None:
    """Wiley Text and Data Mining client downloads a PDF for a Wiley DOI."""
    token = require_config("wiley", "tdm_token", env="WILEY_TDM_TOKEN")
    pytest.importorskip(
        "wiley_tdm",
        reason="live test requires `wiley-tdm` — install with "
               "`uv pip install wiley-tdm`",
    )
    import os
    import tempfile

    from wiley_tdm import TDMClient

    doi = KNOWN_DOIS["wiley_tdm"]
    with tempfile.TemporaryDirectory() as tmp:
        client = TDMClient(api_token=token, download_dir=tmp)
        result = client.download_pdfs([doi])
        assert result, f"Wiley TDM returned no result for DOI {doi}"
        entry = result[0]
        # Common rejection modes from the Wiley TDM API that signal
        # "this DOI is outside your institution's TDM scope" — SKIP, not FAIL.
        status_str = str(entry).lower()
        scope_markers = ("unknown doi", "not entitled", "not authorized",
                         "forbidden", "no access")
        if any(m in status_str for m in scope_markers):
            pytest.skip(
                f"Wiley TDM rejects DOI {doi} as out of scope: {entry}. "
                f"Your institution's Wiley TDM agreement may not cover this "
                f"journal. Try a different DOI in KNOWN_DOIS['wiley_tdm']."
            )
        pdf_path = getattr(entry, "file_path", None) or getattr(entry, "path", None)
        assert pdf_path and os.path.exists(str(pdf_path)), (
            f"Wiley TDM did not produce a file for {doi}: {entry}"
        )
        with open(str(pdf_path), "rb") as f:
            head = f.read(5)
        assert head == b"%PDF-", (
            f"Wiley TDM output for {doi} is not a PDF (got {head!r})"
        )
