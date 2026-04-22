"""Academy of Management — journals.aom.org.

AoM fronts every PDF URL with Cloudflare. Once the user has solved the
challenge in the browser window, page navigation to the `/doi/pdf/`
endpoint reliably fires a download event (with the
`plugins.always_open_pdf_externally` profile pref set, which is handled
by `base.launch_context`).

Institutional access is required — AoM content is not openly available.
"""

from __future__ import annotations

from .base import PageNavigationHandler


class AomHandler(PageNavigationHandler):
    name = "aom"
    display_name = "Academy of Management"
    doi_prefixes = ("10.5465/",)
    url_template = "https://journals.aom.org/doi/pdf/{doi}?download=true"
    # Open the article landing page during setup. Opening the PDF URL
    # directly auto-downloads and leaves the browser at about:blank
    # (see EmeraldHandler for the same reasoning).
    setup_url_template = "https://journals.aom.org/doi/{doi}"
    direct_access_domains = ("journals.aom.org", "aom.org")
    concurrency = 1
    delay_s = 1.0

    setup_hint = (
        "AoM is usually TWO gates, not one:\n"
        "  (a) Cloudflare challenge (a click-through or nothing visible),\n"
        "  (b) Sign-in with your AoM member account (or your institution's\n"
        "      SSO, via 'Institutional Log In' on the AoM sign-in page).\n"
        "Without the AoM sign-in, you'll hit a paywall instead of the PDF."
    )
