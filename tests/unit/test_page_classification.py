"""The browser pass reads the publisher's own page before asking a human.

Requested from a live JYU run on 2026-09-25: the setup prompt asked
"can you see/reach the PDF from this page?" about pages that already
said, in so many words, that the institution has no access. Each of
those questions cost the operator a look, and — when the clearance probe
waved the setup through — two 30 s download timeouts on top.

The marker sentences below are copied from the diagnostics that run
saved. Every one is item-level (it is about this article), and the
verdict it licenses is item-level too: that item fails fast, carrying
the page's words. It never skips the publisher, because one unentitled
journal says nothing about the next.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest
from fetchers.browser import (
    CambridgeHandler,
    EmeraldHandler,
    OupHandler,
    SageHandler,
    SpringerHandler,
    TandfHandler,
    WileyHandler,
)
from fetchers.browser.base import (
    PAGE_GONE,
    PAGE_NO_ENTITLEMENT,
    PAGE_NO_PDF,
    PAGE_UNKNOWN,
    PageObservation,
    classify_page,
    record_observation,
)

# Trimmed page text from the 2026-09-25 JYU diagnostics.
SAGE_DENIED = (
    "I have access via: University of Jyväskylä Restricted access Get access "
    "Institution University of Jyväskylä does not have access to this "
    "article. Add or change institution Download PDF Figures Tables"
)
CAMBRIDGE_DENIED = (
    "Get access Check access Institutional login We recognised you are "
    "associated with one or more institutions that don’t have access to "
    "this content. If you should have access, please contact your librarian."
)
SPRINGER_DENIED = (
    "This is a preview of subscription content, log in via an institution "
    "to check access. Buy Chapter Download preview PDF. 130.234.240.27 "
    "University of Helsinki, Finelib Springer Compact (3003509205) - "
    "University of Jyväskylä (2000617297)"
)
SPRINGER_SIGNED_OUT = (
    "This is a preview of subscription content, log in via an institution "
    "to check access. Buy Chapter Not affiliated"
)
EMERALD_DENIED = (
    "University of Jyvaskyla FinELib Consortia Sign in as different "
    "institution This content is only available via PDF. You do not "
    "currently have access to this content. Sign in"
)
EMERALD_SIGNED_OUT = (
    "Institutional Accounts Sign In This content is only available via "
    "PDF. You do not currently have access to this content."
)
TANDF_DENIED = (
    "Access provided by Jyvaskylan Yliopisto Login | Register Cart Add to "
    "Cart Log in Restore content access Purchase options Article PDF can be "
    "downloaded EUR 48.00 Add to cart PDF download + Online access"
)
OUP_DENIED = (
    "You do not currently have access to this article. Download all slides "
    "Signed in as Institutional account This Feature Is Available To "
    "Subscribers Only"
)

# Entitled landing pages, JYU, 2026-09-25 (captured by the phase2
# session after the first markers shipped). Each is the chrome a naive
# matcher trips on, beside what shows the page is readable.
ENTITLED = {
    "sage": (SageHandler(), "I have access via: University of Jyväskylä "
             "you have access Purchase Access through your institution "
             "Download PDF"),
    "cambridge": (CambridgeHandler(), "Access through University of "
                  "Jyväskylä View PDF Purchase"),
    "springer": (SpringerHandler(), "Download PDF University of Jyväskylä "
                 "(2000617297) Institutional subscriptions"),
    "emerald": (EmeraldHandler(), "University of Jyvaskyla FinELib Consortia "
                "Sign in as different institution References"),
    "tandf": (TandfHandler(), "Access provided by Jyvaskylan Yliopisto Cart "
              "Add to Cart Full access Download PDF Order Reprints"),
    "oup": (OupHandler(), "Purchase This Feature Is Available To Subscribers "
            "Only Sign In or Create an Account This PDF is available to "
            "Subscribers Only View Article Abstract & Purchase Options For "
            "full access to this pdf, sign in to an existing account"),
}

#: What an entitled page looks like to a naive "Get access" matcher:
#: menus and banners carry it on pages we can read.
ENTITLED_CHROME = (
    "Get access Access options Institutional login Download PDF "
    "Purchase this issue"
)


class _Page:
    def __init__(self, text: str, *, url: str = "https://pub/x",
                 title: str = "T", selectors: tuple[str, ...] = ()) -> None:
        self._text = text
        self.url = url
        self._title = title
        #: CSS selectors present on the page.
        self._selectors = selectors

    async def title(self) -> str:
        return self._title

    async def evaluate(self, _js: str, arg=None):
        if arg is None:
            return self._text
        return arg in self._selectors


def _resp(status: int) -> MagicMock:
    r = MagicMock()
    r.status = status
    return r


def _classify(handler, text: str, resp=None, *,
              selectors: tuple[str, ...] = ()) -> PageObservation:
    return asyncio.run(classify_page(
        _Page(text, selectors=selectors), resp,
        denial_markers=handler.denial_markers,
        recognised_markers=handler.recognised_markers,
        pdf_control_selector=handler.pdf_control_selector,
        pdf_link_selector=handler.pdf_offered_selector,
    ))


@pytest.mark.parametrize("handler,text", [
    (SageHandler(), SAGE_DENIED),
    (CambridgeHandler(), CAMBRIDGE_DENIED),
    (SpringerHandler(), SPRINGER_DENIED),
    (EmeraldHandler(), EMERALD_DENIED),
    (TandfHandler(), TANDF_DENIED),
    (OupHandler(), OUP_DENIED),
])
def test_denial_pages_classify_with_the_pages_own_words(handler, text) -> None:
    obs = _classify(handler, text)
    assert obs.klass == PAGE_NO_ENTITLEMENT, handler.name
    assert obs.phrase and obs.phrase.lower() in text.lower()
    assert obs.conclusive


@pytest.mark.parametrize("handler", [
    SageHandler(), CambridgeHandler(), SpringerHandler(), EmeraldHandler(),
    WileyHandler(),
])
def test_menu_chrome_on_an_entitled_page_is_not_a_denial(handler) -> None:
    assert _classify(handler, ENTITLED_CHROME).klass == PAGE_UNKNOWN


@pytest.mark.parametrize("name", sorted(ENTITLED))
def test_entitled_pages_are_not_denials(name) -> None:
    handler, text = ENTITLED[name]
    assert _classify(handler, text).klass == PAGE_UNKNOWN, name


def test_tandf_without_the_access_banner_is_not_a_verdict() -> None:
    assert _classify(
        TandfHandler(), "Purchase options EUR 48.00 Add to cart",
    ).klass == PAGE_UNKNOWN


@pytest.mark.parametrize("handler,text", [
    (SpringerHandler(), SPRINGER_SIGNED_OUT),
    (EmeraldHandler(), EMERALD_SIGNED_OUT),
])
def test_a_signed_out_session_is_not_a_verdict(handler, text) -> None:
    """Signing in could change the answer, and that is the prompt's job."""
    assert _classify(handler, text).klass == PAGE_UNKNOWN


def test_a_gone_landing_page_is_conclusive() -> None:
    obs = _classify(WileyHandler(), "Error 404 Page not found", _resp(404))
    assert obs.klass == PAGE_GONE
    assert obs.phrase == "HTTP 404"


def test_a_challenge_status_is_not_gone() -> None:
    assert _classify(WileyHandler(), "Just a moment", _resp(403)).klass == (
        PAGE_UNKNOWN
    )


def test_a_page_that_cannot_be_read_is_unknown() -> None:
    class _Broken(_Page):
        async def evaluate(self, _js):
            raise RuntimeError("target closed")

    obs = asyncio.run(classify_page(
        _Broken(""), None, denial_markers=SageHandler.denial_markers,
    ))
    assert obs.klass == PAGE_UNKNOWN


def test_observations_are_appended_as_jsonl(tmp_path) -> None:
    obs = PageObservation(url="https://pub/x", title="T",
                          klass=PAGE_NO_ENTITLEMENT, phrase="no access")
    record_observation(tmp_path, handler="sage", doi="10.1/a", obs=obs)
    record_observation(tmp_path, handler="sage", doi="10.1/b", obs=obs)
    lines = (tmp_path / "diagnostics" / "page_observations.jsonl") \
        .read_text().splitlines()
    rows = [json.loads(line) for line in lines]
    assert [r["doi"] for r in rows] == ["10.1/a", "10.1/b"]
    assert rows[0]["class"] == PAGE_NO_ENTITLEMENT
    assert rows[0]["phrase"] == "no access"
    assert rows[0]["url"] == "https://pub/x"


class _NavPage(_Page):
    """A page whose every goto lands on `text` with `status`."""

    def __init__(self, text: str, status: int = 200) -> None:
        super().__init__(text)
        self._status = status
        self.waited_for_download = False

    async def goto(self, url, **kw):
        del kw
        self.url = url
        return _resp(self._status)

    async def wait_for_load_state(self, *a, **kw):
        pass

    def expect_download(self, timeout=0):
        page = self

        class _Info:
            @property
            def value(self):
                page.waited_for_download = True
                raise AssertionError("waited for a download after a verdict")

        class _CM:
            async def __aenter__(self):
                return _Info()

            async def __aexit__(self, *exc):
                return False

        return _CM()

    async def screenshot(self, **kw):
        pass

    async def content(self):
        return ""


def test_setup_answers_from_a_denial_page_without_asking(monkeypatch) -> None:
    def _no_prompt(_prompt):
        raise AssertionError("asked about a page that had already answered")

    monkeypatch.setattr("fetchers.browser.base._read_user_line", _no_prompt)
    h = SageHandler()
    result = asyncio.run(h.setup(_NavPage(SAGE_DENIED), "10.1258/x"))
    # "proceed", not "skip": the verdict is this item's alone.
    assert result == "proceed"
    assert h.last_observation is not None
    assert h.last_observation.klass == PAGE_NO_ENTITLEMENT


def test_setup_still_asks_when_the_page_is_inconclusive(monkeypatch) -> None:
    asked: list[str] = []
    monkeypatch.setattr("fetchers.browser.base._read_user_line",
                        lambda p: asked.append(p) or "y")
    h = SageHandler()
    h.clearance_timeout_s = 0
    asyncio.run(h.setup(_NavPage(ENTITLED_CHROME), "10.1258/x"))
    assert asked, "an inconclusive page must still reach the human"


def test_download_fails_fast_on_a_denial_instead_of_waiting(tmp_path) -> None:
    """Sage redirects an unlicensed PDF URL to the abstract page. The
    handler used to wait 30 s there for a download event."""
    from fetchers.browser import Counter

    h = SageHandler()
    page = _NavPage(SAGE_DENIED)
    counter = Counter()
    result = asyncio.run(h.download(
        page, None, {"doi": "10.1258/x", "title": "T"}, tmp_path,
        counter=counter, total=1, t_start=0.0,
    ))
    assert result is None
    assert not page.waited_for_download
    assert h.last_observation is not None and h.last_observation.conclusive
    assert "does not have access" in h.last_error


def test_a_404_on_the_handlers_own_pdf_url_is_not_a_verdict(tmp_path) -> None:
    """Only the landing page counts: a built URL can be wrong."""
    from fetchers.browser import Counter

    h = WileyHandler()
    asyncio.run(h.download(
        _NavPage("Error 404", status=404), None,
        {"doi": "10.1111/x", "title": "T"}, tmp_path,
        counter=Counter(), total=1, t_start=0.0,
    ))
    assert h.last_observation is None


# Emerald, JYU, 2026-09-25. 10.1108/edi-07-2015-0056 is an HTML-only
# book review: recognised, readable, and no PDF. 10.1108/edi-01-2021-0021
# is a research article with one. Both render the toolbar's PDF slot;
# only the article has the anchor in it.
EMERALD_READABLE = (
    "University of Jyvaskyla FinELib Consortia Sign in as different "
    "institution Book Review Twenty-four authors from institutions "
    "located in 14 countries"
)
EMERALD_TOOLBAR = EmeraldHandler.pdf_control_selector
EMERALD_ANCHOR = EmeraldHandler.pdf_offered_selector


def test_a_readable_page_with_no_pdf_says_so() -> None:
    obs = _classify(EmeraldHandler(), EMERALD_READABLE,
                    selectors=(EMERALD_TOOLBAR,))
    assert obs.klass == PAGE_NO_PDF
    assert obs.conclusive


def test_a_readable_page_with_its_pdf_link_is_not_a_verdict() -> None:
    obs = _classify(EmeraldHandler(), EMERALD_READABLE,
                    selectors=(EMERALD_TOOLBAR, EMERALD_ANCHOR))
    assert obs.klass == PAGE_UNKNOWN


def test_a_missing_link_counts_only_once_the_toolbar_rendered() -> None:
    """Absence is evidence only on a page that got as far as drawing
    its PDF control; Emerald's home page (a bad redirect) draws none."""
    obs = _classify(EmeraldHandler(), EMERALD_READABLE, selectors=())
    assert obs.klass == PAGE_UNKNOWN


def test_a_missing_link_needs_a_recognised_institution() -> None:
    obs = _classify(EmeraldHandler(), "Book Review Sign In",
                    selectors=(EMERALD_TOOLBAR,))
    assert obs.klass == PAGE_UNKNOWN


def test_a_denial_outranks_a_missing_link() -> None:
    obs = _classify(EmeraldHandler(), EMERALD_DENIED,
                    selectors=(EMERALD_TOOLBAR,))
    assert obs.klass == PAGE_NO_ENTITLEMENT


def test_a_readable_page_without_a_pdf_is_logged_no_pdf_offered() -> None:
    """Nobody refused access, so not ACCESS_BLOCKED; the text is right
    there as HTML, so not UNAVAILABLE."""
    import enrich_pdfs
    import pdf_fetch_log

    h = EmeraldHandler()
    h.last_observation = PageObservation(
        url="u", title="t", klass=PAGE_NO_PDF, phrase="p",
    )
    h.last_verdict = "no_pdf_offered: p"
    assert enrich_pdfs._browser_failure_cause(h, False) == (
        pdf_fetch_log.FailureCause.NO_PDF_OFFERED
    )
    h.last_observation = PageObservation(
        url="u", title="t", klass=PAGE_NO_ENTITLEMENT, phrase="p",
    )
    assert enrich_pdfs._browser_failure_cause(h, False) == (
        pdf_fetch_log.FailureCause.ACCESS_BLOCKED
    )
