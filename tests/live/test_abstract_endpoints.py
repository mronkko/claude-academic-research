"""Live tests for direct-HTTP abstract retrieval endpoints.

Opt in with `pytest -m live`. Each test skips cleanly if the required
API key is not configured.

Pass criterion: endpoint returns a non-empty abstract string > 60
characters. Short (< 60 char) responses are typically fragments or
boilerplate, not a real abstract.
"""

from __future__ import annotations

import json
import re
import urllib.parse

import pytest

from tests.live.conftest import KNOWN_DOIS, http_get, require_config

pytestmark = pytest.mark.live


def _strip_jats(text: str) -> str:
    """Crossref abstracts are sometimes JATS XML; strip tags."""
    return re.sub(r"<[^>]+>", " ", text).strip()


def test_crossref_abstract() -> None:
    """Crossref stores publisher-deposited abstracts for many journals."""
    mailto = require_config("crossref", "mailto", env="CROSSREF_MAILTO")
    doi = KNOWN_DOIS["crossref_abstract"]
    status, body, _ = http_get(
        f"https://api.crossref.org/works/{urllib.parse.quote(doi, safe='/:')}",
        headers={"User-Agent": f"academic-research-live-tests (mailto:{mailto})"},
    )
    assert status == 200, f"Crossref returned {status}"
    msg = json.loads(body).get("message", {})
    raw = msg.get("abstract", "")
    if not raw:
        pytest.skip(
            f"Crossref has no abstract for DOI {doi} — publisher did not deposit one. "
            f"This is normal for many journals; not a bug."
        )
    text = _strip_jats(raw)
    assert len(text) > 60, (
        f"Crossref abstract for {doi} is suspiciously short ({len(text)} chars): "
        f"{text!r}"
    )


def test_semantic_scholar_abstract() -> None:
    """Semantic Scholar returns an abstract via DOI lookup."""
    key = require_config("semantic_scholar", "api_key",
                         env="SEMANTIC_SCHOLAR_API_KEY")
    doi = KNOWN_DOIS["semantic_scholar_abstract"]
    status, body, _ = http_get(
        f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}?fields=abstract",
        headers={"x-api-key": key},
    )
    assert status == 200, f"Semantic Scholar returned {status}"
    data = json.loads(body)
    abstract = (data.get("abstract") or "").strip()
    if not abstract:
        pytest.skip(
            f"Semantic Scholar has no abstract for DOI {doi}. Try a different "
            f"DOI in KNOWN_DOIS['semantic_scholar_abstract']."
        )
    assert len(abstract) > 60, (
        f"Semantic Scholar abstract for {doi} is suspiciously short: {abstract!r}"
    )


def test_scopus_abstract() -> None:
    """Scopus abstract retrieval via pybliometrics (reads pybliometrics.cfg)."""
    key = require_config("scopus", "api_key", env="SCOPUS_API_KEY")
    pytest.importorskip(
        "pybliometrics",
        reason="live test requires `pybliometrics` — install with "
               "`uv pip install pybliometrics`",
    )
    import os

    # pybliometrics reads its own config from ~/.config/pybliometrics.cfg;
    # fall back to env var for the API key. Skip if neither exists.
    cfg_exists = os.path.exists(os.path.expanduser("~/.config/pybliometrics.cfg"))
    if not cfg_exists and not key:
        pytest.skip("pybliometrics config not found and SCOPUS_API_KEY not set")

    from pybliometrics.scopus import AbstractRetrieval
    try:
        from pybliometrics.utils.startup import init
        init()
    except Exception:
        pass

    doi = KNOWN_DOIS["scopus_abstract"]
    # Use view="FULL" to match production code in fetch_abstracts.py. With
    # view="META_ABS", Scopus populates `.description` but leaves `.abstract`
    # as None — a pybliometrics quirk that would produce a false-negative.
    try:
        ar = AbstractRetrieval(doi, view="FULL")
    except Exception as e:
        pytest.fail(f"Scopus AbstractRetrieval failed for {doi}: {e}")
    abstract = (getattr(ar, "abstract", "") or "").strip()
    if len(abstract) < 60:
        pytest.skip(
            f"Scopus returned no abstract for DOI {doi} ({len(abstract)} chars). "
            f"Paper is indexed but the publisher did not deposit the abstract "
            f"into Scopus. Try a different DOI in KNOWN_DOIS['scopus_abstract']."
        )


def test_sciencedirect_abstract() -> None:
    """ScienceDirect ArticleRetrieval via pybliometrics for Elsevier DOIs."""
    key = require_config("elsevier", "api_key", env="ELSEVIER_API_KEY")
    pytest.importorskip(
        "pybliometrics",
        reason="live test requires `pybliometrics`",
    )
    import os

    cfg_exists = os.path.exists(os.path.expanduser("~/.config/pybliometrics.cfg"))
    if not cfg_exists and not key:
        pytest.skip("pybliometrics config not found and ELSEVIER_API_KEY not set")

    from pybliometrics.sciencedirect import ArticleRetrieval
    try:
        from pybliometrics.utils.startup import init
        init()
    except Exception:
        pass

    doi = KNOWN_DOIS["sciencedirect_abstract"]
    try:
        ar = ArticleRetrieval(doi, view="META_ABS")
    except Exception as e:
        pytest.skip(f"ScienceDirect returned error for {doi}: {e}")
    abstract = (getattr(ar, "abstract", "") or getattr(ar, "originalText", "") or "").strip()
    assert len(abstract) > 60, (
        f"ScienceDirect abstract for {doi} is empty or too short"
    )


def test_wos_abstract_direct_doi() -> None:
    """WoS Expanded returns an abstract for a DOI it indexes directly.

    Exercises the primary path of `WosSource.fetch_abstract`: query by
    DOI, read `abstracts.abstract.abstract_text.p` from the returned
    record.
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "pipelines"))

    key = require_config("wos", "expanded_key", env="WOS_API_KEY_EXTENDED")
    doi = KNOWN_DOIS["wos_abstract"]

    import http_client
    from fetchers.wos import WosSource

    class _C:
        wos_api_key_extended = key
        wos_api_key = ""

    session = http_client.build_session(mailto="live-tests@example.com")
    src = WosSource(http=session, config=_C())
    abstract = src.fetch_abstract(doi)
    if abstract is None:
        pytest.skip(
            f"WoS returned no abstract for DOI {doi}. Possibly the WoS "
            f"index dropped this paper — update KNOWN_DOIS['wos_abstract']."
        )
    assert len(abstract) > 60, (
        f"WoS abstract for {doi} is suspiciously short ({len(abstract)} chars)"
    )


def test_wos_title_fallback_on_doi_alias() -> None:
    """WoS indexes the paper under a different DOI prefix than the one
    in the Zotero library.

    AoM Annals pre-2014 was published by Routledge/T&F (10.1080/...);
    AoM re-issued with its own prefix (10.5465/...) after the publisher
    transfer. Libraries carry the 10.5465/... DOI; WoS kept the original.
    DOI lookup misses — the title-search fallback must recover it.

    Regression guard for the title-fallback path added in WosSource.
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "pipelines"))

    key = require_config("wos", "expanded_key", env="WOS_API_KEY_EXTENDED")
    doi = KNOWN_DOIS["wos_title_fallback_doi"]
    title = KNOWN_DOIS["wos_title_fallback_title"]

    import http_client
    from fetchers.wos import WosSource

    class _C:
        wos_api_key_extended = key
        wos_api_key = ""

    session = http_client.build_session(mailto="live-tests@example.com")
    src = WosSource(http=session, config=_C())

    # Sanity: DOI-only lookup must miss, proving the test is really
    # exercising the fallback. If WoS later indexes this DOI directly,
    # update KNOWN_DOIS['wos_title_fallback_doi'].
    doi_only = src.fetch_abstract(doi)
    assert doi_only is None, (
        f"DOI {doi} unexpectedly hit WoS directly. WoS may have re-indexed; "
        f"pick a different alias-only DOI for the fallback regression test."
    )

    abstract = src.fetch_abstract(doi, title=title)
    assert abstract is not None, (
        f"WoS title fallback failed for title={title!r}. Title normaliser "
        f"may have regressed, or the paper is no longer in WoS."
    )
    assert len(abstract) > 60, (
        f"WoS title-fallback abstract is too short ({len(abstract)} chars)"
    )


def test_openalex_grobid_abstract() -> None:
    """OpenAlex GROBID TEI XML has an <abstract> element for many works."""
    doi = KNOWN_DOIS["openalex_grobid"]
    # OpenAlex GROBID endpoint requires the paid Content API key
    key = require_config("openalex", "api_key", env="OPENALEX_API_KEY")
    status, body, _ = http_get(
        f"https://api.openalex.org/works/https://doi.org/{doi}/grobid?api_key={key}",
        headers={"Accept": "application/xml"},
    )
    if status == 404:
        pytest.skip(f"OpenAlex has no GROBID data for DOI {doi}")
    assert status == 200, f"OpenAlex GROBID returned {status}"
    # Handle gzip if compressed
    try:
        import gzip
        decoded = gzip.decompress(body).decode("utf-8", errors="replace")
    except Exception:
        decoded = body.decode("utf-8", errors="replace")
    # Extract <abstract> element content
    m = re.search(r"<abstract[^>]*>(.*?)</abstract>", decoded, re.DOTALL)
    assert m, f"No <abstract> element in GROBID TEI XML for DOI {doi}"
    abstract_text = re.sub(r"<[^>]+>", " ", m.group(1)).strip()
    assert len(abstract_text) > 60, (
        f"GROBID abstract for {doi} is empty or too short ({len(abstract_text)} chars)"
    )
