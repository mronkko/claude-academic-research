"""Asking EBSCO about the DOI when it answers with a search page.

The commonest residual failure on this handler is not a block. Alma
hands EBSCO an OpenURL carrying journal, year and title, EBSCO turns it
into `(SO <journal>)AND(DT <year>)AND(TI <title>)`, and that query can
exclude the very article sitting in the database. Measured live on
`10.1287/mnsc.2017.2869`: Crossref and the DOI say 2017 (online-first),
EBSCO holds it as May 2019, so `DT 2017` returned zero and EBSCO fell
back to SmartText — 298 fuzzy hits with the right article at rank 1.
`DI "10.1287/mnsc.2017.2869"` returns 1-1 of 1.

Two rules carry most of the weight here, and both are about not
asserting more than was established:

* **Never read a verdict off the landing page.** Its zero says the
  OpenURL's query found nothing, and that query is the suspect. Only the
  identifier query answers about the article.
* **A count on a SmartText page is not a count.** Those hits answer a
  different question, and picking from them attaches the wrong paper to
  a citation — worse than attaching nothing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import enrich_pdfs
import pdf_fetch_log
import pytest
from fetchers.browser import ebsco
from fetchers.browser.base import Counter
from fetchers.browser.ebsco import (
    EbscoHandler,
    hit_count_from_text,
    is_search_landing,
    requery_url_for,
)

DOI = "10.1287/mnsc.2017.2869"
SIGNED = "https://content.ebscohost.com/cds/retrieve?content=TOKEN"

# Real landing URLs from the 14-item run.
RESULTS_URL = (
    "https://research.ebsco.com/c/7dz6k2/search/results"
    "?limiters=&q=%28SO+%28Management+science.%29%29AND%28DT+2017%29&db=bsu"
)
PROXIED_URL = "https://research-ebsco-com.ezproxy.jyu.fi/c/x3kxfd/search/results?q=x"
DETAILV2_URL = "https://openurl.ebsco.com/srh%3ASRH.98BB8042.7F7F90DA/detailv2?sid=Pri"
IDP_URL = "https://login.jyu.fi/idp/profile/SAML2/POST/SSO?execution=e1s1"


# ---------------------------------------------------------------------------
# Building the re-query
# ---------------------------------------------------------------------------


def test_requery_reuses_profile_and_database() -> None:
    url = requery_url_for(RESULTS_URL, DOI)
    assert url.startswith("https://research.ebsco.com/c/7dz6k2/search/results?")
    assert "q=DI%20%2210.1287%2Fmnsc.2017.2869%22" in url
    assert "db=bsu" in url


def test_requery_keeps_the_proxy_host() -> None:
    """Load-bearing: rebuilding on the canonical host would leave the
    EZproxy session behind and re-query unauthenticated."""
    url = requery_url_for(PROXIED_URL, DOI)
    assert url.startswith(
        "https://research-ebsco-com.ezproxy.jyu.fi/c/x3kxfd/search/results?")


def test_requery_omits_db_when_the_landing_named_none() -> None:
    url = requery_url_for("https://research.ebsco.com/c/7dz6k2/search/results", DOI)
    assert "db=" not in url
    assert url.endswith("q=DI%20%2210.1287%2Fmnsc.2017.2869%22")


@pytest.mark.parametrize("landing", [
    DETAILV2_URL,                      # no c/<profile> segment to query against
    "https://research.ebsco.com/",     # nor here
    "https://example.org/c/7dz6k2/search/results",   # not EBSCO at all
    "",
])
def test_requery_declines_rather_than_guessing(landing: str) -> None:
    """A fabricated profile id would query some other tenant's view."""
    assert requery_url_for(landing, DOI) == ""


def test_requery_needs_a_doi() -> None:
    assert requery_url_for(RESULTS_URL, "") == ""
    assert requery_url_for(RESULTS_URL, "   ") == ""


# ---------------------------------------------------------------------------
# Reading EBSCO's answer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("Search Results 1 - 1 of 1", 1),
    ("1 - 20 of 298", 298),
    ("1 - 50 of 1,204 results", 1204),
    ("No results were found for your search", 0),
    ("No Results Found", 0),
    ("", None),
    ("   ", None),
    ("Some page with no counter on it", None),
])
def test_hit_count_reads_ebscos_own_counter(text: str, expected) -> None:
    assert hit_count_from_text(text) == expected


def test_smarttext_page_never_yields_a_count() -> None:
    """298 fuzzy hits with the right article at rank 1 is not evidence."""
    assert hit_count_from_text(
        "Your search returned no results. SmartText Searching was used. "
        "1 - 20 of 298") is None


def test_none_is_not_zero() -> None:
    """The distinction is the whole point: zero is an answer about the
    article, None is our failure to read the page."""
    assert hit_count_from_text("No results") == 0
    assert hit_count_from_text("<div></div>") is None


@pytest.mark.parametrize("url,expected", [
    (RESULTS_URL, True),
    (DETAILV2_URL, True),
    ("https://research.ebsco.com/c/7dz6k2/search/advanced/filters?a=1", True),
    (IDP_URL, False),
    ("https://research.ebsco.com/c/x/viewer/pdf/y", False),
    ("", False),
])
def test_search_landing_detection(url: str, expected: bool) -> None:
    assert is_search_landing(url) is expected


# ---------------------------------------------------------------------------
# The handler flow
# ---------------------------------------------------------------------------


class _Locator:
    def __init__(self, page: _Page, selector: str) -> None:
        self.page, self.selector = page, selector

    @property
    def first(self) -> _Locator:
        return self

    async def count(self) -> int:
        return 1 if self.selector in self.page.links else 0

    async def click(self, **_kw) -> None:
        self.page.clicks.append(self.selector)
        self.page.on_click(self.page)


class _Page:
    """Playwright page reduced to what this handler touches."""

    def __init__(self, landing_url: str, text: str = "", *,
                 requery_text: str = "", links: tuple[str, ...] = (),
                 on_click=lambda page: None) -> None:
        self.url = landing_url
        self.text = text
        self.requery_text = requery_text
        self.links = set(links)
        self.on_click = on_click
        self.gotos: list[str] = []
        self.clicks: list[str] = []
        self._handlers: list = []

    def on(self, _event, fn) -> None:
        self._handlers.append(fn)

    def remove_listener(self, _event, fn) -> None:
        if fn in self._handlers:
            self._handlers.remove(fn)

    async def goto(self, url: str, **_kw):
        self.gotos.append(url)
        # The first goto is the resolver target, which redirects to the
        # landing URL the test set up; later ones land where they say.
        if len(self.gotos) > 1:
            self.url = url
            self.text = self.requery_text
        return MagicMock()

    async def inner_text(self, _selector: str) -> str:
        return self.text

    def locator(self, selector: str) -> _Locator:
        return _Locator(self, selector)

    def emit_pdf(self) -> None:
        resp = MagicMock()
        resp.url = SIGNED
        for fn in list(self._handlers):
            fn(resp)


def _handler() -> EbscoHandler:
    handler = EbscoHandler()
    # No real waiting: both loops read once and give up.
    handler.response_timeout_ms = 0
    handler.requery_timeout_ms = 0
    return handler


def _good_pdf() -> bytes:
    return b"%PDF-1.4\n" + b"0" * 3000 + b"\nstartxref\n12\n%%EOF\n"


def _ctx(body: bytes = b""):
    """Context whose `request.get` returns `body`, as the real one does
    for the signed CDN URL."""
    ctx = MagicMock()
    resp = MagicMock()
    resp.body = AsyncMock(return_value=body)
    ctx.request.get = AsyncMock(return_value=resp)
    return ctx


def _run(handler: EbscoHandler, page: _Page, tmp_path: Path, ctx=None):
    item = {
        "doi": DOI, "item_key": "K1", "title": "Imperfect Renegotiations",
        "resolver_target_url": "https://alma.example.org/uresolver/openurl",
    }
    return asyncio.run(handler.download(
        page, ctx or _ctx(), item, str(tmp_path),
        counter=Counter(), total=1, t_start=0.0,
    ))


def test_zero_hits_is_an_earned_no_holdings_verdict(tmp_path: Path, capsys) -> None:
    handler = _handler()
    page = _Page(RESULTS_URL, "1 - 20 of 298 SmartText Searching",
                 requery_text="No results were found")

    assert _run(handler, page, tmp_path) is None
    assert handler.last_verdict == ebsco.VERDICT_NO_HOLDINGS
    out = capsys.readouterr().out
    assert "no holdings" in out
    assert "never reached the viewer" not in out


def test_the_landing_pages_own_zero_is_not_a_verdict(tmp_path: Path) -> None:
    """`DT 2017` excluding an article EBSCO holds under 2019 produces a
    zero on the landing page. The article is held; the query was wrong."""
    handler = _handler()
    page = _Page(RESULTS_URL, "No results were found",
                 requery_text="1 - 1 of 1")

    _run(handler, page, tmp_path)

    assert handler.last_verdict != ebsco.VERDICT_NO_HOLDINGS
    assert handler.last_verdict == ebsco.VERDICT_UNIQUE
    assert len(page.gotos) == 2  # it re-queried rather than concluding


def test_more_than_one_record_is_never_guessed(tmp_path: Path, capsys) -> None:
    handler = _handler()
    page = _Page(RESULTS_URL, "1 - 20 of 298 SmartText Searching",
                 requery_text="1 - 2 of 2",
                 links=("a[href*='/viewer/pdf/']",))

    assert _run(handler, page, tmp_path) is None
    assert handler.last_verdict == ebsco.VERDICT_AMBIGUOUS
    assert page.clicks == []
    assert "not guessing" in capsys.readouterr().out


def test_a_unique_record_is_opened_and_its_pdf_captured(tmp_path: Path) -> None:
    handler = _handler()
    page = _Page(
        RESULTS_URL, "1 - 20 of 298 SmartText Searching",
        requery_text="1 - 1 of 1",
        links=("a[href*='/viewer/pdf/']",),
        on_click=lambda p: p.emit_pdf(),
    )

    result = _run(handler, page, tmp_path, ctx=_ctx(_good_pdf()))

    assert handler.last_verdict == ebsco.VERDICT_UNIQUE
    assert page.clicks == ["a[href*='/viewer/pdf/']"]
    # The listener stayed armed across the re-query, so the viewer's own
    # request was still observed.
    assert result is not None and result[1] == SIGNED
    assert result[0].read_bytes() == _good_pdf()


def test_a_unique_record_we_cannot_open_still_reports_the_verdict(
    tmp_path: Path, capsys,
) -> None:
    """The click-through is unverified against a live page; when no
    selector matches, the earned verdict must survive anyway."""
    handler = _handler()
    page = _Page(RESULTS_URL, "SmartText Searching", requery_text="1 - 1 of 1")

    assert _run(handler, page, tmp_path) is None
    assert handler.last_verdict == ebsco.VERDICT_UNIQUE
    assert "found the record by DOI" in capsys.readouterr().out


def test_an_unreadable_requery_claims_nothing(tmp_path: Path, capsys) -> None:
    handler = _handler()
    page = _Page(RESULTS_URL, "SmartText Searching", requery_text="<div></div>")

    assert _run(handler, page, tmp_path) is None
    assert handler.last_verdict == ebsco.VERDICT_UNKNOWN
    assert "never reached the viewer" in capsys.readouterr().out


def test_a_login_stall_is_not_re_queried(tmp_path: Path) -> None:
    """Navigating away from an IdP page would abandon a sign-in the user
    may be part-way through, and says nothing about holdings."""
    handler = _handler()
    page = _Page(IDP_URL, "Sign in", requery_text="No results")

    assert _run(handler, page, tmp_path) is None
    assert handler.last_verdict == ebsco.VERDICT_UNKNOWN
    assert len(page.gotos) == 1


def test_detailv2_no_exact_match_is_reported_as_unconfirmed(
    tmp_path: Path, capsys,
) -> None:
    """That page carries no profile to re-query with, so EBSCO was never
    asked about the DOI — distinct from a confirmed no-holdings."""
    handler = _handler()
    page = _Page(DETAILV2_URL,
                 "No exact match found through your institution")

    assert _run(handler, page, tmp_path) is None
    assert handler.last_verdict == ebsco.VERDICT_NO_MATCH_UNCONFIRMED
    assert "unconfirmed" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# What the verdict means downstream
# ---------------------------------------------------------------------------


def test_no_holdings_is_access_blocked_not_unavailable() -> None:
    """UNAVAILABLE licenses an FE6 exclusion. One platform's stale
    holdings say nothing about whether the article can be had."""
    handler = MagicMock(last_verdict=ebsco.VERDICT_NO_HOLDINGS)

    cause = enrich_pdfs._browser_failure_cause(handler, transport=False)

    assert cause is pdf_fetch_log.FailureCause.ACCESS_BLOCKED
    assert cause is not pdf_fetch_log.FailureCause.UNAVAILABLE
    assert "ILL" in pdf_fetch_log.SUGGESTED_FE_CODE[cause.value]


def test_a_transport_error_outranks_any_verdict() -> None:
    """Nothing was asked, so nothing was answered — whatever a stale
    `last_verdict` still says."""
    handler = MagicMock(last_verdict=ebsco.VERDICT_NO_HOLDINGS)

    assert enrich_pdfs._browser_failure_cause(handler, transport=True) is (
        pdf_fetch_log.FailureCause.NETWORK_ERROR)


@pytest.mark.parametrize("verdict", [
    ebsco.VERDICT_UNKNOWN,
    ebsco.VERDICT_UNIQUE,
    ebsco.VERDICT_AMBIGUOUS,
    ebsco.VERDICT_NO_MATCH_UNCONFIRMED,
])
def test_every_other_verdict_leaves_classification_to_the_shared_rules(
    verdict: str,
) -> None:
    handler = MagicMock(last_verdict=verdict)
    assert enrich_pdfs._browser_failure_cause(handler, transport=False) is None


def test_a_handler_without_verdicts_is_unaffected() -> None:
    """Every other browser handler reaches this function too."""
    assert enrich_pdfs._browser_failure_cause(object(), transport=False) is None
