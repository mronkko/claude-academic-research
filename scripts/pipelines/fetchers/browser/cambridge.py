"""Cambridge University Press — cambridge.org/core.

Cambridge Core's PDF path contains two opaque ids — a content-view
GUID and the internal article filename — neither derivable from the
DOI:

    /core/services/aop-cambridge-core/content/view
        /22D9FE7485D57F9282AFE585CC1DE10F
        /S0305741023001467a.pdf
        /the-old-conflict-in-the-new-economy-....pdf

so the URL has to be read off the landing page. Once read, navigating
to it fires the download directly — measured live, no viewer page and
no second hop, which makes this a plain `PdfLinkNavigationHandler`.

The landing page renders the same href twice ("Save PDF" and "View
PDF" in the Actions dropdown); `.first` picks either safely.

The selector cannot use the shared default. That one looks for
`article-pdf` or a `/pdf/` path segment, and Cambridge has neither —
its path segment is `/content/view/` and the `.pdf` appears twice as a
filename component rather than as a directory.
"""

from __future__ import annotations

from .base import PdfLinkNavigationHandler


class CambridgeHandler(PdfLinkNavigationHandler):
    name = "cambridge"
    display_name = "Cambridge University Press"
    doi_prefixes = ("10.1017/",)
    url_template = "https://doi.org/{doi}"
    direct_access_domains = ("cambridge.org",)
    concurrency = 1
    delay_s = 1.0
    # Measured 2026-08-23 against a cold profile on an institutional IP:
    # landing page and PDF both loaded with no challenge and no sign-in.
    needs_interactive_solve = False

    pdf_link_selector = (
        "a[href*='/core/services/aop-cambridge-core/content/view/']"
        "[href$='.pdf']"
    )

    setup_hint = (
        "Cambridge Core authenticates by IP. On a subscribing network\n"
        "the article page's Actions menu shows 'Save PDF (n.nn mb)'.\n"
        "If you see 'Get access' or a purchase price instead, this\n"
        "title is not reachable from the current session."
    )
