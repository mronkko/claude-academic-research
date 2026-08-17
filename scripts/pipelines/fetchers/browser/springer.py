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

A real browser runs the JavaScript, gets the clearance cookie, and the
institutional IP entitlement then serves the PDF — which is what this
handler is for. `RequestHandler` fetches through `ctx.request`, which
shares the browser context's cookie jar, so one interactive solve
clears the whole batch. That is the same arrangement Sage and Emerald
use for Cloudflare.

The PDF URL is derivable from the DOI, so no page scraping is needed and
this stays a `url_template` handler. If a future Imperva change binds
clearance to more than the cookie (TLS fingerprint, a JS-set header),
switch the base class to `PageNavigationHandler` — a real `page.goto`
plus download event — rather than trying to defeat the challenge here.

DOI prefixes are imported from the HTTP source rather than retyped, so
the two cannot drift apart on which DOIs count as Springer.
"""

from __future__ import annotations

from fetchers.springer import _SPRINGER_PREFIXES

from .base import RequestHandler


class SpringerHandler(RequestHandler):
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
    # Imperva rate-limits aggressively once it is watching a session.
    # Start conservative; raise only with evidence from a real run.
    concurrency = 1
    delay_s = 1.5
