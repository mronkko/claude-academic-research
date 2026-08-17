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
import re
from urllib.parse import parse_qs, quote, urlparse, urlunparse

from fetchers import _pdf_validate
from fetchers.resolvers.base import needs_interactive_login

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

# --- Identifier re-query ---------------------------------------------
#
# The commonest residual failure on this handler is not a block: EBSCO
# answers, but with a search page instead of the article. Alma hands it
# an OpenURL carrying journal, year and title, and EBSCO turns that into
# `(SO <journal>)AND(DT <year>)AND(TI <title>)` — a query that can
# exclude the very article sitting in the database. Measured live on
# `10.1287/mnsc.2017.2869`: Crossref and the DOI say 2017 (online-first),
# EBSCO holds it as May 2019, so `DT 2017` returned zero and EBSCO fell
# back to SmartText — 298 fuzzy hits, correct article at rank 1.
# Searching `DI "10.1287/mnsc.2017.2869"` instead returns 1-1 of 1.
#
# So the re-query asks by identifier, and the identifier is the one thing
# in the OpenURL that cannot drift: the DOI.

#: Where a lane stops when the OpenURL identified the journal but not the
#: article. Not verdicts — this is where the re-query starts.
_SEARCH_LANDING_MARKERS: tuple[str, ...] = (
    "/search/results", "/search/advanced", "/detailv2",
)

#: EBSCO's fuzzy fallback, and the reason a count cannot be read off a
#: page carrying it: those hits answer a different question than the one
#: asked. 298 of them, with the right article at rank 1, is not evidence
#: — and picking from them would attach the wrong paper to a citation,
#: which is worse than attaching nothing.
_SMARTTEXT_MARKERS: tuple[str, ...] = ("smarttext",)

_NO_RESULTS_MARKERS: tuple[str, ...] = (
    "no results were found", "no results found", "no results",
)

#: EBSCO's own "1 - 1 of 1" / "1 - 20 of 298" result counter.
_RESULT_RANGE_RE = re.compile(
    r"\b\d[\d,]*\s*[-–—]\s*\d[\d,]*\s+of\s+(\d[\d,]*)\b",
    re.IGNORECASE,
)

#: EBSCO answered the identifier query with nothing. An earned verdict
#: about this route: the resolver advertised an EBSCOhost route that
#: EBSCO cannot honour for this tenant, i.e. stale holdings.
VERDICT_NO_HOLDINGS = "no_holdings"
#: Exactly one record matched the DOI — unambiguous, safe to open.
VERDICT_UNIQUE = "unique_record"
#: More than one. Do not guess which.
VERDICT_AMBIGUOUS = "ambiguous_records"
#: The page said "no exact match" but no re-query could be built from it,
#: so nothing confirmed it. Deliberately distinct from NO_HOLDINGS: one
#: is EBSCO answering about the DOI, the other is a page we could not
#: interrogate.
VERDICT_NO_MATCH_UNCONFIRMED = "no_exact_match_unconfirmed"
#: Nothing legible. The pre-existing "never reached the viewer" outcome.
VERDICT_UNKNOWN = ""


def is_search_landing(url: str) -> bool:
    """True when the page we stopped on is one the re-query starts from."""
    low = (url or "").lower()
    return any(m in low for m in _SEARCH_LANDING_MARKERS)


def requery_url_for(landing_url: str, doi: str) -> str:
    """`DI "<doi>"` against the same profile and database. "" if not buildable.

    Two things are carried over from the page we landed on rather than
    configured, because both are per-tenant and neither is derivable:
    the `c/<profile>` path segment (`c/7dz6k2` for one institution,
    `c/x3kxfd` for another), and `db` when the OpenURL named one.

    **The netloc is carried over too, and that is load-bearing.** Half of
    these routes arrive EZproxy-wrapped as
    `research-ebsco-com.ezproxy.jyu.fi`; rebuilding on the canonical
    `research.ebsco.com` would leave the proxy session behind and turn a
    re-query into a fresh, unauthenticated one.

    Returns "" rather than guessing when there is no profile segment —
    the `openurl.ebsco.com/srh:SRH.../detailv2` pages have none, and a
    fabricated profile id would query some other tenant's view.
    """
    doi = (doi or "").strip()
    if not doi:
        return ""
    parsed = urlparse(landing_url or "")
    host = (parsed.hostname or "").lower()
    if not host or "ebsco" not in host:
        return ""

    segments = [s for s in parsed.path.split("/") if s]
    profile = ""
    for i, seg in enumerate(segments):
        if seg == "c" and i + 1 < len(segments):
            profile = segments[i + 1]
            break
    if not profile:
        return ""

    term = f'DI "{doi}"'
    query = f"q={quote(term, safe='')}"
    db = (parse_qs(parsed.query).get("db") or [""])[0]
    if db:
        query += f"&db={quote(db, safe='')}"
    return urlunparse((
        parsed.scheme or "https", parsed.netloc,
        f"/c/{profile}/search/results", "", query, "",
    ))


def hit_count_from_text(text: str) -> int | None:
    """How many records EBSCO says it found. None when it cannot be read.

    None is not zero, and the distinction is the whole point: zero is an
    answer about the article, None is our failure to read the page. Only
    the first licenses a verdict.

    A page carrying SmartText always reads None, whatever numbers are on
    it — see `_SMARTTEXT_MARKERS`.
    """
    low = (text or "").lower()
    if not low.strip():
        return None
    if any(m in low for m in _SMARTTEXT_MARKERS):
        return None
    if any(m in low for m in _NO_RESULTS_MARKERS):
        return 0
    m = _RESULT_RANGE_RE.search(low)
    if m:
        return int(m.group(1).replace(",", ""))
    return None


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
    # True of the library this was measured against, and not of every
    # library: see `needs_solve_for`, which decides per queue.
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

    #: Seconds to let the re-query's results render. The identifier query
    #: is one request against an app that is already booted, so this is
    #: far shorter than `response_timeout_ms`.
    requery_timeout_ms = 12000

    #: Set by the driver before `setup()` when this queue needs a login.
    #: Per-instance because each lane copies the handler.
    pending_solve_url = ""

    #: What EBSCO said when asked about the DOI itself, if it was asked.
    #: One of the VERDICT_* constants. Read by the orchestrator to
    #: classify the failure; per-instance, like `last_error`, because
    #: each lane carries its own handler copy.
    last_verdict = VERDICT_UNKNOWN

    def _setup_url_for(self, doi: str) -> str:
        """The URL the solve opens.

        Overrides the base hook — note the underscore. This class used
        to carry a public `setup_url_for` marked "unused"; it really was
        unused, because `setup()` calls `_setup_url_for`. Overriding the
        public name instead left the base implementation in charge, and
        that returns `setup_url_template or url_template`, both empty
        here — so `setup()` navigated nowhere and presented a blank page
        to sign in on.

        `doi.org/<doi>` would be no better: it lands on the publisher's
        own site, where there is no institutional login to clear. The
        login lives on the proxy in front of *this queue's* resolver
        target, so the driver stashes that target and it is used here.
        The DOI form remains a fallback for the unrouted case, where
        `setup()` is not called anyway.
        """
        return self.pending_solve_url or f"https://doi.org/{doi}"

    def solve_url_for(self, items: list[dict]) -> str:
        """First queued route that goes through a signing-in proxy."""
        for it in items:
            url = it.get("resolver_target_url", "")
            if needs_interactive_login(url):
                return url
        return ""

    def needs_solve_for(self, items: list[dict]) -> bool:
        """A solve is needed when any queued route goes through a proxy
        that signs the reader in.

        The static `False` above was measured against a single library
        whose EBSCO route authenticates on institutional IP. Once
        `4cead93` let a second library into the merged target list, some
        EBSCO routes started arriving EZproxy-wrapped and landing on a
        SAML IdP — and because `a8b3d8f` had just made `False` genuinely
        skip `setup()`, no login ever happened.

        The failure was silent and total: with `--browser-workers 4`,
        all four lanes opened cold and hit the IdP simultaneously *on
        the same SAML execution token*, which a human cannot solve —
        each tab invalidates the others'. 8 of 14 items died there.
        Serial hit the same wall one item at a time.
        """
        return any(
            needs_interactive_login(it.get("resolver_target_url", ""))
            for it in items
        )

    def proxied_route_count(self, items: list[dict]) -> int:
        """How many queued routes sit behind a signing-in proxy.

        Distinct from `len(solve_hosts_for(...))`, which counts hosts —
        one host commonly fronts most of a queue, so reporting the host
        count as a route count understates the work by an order of
        magnitude.
        """
        return sum(
            1 for it in items
            if needs_interactive_login(it.get("resolver_target_url", ""))
        )

    def solve_hosts_for(self, items: list[dict]) -> list[str]:
        """Proxy hostnames in this queue, for naming them in the prompt.

        With two institutions configured, "sign in" is an ambiguous
        instruction — only one of the two logins opens any given route.
        """
        hosts = {
            (urlparse(it.get("resolver_target_url", "")).hostname or "").lower()
            for it in items
            if needs_interactive_login(it.get("resolver_target_url", ""))
        }
        return sorted(h for h in hosts if h)

    @staticmethod
    async def _await_pdf_url(pdf_url: list[str], timeout_ms: float) -> None:
        """Wait for the viewer to request its own PDF, or time out."""
        deadline = timeout_ms / 1000
        waited = 0.0
        while not pdf_url and waited < deadline:
            await asyncio.sleep(0.25)
            waited += 0.25

    def _failure_hint(self, url: str) -> str:
        """What to print when no PDF arrived.

        The verdicts come first because they are the only lines here that
        say something about the *article*. "never reached the viewer" is
        the residual: true, and almost content-free.
        """
        if self.last_verdict == VERDICT_NO_HOLDINGS:
            return (
                "no holdings — EBSCO returned 0 records for this DOI, so "
                "the resolver is advertising a route it cannot honour "
                "(try ILL or another platform, not a retry)"
            )
        if self.last_verdict == VERDICT_AMBIGUOUS:
            return (
                "DOI search returned more than one record — not guessing "
                "which is the article"
            )
        if self.last_verdict == VERDICT_UNIQUE:
            return (
                "found the record by DOI, but could not reach its PDF "
                "viewer from the result"
            )
        if self.last_verdict == VERDICT_NO_MATCH_UNCONFIRMED:
            return (
                "page reports no exact match through this institution "
                "(unconfirmed — no profile to re-query by DOI)"
            )
        if _VIEWER_MARKER in url:
            return (
                "viewer loaded but served no PDF (article may be "
                "abstract-only here)"
            )
        return f"never reached the viewer (stopped at {url[:70]})"

    @staticmethod
    async def _page_text(page) -> str:
        """Visible text, or "" if the page will not give it up."""
        try:
            return await page.inner_text("body")
        except Exception:  # noqa: BLE001 — a diagnostic must not fail a fetch
            return ""

    async def _read_hit_count(self, page) -> int | None:
        """Poll until EBSCO's count is legible, or the budget runs out.

        The results list renders client-side, so the first read after
        `goto` is usually of an empty shell. Polling for a *legible*
        count rather than sleeping a fixed time keeps the common case
        fast and still gives a slow render its full budget.
        """
        deadline = self.requery_timeout_ms / 1000
        waited = 0.0
        while True:
            count = hit_count_from_text(await self._page_text(page))
            if count is not None:
                return count
            if waited >= deadline:
                return None
            await asyncio.sleep(0.5)
            waited += 0.5

    async def _requery_by_identifier(self, page, doi: str) -> str:
        """Ask EBSCO about the DOI, and take its answer as a verdict.

        Deliberately does *not* read a verdict off the page we landed on.
        A zero there says only that the OpenURL's own query found
        nothing, and that query is the thing under suspicion — `DT 2017`
        excluding an article EBSCO holds under 2019 produces exactly that
        page. Only the identifier query answers about the article.
        """
        landing = page.url or ""
        url = requery_url_for(landing, doi)
        if not url:
            # No profile segment to query against — the
            # `openurl.ebsco.com/srh:SRH.../detailv2` shape. If that page
            # volunteered "no exact match", say so and mark it as the
            # unconfirmed thing it is.
            text = (await self._page_text(page)).lower()
            if "no exact match" in text:
                return VERDICT_NO_MATCH_UNCONFIRMED
            return VERDICT_UNKNOWN

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            return VERDICT_UNKNOWN

        count = await self._read_hit_count(page)
        if count is None:
            return VERDICT_UNKNOWN
        if count == 0:
            return VERDICT_NO_HOLDINGS
        if count > 1:
            return VERDICT_AMBIGUOUS
        return VERDICT_UNIQUE

    async def _open_only_result(self, page) -> bool:
        """Click the sole result of an identifier query.

        Only ever called on a page EBSCO reported as `1 - 1 of 1`, so
        there is nothing to choose between and nothing fuzzy to get
        wrong. **The selectors below are unverified against a live
        results page** — the remaining DOM work in the plan — so this is
        best-effort: if none matches, the caller still reports the
        earned `unique_record` verdict rather than an unexplained stall.
        """
        for selector in (
            "a[href*='/viewer/pdf/']",
            "a[href*='/search/details/']",
            "[data-auto='search-result-title'] a",
        ):
            try:
                link = page.locator(selector).first
                if await link.count() == 0:
                    continue
                await link.click(timeout=5000)
                return True
            except Exception:  # noqa: BLE001 — try the next shape
                continue
        return False

    async def download(self, page, ctx, item, cache_dir, *,
                       counter, total, t_start):
        doi = item["doi"]
        self.last_error = ""
        self.last_verdict = VERDICT_UNKNOWN
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

            await self._await_pdf_url(pdf_url, self.response_timeout_ms)

            # EBSCO answered, but with a search page rather than the
            # article. Ask it about the DOI before giving up — and stay
            # inside the `finally`, so a record opened here is still
            # watched by the listener above.
            if not pdf_url and is_search_landing(page.url or ""):
                self.last_verdict = await self._requery_by_identifier(page, doi)
                if self.last_verdict == VERDICT_UNIQUE:
                    if await self._open_only_result(page):
                        await self._await_pdf_url(
                            pdf_url, self.response_timeout_ms)
        finally:
            with contextlib.suppress(Exception):
                page.remove_listener("response", _on_response)

        if not pdf_url:
            counter.failed += 1
            print(
                f"  {progress_tag(counter, total, t_start)} "
                f"FAIL: {self._failure_hint(page.url or '')}",
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
