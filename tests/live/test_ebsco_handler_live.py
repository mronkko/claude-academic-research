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

The first DOI is deliberately a pre-1997 article. Aalto's FinELib
SpringerLink holding starts in 1997, so no publisher handler can reach it
— EBSCOhost (from 1982) is the only route, which makes this a real test
of the population the coverage guard diverts here rather than a case that
would have worked anyway.

The second DOI covers the **chooser**: when an OpenURL matches more than
one EBSCO record, EBSCO asks the reader to pick instead of redirecting to
a viewer. Measured live on 2026-09-11 through JYU EZproxy,
`10.5465/amj.2022.0253` returned two records — identical in DOI, volume,
issue, page and authors, one title-cased and one ALL-CAPS — and the
handler used to stop there with "never reached the viewer". Whether that
page appears is a property of the tenant's record set, not of the DOI, so
the end-to-end test asserts bytes and the detection test skips when this
library's OpenURL resolves straight through.
"""

from __future__ import annotations

import asyncio
import os
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


# ---------------------------------------------------------------------------
# The "Multiple items found" chooser
# ---------------------------------------------------------------------------

# To, Sherf & Kouchaki 2024, Academy of Management Journal 67(1):126-149.
# Two EBSCO records carry this DOI (189318373 and 175563540 on one
# tenant), which is what makes the OpenURL land on the chooser.
_CHOOSER_DOI = "10.5465/amj.2022.0253"
_CHOOSER_PUB_DATE = "2024-02"


#: Reuse an existing browser profile instead of the throwaway `tmp_path`
#: one, for tenants whose authentication persists in cookies.
#:
#: It does **not** rescue an SSO route. Measured 2026-09-11: a profile
#: that had completed a Shibboleth login minutes earlier
#: (`probe_browser_handler.py --setup`, same `--cache-dir`) no longer
#: held the session once its Chromium exited — and the Chromium has to
#: exit, because it holds the profile lock. One run reached
#: `openurl-ebsco-com.ezproxy.jyu.fi/linksvc/linking.aspx` and the next
#: landed back on `login.jyu.fi/idp`. So an SSO-walled route needs a
#: human inside the run, which pytest has no way to provide; the
#: supported way to test one is a library whose EBSCO route
#: authenticates on institutional IP.
_PROFILE_ENV = "EBSCO_LIVE_CACHE_DIR"


def _browser_cache_dir(tmp_path) -> str:
    return os.environ.get(_PROFILE_ENV) or str(tmp_path)


def _warm_profile() -> bool:
    return bool(os.environ.get(_PROFILE_ENV))


def _chooser_route(resolver_cfg):
    """The EBSCO route for `_CHOOSER_DOI`, or a skip.

    Skips rather than fails on a login-walled route: some tenants reach
    EBSCO through an EZproxy that demands SSO, and no unattended run can
    clear a SAML prompt. Set `EBSCO_LIVE_CACHE_DIR` to a cache directory
    whose `.chrome-profile` already holds that session to run it anyway.
    """
    from fetchers.library_resolver import lookup_fulltext_target
    from fetchers.resolvers.base import needs_interactive_login

    result = lookup_fulltext_target(
        _CHOOSER_DOI, resolver_cfg, pub_date=_CHOOSER_PUB_DATE,
    )
    if not result.query_ok:
        pytest.skip("resolver unreachable — needs the institutional network")
    if result.url is None:
        pytest.skip(f"library reports no route for {_CHOOSER_DOI}")
    if needs_interactive_login(result.url) and not _warm_profile():
        pytest.skip(
            f"the route for {_CHOOSER_DOI} goes through a proxy that signs "
            f"the reader in ({result.url[:60]}...), and no unattended run "
            f"can answer that. Configure a library whose EBSCO route "
            f"authenticates on institutional IP. {_PROFILE_ENV} forces the "
            f"attempt with an existing profile, but a Shibboleth session "
            f"does not survive the browser exit — see {_PROFILE_ENV}'s note."
        )
    return result


def test_an_openurl_matching_several_records_still_yields_the_pdf(
    resolver_cfg, tmp_path,
) -> None:
    """The regression this file exists to hold: a chooser must not end the
    attempt.

    Asserts bytes and page count, and that the run did not end on
    `VERDICT_AMBIGUOUS` — the refusal that fires only when no candidate
    could be tied to the DOI. Arriving at the viewer directly is an
    equally good outcome, so the verdict is asserted as "not the refusal"
    rather than as the chooser path specifically.
    """
    pytest.importorskip(
        "playwright.async_api",
        reason="requires `playwright` — `uv pip install playwright && "
               "playwright install chromium`",
    )
    pypdf = pytest.importorskip(
        "pypdf", reason="page-count assertion needs pypdf (in the dev group)",
    )
    from fetchers.browser import EbscoHandler, launch_context
    from fetchers.browser.base import Counter
    from fetchers.browser.ebsco import VERDICT_AMBIGUOUS
    from playwright.async_api import async_playwright

    result = _chooser_route(resolver_cfg)
    item = {
        "doi": _CHOOSER_DOI,
        "item_key": "LIVECHOOSE",
        "title": "How Much Inequity Do You See?",
        "resolver_target_url": result.url,
    }
    handler = EbscoHandler()

    async def _run():
        async with async_playwright() as pw:
            ctx = await launch_context(pw, _browser_cache_dir(tmp_path))
            try:
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                return await handler.download(
                    page, ctx, item, str(tmp_path),
                    counter=Counter(), total=1, t_start=0.0,
                )
            finally:
                await ctx.close()

    out = asyncio.run(_run())
    assert handler.last_verdict != VERDICT_AMBIGUOUS, (
        "refused to choose — no candidate on the chooser carried this DOI; "
        "check whether EBSCO still puts the DOI in each record's href"
    )
    assert out is not None, (
        f"handler returned nothing (verdict={handler.last_verdict!r}, "
        f"error={handler.last_error!r})"
    )
    path, url = out
    assert "content.ebscohost.com" in url
    data = path.read_bytes()
    assert data[:5] == b"%PDF-"
    pages = len(pypdf.PdfReader(str(path)).pages)
    assert pages > 1, f"got a {pages}-page file — likely a preview, not the article"


def test_the_chooser_is_recognised_and_its_candidates_carry_the_doi(
    resolver_cfg, tmp_path,
) -> None:
    """Point the real page shape at the real predicates.

    The unit tests pin `is_openurl_chooser` and `chooser_candidates` to
    markup captured on one day; this one asks whether EBSCO still serves
    that shape. Skips when the OpenURL resolves straight to the viewer,
    because that is a tenant property and not a failure.
    """
    pytest.importorskip("playwright.async_api", reason="requires `playwright`")
    from fetchers.browser import EbscoHandler, launch_context
    from fetchers.browser.ebsco import chooser_candidates, is_openurl_chooser
    from playwright.async_api import async_playwright

    result = _chooser_route(resolver_cfg)
    handler = EbscoHandler()

    async def _land():
        async with async_playwright() as pw:
            ctx = await launch_context(pw, _browser_cache_dir(tmp_path))
            try:
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await page.goto(result.url, wait_until="commit", timeout=30000)
                # The OpenURL layer renders its list client-side.
                await page.wait_for_timeout(3000)
                return page.url, await handler._candidate_hrefs(page)
            finally:
                await ctx.close()

    landed_url, hrefs = asyncio.run(_land())
    if not is_openurl_chooser(landed_url):
        pytest.skip(
            f"this library's OpenURL did not stop on the chooser "
            f"(landed on {landed_url[:80]}...) — nothing to disambiguate"
        )
    assert chooser_candidates(hrefs, _CHOOSER_DOI), (
        f"on the chooser at {landed_url[:80]}... but no candidate href "
        f"carries the DOI; hrefs were {hrefs[:3]}"
    )
