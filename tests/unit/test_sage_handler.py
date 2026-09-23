"""Sage downloads through the page, not the Playwright request client.

On 2026-09-23 the request client got Cloudflare's "Just a moment…" for
88 of 88 items while the same window showed the article unchallenged.
"""

from __future__ import annotations

from fetchers.browser.base import PageNavigationHandler, RequestHandler
from fetchers.browser.sage import SageHandler


def test_sage_navigates_the_page() -> None:
    assert issubclass(SageHandler, PageNavigationHandler)
    assert not issubclass(SageHandler, RequestHandler)


def test_sage_download_url_still_asks_for_the_file() -> None:
    url = SageHandler.url_template.format(doi="10.1177/0022185614524410")
    assert url == (
        "https://journals.sagepub.com/doi/pdf/10.1177/0022185614524410?download=true"
    )
