"""Wiley — onlinelibrary.wiley.com (browser fallback).

This is the fallback path for Wiley DOIs not covered by the
institution's Text and Data Mining (TDM) contract; prefer
`fetchers.wiley.WileySource` (which uses `wiley-tdm`) when the TDM
token authorizes the paper.

Wiley's Cloudflare rejects `ctx.request`; page navigation is required.
"""

from __future__ import annotations

from .base import PageNavigationHandler


class WileyHandler(PageNavigationHandler):
    name = "wiley"
    display_name = "Wiley (fallback)"
    # Wiley prefixes: 10.1002 (most), 10.1111 (some), 10.1046 (legacy).
    doi_prefixes = ("10.1002/", "10.1111/", "10.1046/")
    url_template = "https://onlinelibrary.wiley.com/doi/pdf/{doi}?download=true"
    # Open the article landing page during setup so the user can see
    # the abstract, the sign-in prompt, and the Cloudflare challenge.
    # Opening `/doi/pdf/...?download=true` directly triggers an auto-
    # download (Chromium profile has always_open_pdf_externally=true)
    # and leaves the browser on about:blank before the user can act.
    setup_url_template = "https://onlinelibrary.wiley.com/doi/{doi}"
    direct_access_domains = ("onlinelibrary.wiley.com", "wiley.com")
    concurrency = 1
    delay_s = 1.0

    setup_hint = (
        "Wiley requires institutional access for most journals. If you\n"
        "see the 'Institutional login' option on the sign-in page, use it\n"
        "and complete your institution's SSO. Otherwise every PDF URL\n"
        "will time out without firing a download event.\n"
        "Prefer the Wiley TDM path (--sources wiley) when your institution\n"
        "has a TDM agreement — it's faster and doesn't need a browser."
    )
