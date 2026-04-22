"""Emerald — Emerald Insight (business/management journals).

Uses real page navigation (`page.goto()` + `expect_download`) because
Emerald's Cloudflare distinguishes Chrome from Playwright's `ctx.request`
client on TLS fingerprint + User-Agent even when cookies are shared —
empirically reproduced 2026-04-21 against 10.1108/ijchm-04-2020-0259
where the browser landing page loaded fine but `ctx.request.get(PDF_URL)`
returned 403 with a Cloudflare challenge body.

Page-nav is serial but matches the browser's fingerprint, so CF lets
the request through.
"""

from __future__ import annotations

from .base import PageNavigationHandler


class EmeraldHandler(PageNavigationHandler):
    name = "emerald"
    display_name = "Emerald"
    doi_prefixes = ("10.1108/",)
    url_template = (
        "https://www.emerald.com/insight/content/doi/{doi}/full/pdf?download=true"
    )
    # Landing page for setup — the download URL would auto-download and
    # strand the user at about:blank (Chromium profile has
    # always_open_pdf_externally=true).
    setup_url_template = "https://www.emerald.com/insight/content/doi/{doi}"
    direct_access_domains = ("emerald.com",)
    concurrency = 1
    delay_s = 1.0

    setup_hint = (
        "Wait until you see the article landing page (abstract, journal\n"
        "masthead, 'Download as PDF' button). Solve any Cloudflare\n"
        "challenge and sign in if prompted. Only press Enter once the\n"
        "page is fully loaded — the session needs to be authenticated\n"
        "before the downloads start."
    )
