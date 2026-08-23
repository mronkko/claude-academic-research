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

import re

from .base import PdfLinkNavigationHandler

#: Cambridge's legacy journal DOIs are `10.1017/s<digits>`, and their
#: content-view path is the suffix upper-cased — nothing else. Verified
#: against Crossref's `link` field for six such DOIs: derived == deposited
#: in every case.
_LEGACY_JOURNAL_DOI = re.compile(r"^10\.1017/(s\d{5,})$", re.IGNORECASE)

_CONTENT_VIEW = (
    "https://www.cambridge.org/core/services/aop-cambridge-core"
    "/content/view/{ident}"
)


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

    def fallback_pdf_urls(self, doi: str) -> list[str]:
        """Content-view URL for a legacy `10.1017/s<digits>` DOI.

        Some pre-Cambridge-Core DOIs still carry a `resource.primary` of
        `journals.cambridge.org/abstract_<ID>` — a host retired years ago
        that now answers a bare `404 default backend`. `doi.org` follows
        that registration, so the handler lands on nothing and has no
        anchor to read, even though the article is live on Cambridge Core
        and this institution can download it.

        Live case: `10.1017/s0147547903000231` (Surh, *International Labor
        and Working-Class History*) 404s through `doi.org` and downloads
        immediately from the derived URL.

        Only the `s<digits>` form is rewritten. Modern DOIs (`als.2015.2`,
        `lap.2019.62`) resolve to a working landing page and are left to
        the normal path, and book chapters (`9781108610070.037`,
        `cbo9781107282018.004`) are deliberately excluded — those are
        genuinely out of scope for a journal handler.
        """
        m = _LEGACY_JOURNAL_DOI.match((doi or "").strip())
        if not m:
            return []
        return [_CONTENT_VIEW.format(ident=m.group(1).upper())]
