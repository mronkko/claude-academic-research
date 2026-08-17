"""EBSCOhost — retrieval through the library link resolver.

Not a publisher handler. Every other handler in this package is chosen by
DOI prefix or publisher host; EBSCOhost is a *platform* that hosts many
publishers, so the only thing that routes an item here is the resolver
saying "your licensed route for this article is EBSCOhost". It is
therefore kept out of `all_handlers()`, alongside `ZoteroConnectorHandler`,
and driven from the resolver-target pass instead.

Why it is worth having
----------------------
Alma routes to EBSCOhost constantly — it appeared in every target set
measured against a real tenant — and its holdings reach much further back
than the publishers'. For the journals in one 97-item Springer run,
EBSCOhost covered from **1982** where FinELib SpringerLink starts at
1997. So this is the route to pre-1997 material that no publisher handler
can reach, which is exactly the population the coverage guard now diverts
away from Springer.

How retrieval works (measured 2026-08-17)
-----------------------------------------
Navigating the Alma `resolution_url` produces this chain, with no login
and no interactive step — EBSCO authenticates on institutional IP:

    Alma uresolver
      -> login.<inst>-libproxy.idm.oclc.org        (OCLC EZproxy)
      -> openurl.ebsco.com/linksvc/linking.aspx    (EBSCO OpenURL)
      -> logon.ebsco.zone/.../oauth/authorize      (acr_values=ip)
      -> openurl.ebsco.com/openurl?...             (results page, JS app)
      -> research.ebsco.com/c/<opid>/viewer/pdf/<recordId>

The results page is a dead end for a plain HTTP client: no `pdf` string,
no article link, `__NEXT_DATA__` only. That is why the API cascade cannot
do this and a browser must. Once the viewer loads, two requests carry the
file:

    research.ebsco.com/api/researcher-edge-aggregator/v1/records/
        <recordId>/fulltext/pdf?...        -> signed URL
    content.ebscohost.com/cds/retrieve?content=<signed token>  -> the PDF

**The signed URL works from a plain HTTP client** — verified: no cookies,
no session, 759 KB of `application/pdf`. So this handler only needs the
browser to *observe* that URL, and hands the download itself to
`ctx.request`, which is fast and needs no page. That is why it intercepts
a response rather than clicking the viewer's Download button or waiting
on a download event: the button would work, but it serialises everything
through the page and produces a file we then have to locate.

The `content=` token is opaque and per-request, so nothing here is
derivable from a DOI and no URL template is possible.
"""

from __future__ import annotations

import asyncio
import contextlib

from fetchers import _pdf_validate

from .base import PublisherHandler, cache_path_for, is_cached, progress_tag

#: Host that serves the actual bytes. Matched as a substring of the URL
#: rather than parsed, because it is only ever used to recognise our own
#: expected response.
_CONTENT_HOST = "content.ebscohost.com/cds/retrieve"

#: The viewer route, used only to tell "we arrived" from "we are still
#: bouncing through the proxy" in diagnostics.
_VIEWER_MARKER = "/viewer/pdf/"

#: Names the resolver uses for this platform. Kept here rather than
#: reusing PLATFORM_PRIORITY's entry so routing cannot silently change
#: when someone reorders the priority list.
EBSCO_PLATFORM_NAMES: tuple[str, ...] = ("ebscohost", "ebsco")


def is_ebsco_target(target) -> bool:
    """True when a resolver `FulltextTarget` is served by EBSCOhost.

    Checks the platform naming rather than the URL, because on Alma the
    URL is always the tenant's own redirector — see this module's
    docstring and `resolvers/base.py`.
    """
    if target is None:
        return False
    text = f"{getattr(target, 'package_name', '')} " \
           f"{getattr(target, 'interface_name', '')}".lower()
    if any(n in text for n in EBSCO_PLATFORM_NAMES):
        return True
    url = (getattr(target, "url", "") or "").lower()
    return "ebscohost.com" in url or "research.ebsco.com" in url


class EbscoHandler(PublisherHandler):
    """Drive the resolver target to EBSCO's viewer and capture the PDF."""

    # Bypasses `__init_subclass__`'s doi_prefixes requirement, the same
    # way `ZoteroConnectorHandler` does: that check exists to stop a leaf
    # *publisher* handler shipping unroutable, and this is a platform
    # handler the routing layer selects explicitly.
    _is_intermediate_base = True  # bypass doi_prefixes check

    name = "ebsco"
    display_name = "EBSCOhost"
    # Deliberately empty: nothing selects this handler by DOI. Keeping it
    # empty also keeps it out of `resolve_by_doi`.
    doi_prefixes = ()
    # Empty for the same reason `ZoteroConnectorHandler` leaves it empty:
    # the routing layer has already decided this item goes to EBSCO, and a
    # non-empty value here would let `resolve_by_host` pull this handler
    # into Pass 1, where it has no resolver target to work from.
    direct_access_domains = ()
    # No Cloudflare/Imperva interstitial was observed — IP auth is
    # silent — so a run needs no human at the keyboard for this platform.
    needs_interactive_solve = False
    #: The one handler raised above a single lane, because it is the one
    #: with the evidence. Every publisher handler in this package sits
    #: behind Cloudflare or Imperva, where N simultaneous requests from
    #: one IP is the shape bot detection looks for and the cost of being
    #: wrong is the whole publisher plus the profile's clearance. None of
    #: that applies here: authentication is by institutional IP with no
    #: interstitial, and most of the ~20 s an item takes is the six-hop
    #: redirect chain and the viewer's JS boot — waiting, not load. The
    #: bytes then come from a CDN via `ctx.request`, not through the page.
    #:
    #: Four rather than ten: unattended runs of 400+ items make this the
    #: pass worth parallelising, and 4 is the conservative first step
    #: against an aggregator API whose rate limits are undocumented.
    #: Raise it once a live run at 4 says it holds — `effective_lanes`
    #: caps `--browser-workers` here, so this number is the real ceiling.
    concurrency = 4
    delay_s = 1.0
    #: Seconds to wait for the viewer to request its own PDF. The chain is
    #: six redirects plus a JS app boot; 45s is generous rather than tight
    #: because the cost of being wrong is a false failure on an article we
    #: are entitled to.
    response_timeout_ms = 45000

    def setup_url_for(self, doi: str) -> str:  # pragma: no cover - unused
        """Unused: this handler is never set up per publisher, because it
        is entered from a resolver target rather than a DOI."""
        return f"https://doi.org/{doi}"

    async def download(self, page, ctx, item, cache_dir, *,
                       counter, total, t_start):
        doi = item["doi"]
        self.last_error = ""
        out = cache_path_for(cache_dir, doi)
        if is_cached(out):
            counter.cached += 1
            return out, f"cache://{out}"

        target_url = item.get("resolver_target_url")
        if not target_url:
            counter.failed += 1
            print(
                f"  {progress_tag(counter, total, t_start)} "
                f"SKIP: no resolver target URL",
                flush=True,
            )
            return None

        # Watch for the content request the viewer makes on its own. Set
        # up before navigating: the viewer can issue it during load, and a
        # listener attached afterwards would miss it.
        pdf_url: list[str] = []

        def _on_response(resp) -> None:
            if _CONTENT_HOST in resp.url and not pdf_url:
                pdf_url.append(resp.url)

        page.on("response", _on_response)
        try:
            try:
                await page.goto(target_url, wait_until="commit", timeout=30000)
            except Exception as e:
                counter.failed += 1
                self.last_error = str(e)
                print(
                    f"  {progress_tag(counter, total, t_start)} "
                    f"FAIL: goto {str(e)[:60]}",
                    flush=True,
                )
                return None

            deadline = self.response_timeout_ms / 1000
            waited = 0.0
            while not pdf_url and waited < deadline:
                await asyncio.sleep(0.25)
                waited += 0.25
        finally:
            with contextlib.suppress(Exception):
                page.remove_listener("response", _on_response)

        if not pdf_url:
            counter.failed += 1
            arrived = _VIEWER_MARKER in (page.url or "")
            hint = (
                "viewer loaded but served no PDF (article may be "
                "abstract-only here)" if arrived
                else f"never reached the viewer (stopped at {page.url[:70]})"
            )
            print(
                f"  {progress_tag(counter, total, t_start)} FAIL: {hint}",
                flush=True,
            )
            return None

        # The signed URL needs no browser, but fetch through `ctx.request`
        # anyway: it shares the context's proxy configuration, so an
        # EZproxy-scoped session keeps working.
        url = pdf_url[0]
        try:
            resp = await ctx.request.get(url, timeout=60000)
            body = await resp.body()
        except Exception as e:
            counter.failed += 1
            self.last_error = str(e)
            print(
                f"  {progress_tag(counter, total, t_start)} "
                f"FAIL: fetch {str(e)[:60]}",
                flush=True,
            )
            return None

        # Full validation, not just a `%PDF-` header check: EBSCO serves
        # through a CDN and a truncated body would otherwise be cached and
        # attached, which is the failure mode this repo has hit before.
        defect = _pdf_validate.pdf_defect(body)
        if defect is not None:
            counter.failed += 1
            print(
                f"  {progress_tag(counter, total, t_start)} "
                f"FAIL: {defect}",
                flush=True,
            )
            return None

        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(body)
        counter.ok += 1
        title = (item.get("title") or "")[:50]
        print(
            f"  {progress_tag(counter, total, t_start)} "
            f"ok ({len(body) // 1024}KB) {title}",
            flush=True,
        )
        return out, url
