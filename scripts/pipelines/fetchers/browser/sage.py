"""Sage — SAGE Journals.

Sage blocks sessions that exceed ~30 requests/minute with an HTTP 429
or session reset. Keep concurrency at 1 and a 2.5-second delay between
requests as the safe default; the user can override by editing the
class attributes if their institutional agreement permits more.
"""

from __future__ import annotations

from .base import RequestHandler


class SageHandler(RequestHandler):
    name = "sage"
    display_name = "Sage"
    doi_prefixes = ("10.1177/",)
    url_template = "https://journals.sagepub.com/doi/pdf/{doi}?download=true"
    # Landing page for setup — opening the PDF URL directly triggers
    # a Chromium auto-download that consumes the session and leaves
    # the user with about:blank.
    setup_url_template = "https://journals.sagepub.com/doi/{doi}"
    direct_access_domains = ("sagepub.com",)
    concurrency = 1
    delay_s = 2.5
