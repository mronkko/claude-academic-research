"""APA PsycNET — psycnet.apa.org.

APA gates every article behind an access check. `https://doi.org/{doi}`
redirects to `psycnet.apa.org/doiLanding?doi=...`, an Angular page that
renders its controls client-side and carries the article's PsycNET
accession number in the record link (`/record/2015-01016-001?doi=1`).
That accession number is the key to everything: the full text lives at
`/fulltext/{accession}.pdf`.

Two routes, in order:

  1. **Direct full-text URL.** Read the accession number off the landing
     page and navigate to `/fulltext/{accession}.pdf`. An entitled
     session downloads it outright (the Chromium profile has the
     built-in PDF viewer disabled, so navigation fires a download
     event). An unentitled one is bounced back to `/record/{accession}`
     and no download event fires — cheap to detect, and it skips three
     failure points.

  2. **Click-through.** "Get Access" opens an overlay offering
     "CHECK ACCESS" and "PURCHASE PDF". CHECK ACCESS navigates to
     `sso.apa.org/apasso/idm/login?CheckAccess=1&UID={accession}
     &ERIGHTS_TARGET={landing}` — the IdP. A session APA already
     recognises (campus IP, prior OpenAthens/SSO login) passes straight
     back through to the ERIGHTS_TARGET with entitlement, at which point
     route 1 works; one that it does not recognise *stops on the login
     form*.

**Correction, verified in a shared browser session on 2026-08-18.** This
docstring used to say `/recordAccess/institutional/**` was "a URL
PsycNET no longer produces". It produces exactly that URL, and the whole
retrieval hinges on it. Recording that as fact is what sent later work
hunting for the PDF anywhere but the page that actually has it, and cost
five rounds of wrong diagnoses — entitlement, markup drift, cookie
consent — for a fault that was none of those.

What CHECK ACCESS does, watched step by step on an entitled session:

    /record/<accession>
      -> click "Get Access"       (an <a href="#">, rendered ~6s in)
      -> overlay "Access Options" with three controls
      -> click "Check Access"
      -> navigates to /recordAccess/institutional/<accession>?returnUrl=...
         (a full page titled "APA PsycNet Record Access", no dialog —
          its card merely resembles the overlay it came from, which is
          why it gets described as "the same overlay")
      -> that page carries the only URL that downloads:
         /fulltext/<accession>.pdf?auth_id=<n>&returnUrl=...

The bare `/fulltext/<accession>.pdf` is not a substitute: without the
per-session `auth_id` it redirects to `/record/<accession>`.

The overlay's three controls share one per-article id stem, and the
close button sorts first:

    psycnet-check-access-close_651260    <- the x
    psycnet-check-access-yes_651260      <- CHECK ACCESS
    psycnet-purchase-access-yes_651260   <- Purchase PDF ($19.95)

A selector of `button[id^='psycnet-check-access']` therefore clicks the
x, dismisses the overlay, and reports success. That was the actual bug.

An unentitled session stops at the `sso.apa.org` login form instead;
that case is detected and reported as itself. Institutional SSO cookies
persist across DOIs, so at most the first item of a run needs a human.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import unquote, urljoin

from .base import (
    Counter,
    PublisherHandler,
    cache_path_for,
    is_cached,
    progress_tag,
    try_click,
)

#: PsycNET accession number, e.g. `2015-01016-001` or `2011-19052-001A`.
#: Lifted from the landing page's record link — it is the only per-article
#: identifier PsycNET's full-text URLs accept.
_RECORD_ID_RE = re.compile(r"/record/(\d{4}-\d{4,6}-\d{3}[A-Za-z]?)")

#: The same accession, as it appears in the RightsLink permissions URL
#: (`http://rightslink.apa.org/journal/2015-01016-001`). Used as an
#: independent second witness — see `_wait_for_landing_record_id`.
_PERMISSIONS_ID_RE = re.compile(r"rightslink\.apa\.org/\w+/(\d{4}-\d{4,6}-\d{3}[A-Za-z]?)")

#: Where the PDF lives once the session is entitled.
_FULLTEXT_URL = "https://psycnet.apa.org/fulltext/{record_id}.pdf"

#: APA's identity provider. Reaching it means the access check did *not*
#: recognise this session — the single most useful thing the log can say.
_SSO_HOST = "sso.apa.org"


class ApaHandler(PublisherHandler):
    name = "apa"
    display_name = "APA PsycNET"
    doi_prefixes = ("10.1037/",)
    url_template = "https://doi.org/{doi}"
    direct_access_domains = ("psycnet.apa.org", "apa.org")
    concurrency = 1
    delay_s = 1.0

    #: How long to wait for the doiLanding view to render *this* DOI's
    #: record link. Generous because the page is Angular-rendered behind
    #: a doi.org redirect chain, and because the alternative to waiting
    #: is reading the previous item's DOM.
    landing_timeout_ms: int = 20000

    setup_hint = (
        "FIRST: answer the cookie banner at the bottom of the page.\n"
        "Accept or reject — either records a decision, and either is\n"
        "fine for retrieval. This plugin deliberately will not answer it\n"
        "for you: consent has to be your act, not the tool's. Answering\n"
        "it also stops the banner overlaying controls lower down. Your\n"
        "choice persists in this browser profile and covers every later\n"
        "APA item.\n"
        "\n"
        "THEN: EXPECT TO SIGN IN ONCE PER RUN. Unlike the Cloudflare-gated\n"
        "publishers, whose clearance cookie is saved in the browser\n"
        "profile, APA's session cookie does not survive closing the\n"
        "browser — a fresh run starts signed out even though the last\n"
        "one ended signed in.\n"
        "On the page in the browser window, click 'Get Access' and then\n"
        "'CHECK ACCESS'. If that lands you on an sso.apa.org login form,\n"
        "sign in there (OpenAthens, institutional email, or your APA\n"
        "account) — otherwise every download this run will stop at that\n"
        "same form. You are ready when the address bar shows\n"
        "psycnet.apa.org/record/... rather than /doiLanding: that\n"
        "redirect is APA confirming the session is entitled.\n"
        "The sign-in covers every APA item in the run, not just this one."
    )

    async def download(
        self, page, ctx, item, cache_dir,
        *, counter: Counter, total: int, t_start: float,
    ) -> tuple[Path, str] | None:
        del ctx
        doi = item["doi"]
        out = cache_path_for(cache_dir, doi)
        if is_cached(out):
            counter.cached += 1
            return out, f"cache://{out}"

        url = self.url_template.format(doi=doi)
        source_url = url
        try:
            # Step 1: doi.org → doiLanding, then wait for Angular to put
            # the record link in the DOM. A fixed sleep here used to be
            # a coin-flip on a slow load.
            # Blank the page first. Everything below identifies the
            # article from what the browser is showing, and `goto()`
            # returns before Angular has swapped the view in — so
            # without this, the previous item's DOM is still readable
            # and gets mistaken for this one's. A live run fetched a
            # cited paper's buy page that way. Starting from
            # `about:blank` makes the mix-up impossible rather than
            # merely detectable.
            try:
                await page.goto(
                    "about:blank", wait_until="domcontentloaded", timeout=10000,
                )
            except Exception:
                pass
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            record_id = await _wait_for_landing_record_id(
                page, doi, timeout_ms=self.landing_timeout_ms,
            )
            if not record_id:
                raise RuntimeError(
                    f"PsycNET never rendered its own record link for {doi} "
                    f"(last seen at {page.url}) — the DOI may not resolve to "
                    f"an article record"
                )
            await _dismiss_cookie_banner(page)
            fulltext_url = _FULLTEXT_URL.format(record_id=record_id)

            # Step 2: the direct full-text URL. Works whenever the
            # session is already entitled, which after the first item of
            # a run is the normal case.
            if await _download_from(page, fulltext_url, out):
                source_url = fulltext_url
            else:
                # Step 3: the access check. Its job is to turn an
                # unentitled session into an entitled one; where it
                # leaves us is the diagnosis.
                state = await _run_access_check(page)
                if state == "sso":
                    raise RuntimeError(
                        f"APA's access check sent this session to the "
                        f"{_SSO_HOST} login form instead of granting access "
                        f"to record {record_id}. The browser is not signed in "
                        f"to APA (or is off the entitled network). Sign in in "
                        f"the open browser window — OpenAthens, institutional "
                        f"email, or an APA account — and re-run; the session "
                        f"then covers every remaining APA item."
                    )
                if state == "no-access-control":
                    raise RuntimeError(
                        f"'Get Access' never appeared on "
                        f"{page.url} for record {record_id} within "
                        f"{_GET_ACCESS_TIMEOUT_MS // 1000}s. Measured live, "
                        f"PsycNET renders it as an <a href='#'> about six "
                        f"seconds after domcontentloaded, so this is a "
                        f"timing or routing problem before it is a markup "
                        f"one — check the saved screenshot before assuming "
                        f"the selectors are stale"
                    )
                if state == "no-navigation":
                    raise RuntimeError(
                        f"CHECK ACCESS was clicked for record {record_id} "
                        f"and the page did not move (still at {page.url}). "
                        f"That is not an entitlement answer — PsycNET never "
                        f"ran the check. The known cause is the click "
                        f"landing on a control that is not the affirmative "
                        f"one; the modal's close button shares the "
                        f"`psycnet-check-access` id prefix. Re-run with "
                        f"scripts/dev/probe_browser_handler.py "
                        f"--handler apa --keep-open and watch which control "
                        f"the overlay loses."
                    )
                if state == "no-check-access":
                    raise RuntimeError(
                        f"'Get Access' opened but the overlay offered no "
                        f"'CHECK ACCESS' control for record {record_id} — "
                        f"usually means purchase is the only option offered "
                        f"for this article"
                    )

                # Access check came back without a detour to the IdP, so
                # we are standing on `/recordAccess/institutional/...`.
                # That page mints the signed link — try it first: the
                # bare `/fulltext/` URL below cannot work on its own,
                # because the real one carries a per-session `auth_id`.
                signed = await _download_from_access_page(page, out)
                if signed:
                    source_url = signed
                # Retry the full-text URL, then fall back to whatever
                # PDF control the resulting page offers.
                elif await _download_from(page, fulltext_url, out):
                    source_url = fulltext_url
                elif await _click_pdf_control(page, out):
                    source_url = page.url
                else:
                    raise RuntimeError(
                        f"APA's access check completed for record "
                        f"{record_id} but neither {fulltext_url} nor any PDF "
                        f"control on the resulting page produced a download "
                        f"— your institution likely has no entitlement to "
                        f"this article"
                    )
        except Exception as e:
            await self.report_failure(
                e, counter=counter, total=total, t_start=t_start,
                page=page, cache_dir=cache_dir, doi=doi,
            )
            return None

        if not is_cached(out):
            try:
                out.unlink(missing_ok=True)
            except Exception:
                pass
            counter.failed += 1
            title = (item.get("title") or "")[:45]
            print(
                f"  {progress_tag(counter, total, t_start)} "
                f"not a PDF {title}",
                flush=True,
            )
            return None

        counter.ok += 1
        size = out.stat().st_size
        title = (item.get("title") or "")[:50]
        print(
            f"  {progress_tag(counter, total, t_start)} "
            f"ok ({size // 1024}KB) {title}",
            flush=True,
        )
        return out, source_url


# ---------------------------------------------------------------------------
# Flow steps. Each is separately observable so a failure names its own
# step rather than surfacing as the last one.
# ---------------------------------------------------------------------------


async def _cookie_banner_present(page) -> bool:
    """True when OneTrust is still asking, i.e. no choice is recorded."""
    try:
        return bool(await page.evaluate(
            "() => !!document.querySelector('#onetrust-banner-sdk')"
        ))
    except Exception:
        return False


async def _dismiss_cookie_banner(page) -> bool:
    """Get the consent banner out of the way of the clicks. Never answer it.

    Returns True if the banner was there — which is itself a diagnosis,
    because a browser profile whose owner has answered OneTrust does not
    show it again.

    **This removes the element and does not accept.** Consent under
    GDPR/ePrivacy has to be an act of the data subject; a tool clicking
    "Accept All Cookies" and persisting that into a profile is not
    obtaining consent, it is manufacturing a record of one, on behalf of
    every user who installs this plugin. So the banner is answered by a
    human during `setup()` or not at all.

    The cost of that is real and worth stating: APA appears to gate its
    own access-check script on OneTrust *resolving*, so on a profile
    where nobody has answered, CHECK ACCESS clicks cleanly and then does
    nothing. Deleting the element does not substitute for a decision.
    That is why the failure path below names the banner instead of
    guessing at entitlement — the fix is a human answering it once, and
    the choice then persists in the profile for every later run.
    """
    present = await _cookie_banner_present(page)
    try:
        await page.evaluate(
            "() => document.querySelector('#onetrust-consent-sdk')?.remove()"
        )
    except Exception:
        pass
    return present


#: Read in one `evaluate` so the URL and the links are sampled from the
#: same document. Reading them in separate calls leaves a window in
#: which the page navigates between the two — which is precisely the
#: kind of gap that produced the wrong accession number.
_LANDING_PROBE_JS = """
() => JSON.stringify({
  url: location.href,
  marked: document.querySelector(
    "a[href*='/record/'][href*='doi=1']")?.getAttribute('href') || null,
  perms: document.querySelector(
    "a.permissions-link")?.getAttribute('href') || null,
})
"""


def _is_landing_for(url: str, doi: str) -> bool:
    """True when `url` is the doiLanding page for exactly this DOI.

    The DOI is carried in the query string (`?doi=10.1037%2Fapl0000007`,
    occasionally double-encoded through the SSO round trip), so matching
    on it ties the page we are reading to the item we were asked for.
    Any other page — a stale one from the previous item, a record page, a
    buy page — fails this and we keep waiting.
    """
    if "doiLanding" not in url:
        return False
    decoded = unquote(unquote(url)).lower()
    return doi.lower() in decoded


async def _wait_for_landing_record_id(
    page, doi: str, timeout_ms: int = 20000,
) -> str:
    """Poll until this DOI's landing page yields its own accession number.

    `doi.org` lands in one of two places depending on the session, and
    both were observed live:

    - **Signed in and entitled** → straight to `/record/{accession}`,
      never showing the landing page at all. The accession is then in
      the URL, which is the least ambiguous source there is. (This is
      what the old docstring's "auto-routed past this step" meant.)
    - **Not entitled** → `/doiLanding?doi=...`, where the accession has
      to come from the record link marked `?doi=1`. That marker is how
      the page distinguishes *the article this DOI resolved to* from the
      records it merely cites, and it is not optional: the first
      unmarked `/record/` anchor on an article page is a reference. A
      live run took one and fetched `2010-04200-005` — a cited paper —
      for a DOI whose record is `2015-01016-001`, landing on that
      paper's $19.95 buy page.

    Either way the URL must belong to *this* DOI, and RightsLink — an
    independent second witness to the accession — must agree when it is
    present. Disagreement raises rather than guesses.

    Guessing here would not be a failed download; it would be the wrong
    PDF attached to the right Zotero item, which no later stage checks.
    Returns "" on timeout; raises only on an outright contradiction.
    """
    deadline = time.monotonic() + timeout_ms / 1000.0
    while True:
        data: dict = {}
        try:
            data = json.loads(await page.evaluate(_LANDING_PROBE_JS))
        except Exception:
            data = {}          # mid-navigation; the next tick re-reads

        url = str(data.get("url") or "")
        candidate = ""
        # Entitled path: the redirect chain ended on the record itself.
        # Safe to trust because `download()` blanks the page first, so a
        # `/record/` URL can only have come from this DOI's redirect.
        in_url = _RECORD_ID_RE.search(url)
        if in_url:
            candidate = in_url.group(1)
        elif _is_landing_for(url, doi):
            marked = _RECORD_ID_RE.search(str(data.get("marked") or ""))
            if marked:
                candidate = marked.group(1)

        if candidate:
            perms = _PERMISSIONS_ID_RE.search(str(data.get("perms") or ""))
            if perms and perms.group(1) != candidate:
                raise RuntimeError(
                    f"PsycNET page for {doi} is inconsistent: it identifies "
                    f"the article as {candidate} but its permissions link "
                    f"says {perms.group(1)}. Refusing to guess which article "
                    f"this is rather than risk fetching the wrong PDF"
                )
            return candidate

        if time.monotonic() >= deadline:
            return ""
        await page.wait_for_timeout(250)


async def _download_from(page, url: str, out: Path) -> bool:
    """Navigate to `url` and save the download it fires, if any.

    False means no download event arrived — for `/fulltext/*.pdf` that is
    PsycNET redirecting an unentitled session back to the record page,
    which is a routine outcome rather than an error. The 20s budget is
    deliberately shorter than the 30s used elsewhere: this is a probe
    that the caller has a fallback for, not the last attempt.
    """
    try:
        async with page.expect_download(timeout=20000) as dl_info:
            try:
                await page.goto(url, wait_until="commit", timeout=15000)
            except Exception:
                pass  # expected — the download event interrupts navigation
        dl = await dl_info.value
        out.parent.mkdir(parents=True, exist_ok=True)
        await dl.save_as(str(out))
    except Exception:
        return False
    return is_cached(out)


#: The page "Check Access" navigates to once the institution is
#: recognised. Reaching it is success, not failure — it is where the
#: signed link is minted. See `_download_from_access_page`.
#:
#: NOT where a bare `/fulltext/<id>.pdf` lands: measured live, that
#: redirects to `/record/<id>`. An earlier fix keyed the whole
#: institutional-access branch off this path immediately after the
#: `/fulltext/` probe and was therefore dead code.
_ACCESS_PATH = "/recordaccess/institutional/"

#: How long to wait for "Get Access". PsycNET's record page is a JS app
#: that renders it as an `<a href="#">` roughly six seconds after
#: domcontentloaded — measured against the live site. The old 4s budget
#: expired before the control existed, and the miss was reported as
#: "PsycNET's landing markup has changed", which sent debugging after
#: selectors that were correct all along.
_GET_ACCESS_TIMEOUT_MS = 20000


async def _download_from_access_page(page, out: Path) -> str:
    """Take the signed PDF link off PsycNET's institutional-access page.

    `_FULLTEXT_URL` builds `/fulltext/<record_id>.pdf`, which is not a
    complete URL: the real one carries an `auth_id` minted per session,
    e.g. `/fulltext/1997-06412-019.pdf?auth_id=4168&returnUrl=...`.
    Navigating to the bare form therefore never downloads — PsycNET
    redirects to `/recordAccess/institutional/<record_id>`, which
    verifies the institution and *then* renders a "Download PDF" link
    holding the signed URL.

    That redirect is what made this look broken on a live JYU run. The
    caller treated the failed `/fulltext/` probe as "not entitled" and
    ran `_run_access_check` on the page it had landed on — but that page
    is the access modal, not the record page, so there was no "Get
    Access" control and the handler reported the markup as changed. The
    screenshot told the real story: "Your access to this content through
    Jyvaskylan yliopisto has been verified!" above a spinner.

    The link is populated by JS several seconds after arrival, so the
    wait here is generous. Returns the signed URL on success, "" if the
    link never appeared.
    """
    # The access page is a fresh document too.
    await _dismiss_cookie_banner(page)
    try:
        link = page.locator(
            "a[href*='/fulltext/'][href*='.pdf']"
        ).first
        await link.wait_for(state="attached", timeout=25000)
        href = await link.get_attribute("href")
    except Exception:
        return ""
    if not href:
        return ""
    url = urljoin(page.url or "https://psycnet.apa.org/", href)
    return url if await _download_from(page, url, out) else ""


async def _run_access_check(page) -> str:
    """Drive "Get Access" → "CHECK ACCESS" and report where it landed.

    Returns one of:
      - ``"sso"``               — stopped at APA's login form; the
                                  session is not entitled and no
                                  selector fix can change that.
      - ``"granted"``           — the check completed and returned to
                                  PsycNET; the caller retries the PDF.
      - ``"no-access-control"`` — no "Get Access" on the page at all.
      - ``"no-check-access"``   — overlay opened, but offered no access
                                  check (usually purchase-only).
      - ``"no-navigation"``     — CHECK ACCESS clicked and the page
                                  stayed put. Neither entitlement nor
                                  consent: historically this meant the
                                  click had gone to the wrong control.
    """
    # Again, because the caller's dismissal happened before the
    # `/fulltext/` probe — and that probe navigates (to `/fulltext/`,
    # bounced back to `/record/`), which reloads the document and brings
    # the banner back. Every click below therefore happened with the
    # banner present, on a cold profile, however diligently it was
    # removed earlier. A live cold-profile probe failed both items and
    # its screenshot showed the banner still there at the end.
    banner_was_up = await _dismiss_cookie_banner(page)

    opened = await try_click(
        page,
        # `title`/`aria-label` are the stable handles: the visible text
        # lives in a nested <span translate="">, and the class list
        # varies per user. `button.getAccessButton` matched nothing on
        # the live page and is kept only as a cheap legacy fallback.
        "a[title='Get Access']",
        "a[aria-label='Get Access']",
        "a.list-group-item.pdf",
        "button.getAccessButton",
        "button:has-text('Get Access')",
        "a:has-text('Get Access')",
        timeout=_GET_ACCESS_TIMEOUT_MS,
    )
    if not opened:
        return "no-access-control"

    # The affirmative control only. Its id is per-article
    # (`psycnet-check-access-yes_760346`) — but so is the modal's own
    # close button (`psycnet-check-access-close_760346`), and the old
    # prefix `psycnet-check-access` matched that one first. It is
    # visible, so the click landed on it, dismissed the modal, and
    # reported success. The access check never ran; the flow then spent
    # 60s looking for a PDF on a page that had gone nowhere and blamed
    # the institution's entitlements.
    #
    # Playwright's strict mode is what finally showed it — a plain
    # `.first`-style sweep silently prefers whichever comes first in the
    # DOM, and that is the close button.
    checked = await try_click(
        page,
        "button[id*='check-access-yes']",
        "button[title='Check Access']",
        "button[aria-label='Check Access']",
        "button:has-text('Check Access')",
        timeout=6000,
    )
    if not checked:
        return "no-check-access"

    # CHECK ACCESS navigates: an entitled session lands on
    # `/recordAccess/institutional/<id>`, an unentitled one stops at the
    # IdP login form.
    #
    # The predicate must name those two destinations. It used to accept
    # any `psycnet.apa.org` URL — which is where we already are, so
    # `wait_for_url` returned immediately and never waited for the
    # navigation at all. Everything downstream then ran against the
    # record page: no signed link to find, the bare `/fulltext/` retry
    # bounced back to `/record/`, and the run reported "your institution
    # likely has no entitlement to this article" about an article the
    # institution demonstrably had.
    try:
        await page.wait_for_url(
            lambda u: _SSO_HOST in u or _ACCESS_PATH in (u or "").lower(),
            timeout=15000,
        )
    except Exception:
        pass
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass

    current = ""
    try:
        current = page.url or ""
    except Exception:
        pass
    if _SSO_HOST in current:
        return "sso"
    if _ACCESS_PATH in current.lower():
        return "granted"
    # Clicked, and the page did not move. This was briefly attributed to
    # the consent banner — a cold profile showed one and failed, a warm
    # profile did not and worked. That correlation was real and the
    # cause was not: with the banner answered, the run failed
    # identically. The actual cause was the click landing on the modal's
    # close button. Kept as its own state so a future non-navigation is
    # not silently folded into "granted" again.
    del banner_was_up
    return "no-navigation"


async def _click_pdf_control(page, out: Path) -> bool:
    """Click whatever PDF control the post-access-check page offers.

    Last resort behind the direct full-text URL, for the case where
    PsycNET hands back a page whose PDF link is session-bound rather
    than the plain `/fulltext/` route.
    """
    try:
        async with page.expect_download(timeout=25000) as dl_info:
            clicked = await try_click(
                page,
                "a[href*='/fulltext/'][href*='.pdf']",
                "a.list-group-item.pdf[href*='/fulltext/']",
                "button:has-text('Download PDF')",
                "a:has-text('Download PDF')",
                timeout=5000,
            )
            if not clicked:
                raise RuntimeError("no PDF control on the page")
        dl = await dl_info.value
        out.parent.mkdir(parents=True, exist_ok=True)
        await dl.save_as(str(out))
    except Exception:
        return False
    return is_cached(out)
