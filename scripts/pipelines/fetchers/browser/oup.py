"""Oxford University Press — academic.oup.com.

OUP's PDF URL contains an opaque numeric article ID that isn't
derivable from the DOI, so we can't construct the URL directly. The
shared `PdfLinkNavigationHandler` flow handles this: navigate
`https://doi.org/{doi}` (redirects to academic.oup.com), extract the
PDF anchor's href, navigate to it to fire the download event.

Flow originally ported verbatim from the working SLR-motivation
script; generalised into the shared base when AAA (the other
Silverchair platform) needed the identical treatment.
"""

from __future__ import annotations

from .base import PdfLinkNavigationHandler


class OupHandler(PdfLinkNavigationHandler):
    name = "oup"
    display_name = "Oxford University Press"
    doi_prefixes = ("10.1093/",)
    url_template = "https://doi.org/{doi}"
    direct_access_domains = ("academic.oup.com", "oup.com")
    concurrency = 1
    delay_s = 1.0
