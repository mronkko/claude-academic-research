"""American Accounting Association — publications.aaahq.org.

AAA publishes through a Silverchair-based platform (same as OUP's
academic.oup.com). The PDF path contains an opaque numeric article ID
(`/article-pdf/doi/{doi}/{id}/{file}.pdf`) that isn't derivable from
the DOI — navigating to the bare `/article-pdf/doi/{doi}` returns
Silverchair's "Your action has resulted in an error" page. The shared
`PdfLinkNavigationHandler` flow handles this: open the article landing
page (which does accept a bare DOI path), extract the PDF anchor's
href, navigate to it to fire the download event.
"""

from __future__ import annotations

from .base import PdfLinkNavigationHandler


class AaaHandler(PdfLinkNavigationHandler):
    name = "aaa"
    display_name = "AAA (Accounting Review)"
    doi_prefixes = ("10.2308/",)
    # Landing page — the only Silverchair path that resolves a bare DOI.
    url_template = (
        "https://publications.aaahq.org/accounting-review/article/doi/{doi}"
    )
    direct_access_domains = ("aaahq.org",)
    concurrency = 1
    delay_s = 1.0
