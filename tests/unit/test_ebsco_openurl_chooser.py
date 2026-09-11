"""EBSCO's "Multiple items found" chooser, and picking from it by DOI.

When an OpenURL matches more than one EBSCO record, EBSCO does not
redirect to a viewer — it serves a chooser: *"Multiple items found / We
found more than one possible match to your request. Select an item to
continue."* Nothing in this handler recognised that page, so the DOI
re-query never started and the item failed with the content-free
"never reached the viewer" hint.

Every constant below was captured live on 2026-09-11 driving a shared
browser through JYU EZproxy for `10.5465/amj.2022.0253` (AMJ 67(1), Feb
2024). Two records came back:

    /189318373/detailv2?...&doi=10.5465%2Famj.2022.0253...
    /175563540/detailv2?...&doi=10.5465%2Famj.2022.0253...

identical in DOI, volume, issue, page and authors — one title-cased, one
ALL-CAPS — and *both* redirecting to the same viewer,
`/c/x3kxfd/viewer/pdf/hd6o7h3ccb`, where the signed-URL request and
`content.ebscohost.com/cds/retrieve` both returned 200.

That is why this is not the case `VERDICT_AMBIGUOUS` was written for.
That rule guards against SmartText's fuzzy hits — 298 *different*
articles with the right one at rank 1, where picking rank 1 attaches the
wrong paper to a citation. Here every candidate carries the queried DOI
in its own href, so identity is established before anything is opened,
and "more than one" means "indexed twice", not "we are guessing".
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fetchers.browser.base import Counter
from fetchers.browser.ebsco import (
    VERDICT_AMBIGUOUS,
    VERDICT_CHOOSER_DISAMBIGUATED,
    EbscoHandler,
    chooser_candidates,
    is_openurl_chooser,
)

DOI = "10.5465/amj.2022.0253"
SIGNED = "https://content.ebscohost.com/cds/retrieve?content=TOKEN"

#: The page EBSCO actually served, EZproxy-rewritten host and all.
CHOOSER_URL = (
    "https://openurl-ebsco-com.ezproxy.jyu.fi/openurl"
    "?spage=126&title=Academy+of+Management+Journal"
    "&doi=10.5465%2Famj.2022.0253&volume=67&genre=article"
    "&date=2024-02&issue=1"
)
#: Same page on the canonical host, which a library reaching EBSCO on
#: institutional IP gets instead of the proxied form.
CHOOSER_URL_CANONICAL = (
    "https://openurl.ebsco.com/openurl?doi=10.5465%2Famj.2022.0253&genre=article"
)
VIEWER_URL = (
    "https://research-ebsco-com.ezproxy.jyu.fi/c/x3kxfd/viewer/pdf/"
    "hd6o7h3ccb?route=details"
)
RESULTS_URL = "https://research.ebsco.com/c/7dz6k2/search/results?q=x&db=bsu"
IDP_URL = "https://login.jyu.fi/idp/profile/SAML2/POST/SSO?execution=e1s2"

_CAND_1 = (
    "/189318373/detailv2?spage=126&title=Academy%20of%20Management%20Journal"
    "&doi=10.5465%2Famj.2022.0253&volume=67&genre=article&date=2024-02"
    "&issue=1&link_origin=login.jyu.fi"
)
_CAND_2 = (
    "/175563540/detailv2?spage=126&title=Academy%20of%20Management%20Journal"
    "&doi=10.5465%2Famj.2022.0253&volume=67&genre=article&date=2024-02"
    "&issue=1&link_origin=login.jyu.fi"
)
#: A different article's record, as a neighbouring result would be.
_CAND_OTHER_DOI = "/111111111/detailv2?doi=10.1287%2Fmnsc.2017.2869&genre=article"


# ---------------------------------------------------------------------------
# Recognising the page
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", [CHOOSER_URL, CHOOSER_URL_CANONICAL])
def test_the_openurl_layer_is_recognised(url: str) -> None:
    assert is_openurl_chooser(url) is True


@pytest.mark.parametrize("url", [
    VIEWER_URL,          # already arrived — nothing to choose
    RESULTS_URL,         # a search page: the identifier re-query owns this
    IDP_URL,             # still signing in
    "https://openurl.ebsco.com/srh%3ASRH.98BB8042/detailv2?sid=Pri",
    "",
])
def test_other_pages_are_not_the_chooser(url: str) -> None:
    assert is_openurl_chooser(url) is False


def test_a_non_ebsco_host_is_never_the_chooser() -> None:
    """The path alone is not enough — `/openurl` is a generic OpenURL
    route that other resolvers also serve."""
    assert is_openurl_chooser("https://sfx.finna.fi/openurl?doi=x") is False


# ---------------------------------------------------------------------------
# Choosing by DOI
# ---------------------------------------------------------------------------


def test_candidates_carrying_the_queried_doi_are_kept_in_page_order() -> None:
    assert chooser_candidates([_CAND_1, _CAND_2], DOI) == [_CAND_1, _CAND_2]


def test_a_candidate_for_another_article_is_dropped() -> None:
    assert chooser_candidates([_CAND_OTHER_DOI, _CAND_2], DOI) == [_CAND_2]


def test_a_candidate_with_no_doi_is_dropped() -> None:
    """An href we cannot tie to the DOI is exactly the guess this
    handler refuses to make."""
    assert chooser_candidates(["/189318373/detailv2?genre=article"], DOI) == []


def test_the_comparison_survives_a_url_wrapped_doi() -> None:
    """Zotero hands out `https://doi.org/...` forms, and a DOI that only
    differs by prefix or case is the same article."""
    assert chooser_candidates(
        [_CAND_1], "https://doi.org/10.5465/AMJ.2022.0253",
    ) == [_CAND_1]


def test_an_unparseable_doi_matches_nothing() -> None:
    assert chooser_candidates([_CAND_1], "not-a-doi") == []


# ---------------------------------------------------------------------------
# The handler flow
# ---------------------------------------------------------------------------


class _Page:
    """Playwright page reduced to what the chooser path touches."""

    def __init__(self, landing_url: str, *, hrefs: tuple[str, ...] = (),
                 serves_pdf: tuple[str, ...] = ()) -> None:
        self.url = landing_url
        self.hrefs = list(hrefs)
        self.serves_pdf = tuple(serves_pdf)
        self.gotos: list[str] = []
        self.selectors: list[str] = []
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
            if any(marker in url for marker in self.serves_pdf):
                self.emit_pdf()
        return MagicMock()

    async def inner_text(self, _selector: str) -> str:
        return "Academy of Management Journal"

    async def eval_on_selector_all(self, selector: str, _expr: str):
        self.selectors.append(selector)
        return list(self.hrefs)

    def emit_pdf(self) -> None:
        resp = MagicMock()
        resp.url = SIGNED
        for fn in list(self._handlers):
            fn(resp)


def _handler() -> EbscoHandler:
    handler = EbscoHandler()
    handler.response_timeout_ms = 0
    handler.requery_timeout_ms = 0
    return handler


def _good_pdf() -> bytes:
    return b"%PDF-1.4\n" + b"0" * 3000 + b"\nstartxref\n12\n%%EOF\n"


def _ctx(body: bytes = b""):
    ctx = MagicMock()
    resp = MagicMock()
    resp.body = AsyncMock(return_value=body)
    ctx.request.get = AsyncMock(return_value=resp)
    return ctx


def _run(handler: EbscoHandler, page: _Page, tmp_path: Path, ctx=None):
    item = {
        "doi": DOI, "item_key": "K1",
        "title": "How Much Inequity Do You See?",
        "resolver_target_url": "http://ezproxy.jyu.fi/login?url=https://openurl.ebsco.com/x",
    }
    return asyncio.run(handler.download(
        page, ctx or _ctx(), item, str(tmp_path),
        counter=Counter(), total=1, t_start=0.0,
    ))


def test_a_chooser_is_disambiguated_by_doi_and_the_pdf_captured(
    tmp_path: Path,
) -> None:
    page = _Page(CHOOSER_URL, hrefs=(_CAND_1, _CAND_2),
                 serves_pdf=("/189318373/",))
    handler = _handler()

    out = _run(handler, page, tmp_path, _ctx(_good_pdf()))

    assert out is not None, "the chooser's DOI-matching record was not opened"
    path, url = out
    assert url == SIGNED
    assert path.read_bytes()[:5] == b"%PDF-"
    assert handler.last_verdict == VERDICT_CHOOSER_DISAMBIGUATED


def test_the_candidate_url_is_resolved_against_the_proxy_host(
    tmp_path: Path,
) -> None:
    """The hrefs are root-relative. Rebuilding them on the canonical host
    would leave the EZproxy session behind — the same trap
    `requery_url_for` documents."""
    page = _Page(CHOOSER_URL, hrefs=(_CAND_1,), serves_pdf=("/189318373/",))

    _run(_handler(), page, tmp_path, _ctx(_good_pdf()))

    opened = [u for u in page.gotos if "/detailv2" in u]
    assert opened, "no candidate was opened"
    assert opened[0].startswith(
        "https://openurl-ebsco-com.ezproxy.jyu.fi/189318373/detailv2",
    ), opened[0]


def test_the_second_candidate_is_tried_when_the_first_serves_nothing(
    tmp_path: Path,
) -> None:
    """Both records are the same article, so trying each is safe — and
    only one of the two may carry full text."""
    page = _Page(CHOOSER_URL, hrefs=(_CAND_1, _CAND_2),
                 serves_pdf=("/175563540/",))

    out = _run(_handler(), page, tmp_path, _ctx(_good_pdf()))

    assert out is not None, "gave up after the first candidate"
    opened = [u for u in page.gotos if "/detailv2" in u]
    assert len(opened) == 2, opened
    assert "/189318373/" in opened[0] and "/175563540/" in opened[1]
    # Every candidate is resolved against the *chooser's* host. After the
    # first navigation `page.url` is the record (or the viewer, on
    # research-ebsco-com), so re-reading it per candidate would send the
    # second one to the wrong host.
    assert all(
        u.startswith("https://openurl-ebsco-com.ezproxy.jyu.fi/") for u in opened
    ), opened


def test_a_chooser_with_no_doi_match_is_never_guessed(
    tmp_path: Path, capsys,
) -> None:
    page = _Page(CHOOSER_URL, hrefs=(_CAND_OTHER_DOI,))
    handler = _handler()

    out = _run(handler, page, tmp_path)

    assert out is None
    assert handler.last_verdict == VERDICT_AMBIGUOUS
    assert not [u for u in page.gotos if "/detailv2" in u], (
        "opened a record that does not carry the queried DOI"
    )
    assert "more than one" in capsys.readouterr().out.lower()


def test_a_chooser_whose_records_serve_no_pdf_reports_the_verdict(
    tmp_path: Path, capsys,
) -> None:
    """Matched the DOI, opened both, got no bytes. That is a different
    claim from "we could not tell which record" and must read as one."""
    page = _Page(CHOOSER_URL, hrefs=(_CAND_1, _CAND_2))
    handler = _handler()

    out = _run(handler, page, tmp_path)

    assert out is None
    assert handler.last_verdict == VERDICT_CHOOSER_DISAMBIGUATED
    assert "chooser" in capsys.readouterr().out.lower()
