"""Taylor & Francis — tandfonline.com.

T&F's Cloudflare configuration rejects `ctx.request` calls even when
the session has valid CF cookies.  The only reliable path is real
page navigation, where the browser user-agent and request timing
match what Cloudflare expects.
"""

from __future__ import annotations

import re

from .base import PageNavigationHandler

#: An ISBN-13 at the start of a DOI suffix: a book or chapter.
_BOOK_SUFFIX = re.compile(r"97[89]-?\d")


class TandfHandler(PageNavigationHandler):
    name = "tandf"
    display_name = "Taylor & Francis"
    # Routledge (10.4324), Haworth (10.1300) and Lawrence Erlbaum
    # (10.1207) are all T&F imprints today and serve from
    # tandfonline.com under the same /doi/pdf/ shape as 10.1080.
    doi_prefixes = ("10.1080/", "10.4324/", "10.1300/", "10.1207/")
    url_template = "https://www.tandfonline.com/doi/pdf/{doi}?download=true"
    # Landing page for setup — the PDF URL auto-downloads and leaves
    # about:blank; the user needs to see a real page to solve CF or
    # sign in.
    setup_url_template = "https://www.tandfonline.com/doi/full/{doi}"
    direct_access_domains = ("tandfonline.com",)
    concurrency = 1
    delay_s = 1.0

    # From JYU samples, 2026-09-25: 10.1080/10852352.2018.1470423 shows
    # "EUR 48.00 Add to cart" beside "Access provided by Jyvaskylan
    # Yliopisto"; the entitled 10.1080/09585192.2010.516595 shows the
    # same banner and no price. The word "purchase" is chrome on both,
    # so the rule is on the priced cart widget, not the word.
    denial_markers = (
        r"(?:EUR|USD|GBP|€|£|\$)\s?\d[\d.,]*\s+Add to cart",
    )
    recognised_markers = (r"Access provided by",)

    def matches_doi(self, doi: str) -> bool:
        """Not book DOIs: Routledge chapters (`10.4324/9781315224350-6`)
        live on taylorfrancis.com, and /doi/pdf/ on tandfonline.com is a
        404 for them (reported 2026-09-24). Left unclaimed they reach the
        handler for their resolved host, or the Connector, which reads
        taylorfrancis.com."""
        if _BOOK_SUFFIX.match(doi.partition("/")[2]):
            return False
        return super().matches_doi(doi)
