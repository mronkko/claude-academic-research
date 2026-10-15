"""Page-navigation handlers must not swallow a dead network or browser.

`PdfLinkNavigationHandler` (OUP, AAA, Cambridge) wrapped its landing-page
`goto` in `except Exception: pass`, so a lost connection surfaced as
"PDF link not found" and the outage breaker and BrowserGone — both of
which read `handler.last_error` — never fired. An outage was then logged
item by item. No Playwright here: the page is a stub.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fetchers.browser.base import (
    Counter,
    PageNavigationHandler,
    is_browser_gone,
    is_transport_error,
)
from fetchers.browser.oup import OupHandler

_DISCONNECTED = "page.goto: net::ERR_INTERNET_DISCONNECTED at https://x"
_CLOSED = "page.goto: Target page, context or browser has been closed"


class _DeadPage:
    """Every navigation fails with `message`."""

    url = "about:blank"

    def __init__(self, message: str) -> None:
        self.message = message

    async def goto(self, *_a, **_k):
        raise RuntimeError(self.message)

    async def title(self):
        return ""

    async def screenshot(self, **_k):
        raise RuntimeError("no screenshot")

    async def content(self):
        return ""

    def expect_download(self, **_k):
        page = self

        class _Ctx:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *exc):
                return False

            @property
            def value(self_inner):
                async def _v():
                    raise RuntimeError(
                        'Timeout 30000ms exceeded while waiting for event "download"'
                    )
                return _v()

        del page
        return _Ctx()


def _run(handler, page, tmp_path):
    return asyncio.run(handler.download(
        page, None, {"doi": "10.1093/jleo/ewaa004", "title": "t"}, tmp_path,
        counter=Counter(), total=1, t_start=time.monotonic(),
    ))


@pytest.mark.parametrize("message, check", [
    (_DISCONNECTED, is_transport_error),
    (_CLOSED, is_browser_gone),
])
def test_pdf_link_handler_reports_a_dead_landing_page_in_its_own_words(
    tmp_path, message, check,
) -> None:
    handler = OupHandler()
    assert _run(handler, _DeadPage(message), tmp_path) is None
    assert check(handler.last_error), handler.last_error


class _Direct(PageNavigationHandler):
    name = "direct_test"
    doi_prefixes = ("10.9999/",)
    url_template = "https://example.org/{doi}.pdf"


def test_page_navigation_handler_reports_a_dead_network(tmp_path) -> None:
    """Same swallow in the direct-PDF flow: a dead connection waited out
    the 30 s download timeout and reported *that* instead."""
    handler = _Direct()
    assert _run(handler, _DeadPage(_DISCONNECTED), tmp_path) is None
    assert is_transport_error(handler.last_error), handler.last_error
