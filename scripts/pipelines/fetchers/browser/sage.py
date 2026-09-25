"""Sage — SAGE Journals.

Sage blocks sessions that exceed ~30 requests/minute with an HTTP 429
or session reset. Keep concurrency at 1 and a 2.5-second delay between
requests as the safe default; the user can override by editing the
class attributes if their institutional agreement permits more.
"""

from __future__ import annotations

from .base import PageNavigationHandler


class SageHandler(PageNavigationHandler):
    """Downloads by navigating the page to the PDF URL.

    Was a `RequestHandler` (`ctx.request.get`), and on 2026-09-23 failed
    88 of 88 with Cloudflare's "Just a moment…" while the same window
    showed the article with no challenge: the clearance the page holds
    does not carry to Playwright's separate request client. Navigation
    uses the page's own connection, as for T&F, Wiley and AoM.
    """

    name = "sage"
    display_name = "Sage"
    # `10.2190` = Baywood Publishing, acquired by Sage in 2015; its
    # back-catalogue serves from journals.sagepub.com.
    doi_prefixes = ("10.1177/", "10.2190/")
    url_template = "https://journals.sagepub.com/doi/pdf/{doi}?download=true"
    # Landing page for setup — opening the PDF URL directly triggers
    # a Chromium auto-download that consumes the session and leaves
    # the user with about:blank. (For `download()` that auto-download
    # is the point: `PageNavigationHandler` waits for it.)
    setup_url_template = "https://journals.sagepub.com/doi/{doi}"
    direct_access_domains = ("sagepub.com",)
    concurrency = 1
    delay_s = 2.5

    # Seen 2026-09-25 at JYU (10.1258/mlj.2011.011026 and two more). It
    # sits in the sign-in drawer, hidden until opened, and names the
    # recognised institution, so it needs no second marker. Not
    # "Restricted access": Sage labels the article type with it.
    denial_markers = (r"does not have access to this article",)
