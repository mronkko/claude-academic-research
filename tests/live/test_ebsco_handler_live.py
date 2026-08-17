"""Live test: EBSCOhost retrieval through the real link resolver.

Exercises the one thing the unit tests cannot, because it is the entire
reason this handler exists: a six-hop redirect chain (Alma uresolver →
OCLC EZproxy → EBSCO OpenURL → EBSCO OAuth on institutional IP → results
page → PDF viewer) followed by a JavaScript app that requests its own
PDF. Every hop is somebody else's infrastructure, and the results page is
inert without a JS engine — so a mock proves nothing about whether this
works.

Requires the institutional network (Aalto VPN or on-campus) and a
configured `[library] openurl_base`; skips cleanly otherwise rather than
failing a run.

The DOI is deliberately a pre-1997 article. Aalto's FinELib SpringerLink
holding starts in 1997, so no publisher handler can reach it — EBSCOhost
(from 1982) is the only route, which makes this a real test of the
population the coverage guard diverts here rather than a case that would
have worked anyway.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytestmark = pytest.mark.live_browser

REPO_ROOT = Path(__file__).resolve().parents[2]

# Koys 1988, Journal of Business Ethics 7:459–466. Confirmed live
# (2026-08-17): Alma reports 15 getFullTxt routes, EBSCOhost among them
# from 1982; SpringerLink's holding starts 1997 and its own page paywalls.
_PRE_1997_DOI = "10.1007/bf00382859"
_PUB_YEAR = "1988"


@pytest.fixture
def resolver_cfg():
    import http_client
    from fetchers.library_resolver import load_from_config

    cfg = load_from_config(http_client.build_session(), None)
    if cfg is None:
        pytest.skip(
            "[library] openurl_base is not configured — run /setup and set "
            "your institution's SFX or Alma uresolver endpoint."
        )
    return cfg


def test_resolver_routes_this_doi_to_ebscohost(resolver_cfg) -> None:
    """Precondition for the handler test, and a regression guard for the
    coverage-aware ranking: the chosen platform must be one that actually
    holds 1988, not merely one the library subscribes to."""
    from fetchers.browser import is_ebsco_target
    from fetchers.library_resolver import lookup_fulltext_target

    result = lookup_fulltext_target(
        _PRE_1997_DOI, resolver_cfg, pub_date=_PUB_YEAR,
    )
    if not result.query_ok:
        pytest.skip("resolver unreachable — needs the institutional network")
    assert result.url is not None, "no licensed route reported for a held article"
    assert result.target is not None
    assert result.target.covers_year(_PUB_YEAR) is not False, (
        f"chosen route does not hold {_PUB_YEAR}: "
        f"{result.target.interface_name} / {result.target.coverage!r}"
    )
    assert is_ebsco_target(result.target), (
        f"expected an EBSCOhost route, got {result.target.interface_name!r}"
    )


def test_ebsco_handler_downloads_a_real_pdf(resolver_cfg, tmp_path) -> None:
    """The whole chain, ending in bytes on disk.

    Asserts page count as well as the `%PDF-` header: a plausible-looking
    `application/pdf` of a few hundred KB can still be a one-page preview,
    which is exactly how Elsevier's entitlement failure presents.
    """
    pytest.importorskip(
        "playwright.async_api",
        reason="requires `playwright` — `uv pip install playwright && "
               "playwright install chromium`",
    )
    # Imported up here, not next to the assertion that uses it. Skipping
    # mid-test left the download "passing" while the page-count check —
    # the only thing separating an article from a one-page preview —
    # silently never ran.
    pypdf = pytest.importorskip(
        "pypdf", reason="page-count assertion needs pypdf (in the dev group)",
    )
    from fetchers.browser import EbscoHandler, launch_context
    from fetchers.browser.base import Counter
    from fetchers.library_resolver import lookup_fulltext_target
    from playwright.async_api import async_playwright

    result = lookup_fulltext_target(
        _PRE_1997_DOI, resolver_cfg, pub_date=_PUB_YEAR,
    )
    if not result.query_ok or result.url is None:
        pytest.skip("resolver reported no route — needs the institutional network")

    item = {
        "doi": _PRE_1997_DOI,
        "item_key": "LIVE",
        "title": "Values Underlying Personnel/Human Resource Management",
        "resolver_target_url": result.url,
    }

    async def _run():
        async with async_playwright() as pw:
            ctx = await launch_context(pw, str(tmp_path))
            try:
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                return await EbscoHandler().download(
                    page, ctx, item, str(tmp_path),
                    counter=Counter(), total=1, t_start=0.0,
                )
            finally:
                await ctx.close()

    out = asyncio.run(_run())
    assert out is not None, (
        "handler returned nothing — check whether EBSCO still redirects the "
        "OpenURL page to /viewer/pdf/ and still fetches via "
        "content.ebscohost.com/cds/retrieve"
    )
    path, url = out
    assert "content.ebscohost.com" in url
    data = path.read_bytes()
    assert data[:5] == b"%PDF-"

    pypdf = pytest.importorskip("pypdf", reason="page-count assertion needs pypdf")
    pages = len(pypdf.PdfReader(str(path)).pages)
    assert pages > 1, f"got a {pages}-page file — likely a preview, not the article"
