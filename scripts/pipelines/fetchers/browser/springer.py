"""Springer Nature — link.springer.com.

Exists because the HTTP cascade cannot reach Springer at all.
`fetchers/springer.py` builds the right URL and is correctly entitled,
but SpringerLink answers any non-browser client with a ~3 KB HTML page
titled `Client Challenge` — an Imperva JavaScript interstitial. Measured
from an on-campus IP across ten DOIs including licensed titles, and
unchanged by a complete browser header set: the same 3038 bytes every
time. The challenge is served *before* entitlement is evaluated, so
being on the VPN makes no difference, and Crossref's TDM record points
at the same URL and so fails identically.

A real browser runs the JavaScript and clears the challenge — verified
live: the article page rendered with the PDF reachable and no human
action needed at all.

**But `ctx.request` does not inherit that clearance.** This handler was
first written as a `RequestHandler`, on the theory that Playwright's
request client shares the browser context's cookie jar and so would
inherit the cookie, exactly as Sage and Emerald do for Cloudflare. Tried
live on 2026-08-17: the page was clear, and `ctx.request.get()` still
came back HTTP 200 with the 3038-byte `Client Challenge` body. So
Imperva binds clearance to more than the cookie, and `PageNavigationHandler`
— a real `page.goto` plus download event — is the only route that works.
That base class exists for precisely this, per its own docstring:
publishers whose bot wall rejects `ctx.request` even with valid cookies
(Taylor & Francis, Wiley, AoM). Do not "optimise" this back to request
mode; it has been measured.

Downloads work because the bundled Chromium profile sets
`plugins.always_open_pdf_externally`, so navigating to a PDF URL fires a
download event instead of opening the built-in viewer.

`setup_url_template` still points at the article page rather than the
PDF: during setup there is no `expect_download` in flight, so navigating
straight to the PDF would fire an auto-download that consumes the session
and leave the user staring at about:blank. Same reasoning as Sage's.

DOI prefixes are imported from the HTTP source rather than retyped, so
the two cannot drift apart on which DOIs count as Springer.
"""

from __future__ import annotations

from fetchers.springer import _SPRINGER_PREFIXES

from .base import PageNavigationHandler


class SpringerHandler(PageNavigationHandler):
    name = "springer"
    display_name = "Springer Nature"
    doi_prefixes = _SPRINGER_PREFIXES
    url_template = "https://link.springer.com/content/pdf/{doi}.pdf"
    # Landing page for the interactive solve. Opening the PDF URL
    # directly triggers a Chromium auto-download that consumes the
    # session and leaves the user looking at about:blank — the same
    # reason Sage points its setup at the article page.
    setup_url_template = "https://link.springer.com/article/{doi}"
    direct_access_domains = ("link.springer.com", "springer.com")
    # Page-navigation mode serialises through the single page regardless,
    # and Imperva rate-limits aggressively once it is watching a session.
    concurrency = 1
    delay_s = 1.5
