"""Taylor & Francis — tandfonline.com.

T&F's Cloudflare configuration rejects `ctx.request` calls even when
the session has valid CF cookies.  The only reliable path is real
page navigation, where the browser user-agent and request timing
match what Cloudflare expects.
"""

from __future__ import annotations

from .base import PageNavigationHandler


class TandfHandler(PageNavigationHandler):
    name = "tandf"
    display_name = "Taylor & Francis"
    doi_prefixes = ("10.1080/",)
    url_template = "https://www.tandfonline.com/doi/pdf/{doi}?download=true"
    # Landing page for setup — the PDF URL auto-downloads and leaves
    # about:blank; the user needs to see a real page to solve CF or
    # sign in.
    setup_url_template = "https://www.tandfonline.com/doi/full/{doi}"
    direct_access_domains = ("tandfonline.com",)
    concurrency = 1
    delay_s = 1.0
