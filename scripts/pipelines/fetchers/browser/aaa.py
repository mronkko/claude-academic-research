"""American Accounting Association — publications.aaahq.org.

AAA publishes through a Silverchair-based platform (pubs.aaahq.org).
The `/article-pdf/doi/{doi}` path responds with a PDF when the browser
session has valid institutional cookies. Page navigation is the safer
default until we verify `ctx.request` works cleanly.
"""

from __future__ import annotations

from .base import PageNavigationHandler


class AaaHandler(PageNavigationHandler):
    name = "aaa"
    display_name = "AAA (Accounting Review)"
    doi_prefixes = ("10.2308/",)
    url_template = (
        "https://publications.aaahq.org/accounting-review/article-pdf/doi/{doi}"
    )
    # Landing page for setup — the article-pdf URL would auto-download.
    setup_url_template = (
        "https://publications.aaahq.org/accounting-review/article/doi/{doi}"
    )
    direct_access_domains = ("aaahq.org",)
    concurrency = 1
    delay_s = 1.0
