"""EBSCOhost handler: platform detection, registry placement, download.

This handler exists because EBSCOhost is the platform the link resolver
routes to most, and its holdings reach much further back than the
publishers' — from 1982 where FinELib SpringerLink starts at 1997. It is
therefore the route to exactly the pre-1997 population the coverage guard
diverts away from Springer.

Two structural properties are pinned here because getting either wrong
breaks routing silently rather than loudly:

1. It must stay **out of `all_handlers()`**. Nothing selects it by DOI or
   host; the resolver-target pass picks it. If it leaked into the registry
   it would be offered to `resolve_by_doi` / `resolve_by_host` in Pass 1,
   where no resolver target exists and it can only fail.
2. Detection must key on **platform naming, not the URL**. On Alma every
   target shares the tenant's redirector host, so a URL test cannot tell
   EBSCOhost from ProQuest.

The download flow itself is mocked: the real chain is six redirects, an
OAuth IP handshake and a JS app boot, covered by the live test.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fetchers.browser import (
    EbscoHandler,
    all_handlers,
    is_ebsco_target,
    resolve_by_doi,
    resolve_by_host,
)
from fetchers.browser.base import Counter
from fetchers.resolvers import FulltextTarget

ALMA_URL = "https://aalto.alma.exlibrisgroup.com/view/action/uresolver.do?x=1"
SIGNED = "https://content.ebscohost.com/cds/retrieve?content=AQIC-signed-blob"


def _good_pdf() -> bytes:
    return b"%PDF-1.4\n" + b"0" * 3000 + b"\nstartxref\n12\n%%EOF\n"


def _truncated_pdf() -> bytes:
    return b"%PDF-1.4\n" + b"0" * 3000 + b"\nstartxref\n1744085\n%%EOF\n"


def _target(package="EBSCOhost Business Source Ultimate", interface="EBSCOhost"):
    return FulltextTarget(
        url=ALMA_URL, package_name=package, interface_name=interface,
    )


# ---------------------------------------------------------------------------
# Registry placement
# ---------------------------------------------------------------------------


def test_ebsco_is_not_in_the_doi_registry() -> None:
    assert "ebsco" not in {h.name for h in all_handlers()}


def test_ebsco_is_not_reachable_by_doi_or_host() -> None:
    """Both routes must miss it, or Pass 1 would hand it items with no
    resolver target."""
    assert resolve_by_doi("10.1007/bf00382859", all_handlers()) is None or \
        resolve_by_doi("10.1007/bf00382859", all_handlers()).name != "ebsco"
    assert resolve_by_host("research.ebsco.com", all_handlers()) is None


def test_ebsco_needs_no_interactive_solve() -> None:
    """Measured: EBSCO authenticates on institutional IP with no
    interstitial, so an unattended run can use this platform."""
    assert EbscoHandler().needs_interactive_solve is False


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("interface", ["EBSCOhost", "ebscohost", "EBSCO"])
def test_detects_ebsco_by_interface_name(interface: str) -> None:
    assert is_ebsco_target(_target(interface=interface)) is True


def test_detects_ebsco_by_package_name_alone() -> None:
    """Alma spells the platform several ways across packages."""
    assert is_ebsco_target(
        _target(package="EBSCOhost Academic Search Premier", interface=""),
    ) is True


@pytest.mark.parametrize(
    ("package", "interface"),
    [
        ("JSTOR Archival Journals", "JSTOR"),
        ("ABI/INFORM Collection", "ProQuest"),
        ("FinELib SpringerLink Contemporary Journals", "Springer Link"),
    ],
)
def test_other_platforms_are_not_ebsco(package: str, interface: str) -> None:
    """The URL is identical for all of these on Alma — only the names
    differ, which is the whole point."""
    assert is_ebsco_target(_target(package, interface)) is False


def test_detects_ebsco_from_a_bare_sfx_style_url() -> None:
    """SFX emits a real EBSCOhost host and no names at all."""
    assert is_ebsco_target(
        FulltextTarget(url="https://search.ebscohost.com/login.aspx?x=1"),
    ) is True


def test_no_target_is_not_ebsco() -> None:
    """`chosen` is None whenever the resolver could not answer."""
    assert is_ebsco_target(None) is False


# ---------------------------------------------------------------------------
# Download flow
# ---------------------------------------------------------------------------


def _page(*, final_url: str = "https://research.ebsco.com/c/x/viewer/pdf/y",
          emit: str | None = SIGNED):
    """Page whose `goto` fires the viewer's own content request."""
    page = MagicMock()
    page.url = final_url
    handlers: list = []
    page.on = lambda event, fn: handlers.append(fn)
    page.remove_listener = MagicMock()

    async def _goto(url, **kw):
        if emit is not None:
            resp = MagicMock()
            resp.url = emit
            for fn in handlers:
                fn(resp)
        return MagicMock()

    page.goto = AsyncMock(side_effect=_goto)
    return page


def _ctx(body: bytes):
    ctx = MagicMock()
    resp = MagicMock()
    resp.body = AsyncMock(return_value=body)
    ctx.request.get = AsyncMock(return_value=resp)
    return ctx


def _item(**kw):
    base = {
        "doi": "10.1007/bf00382859", "item_key": "K1",
        "title": "Values Underlying Personnel/Human Resource Management",
        "resolver_target_url": ALMA_URL,
    }
    base.update(kw)
    return base


def _run(handler, page, ctx, item, tmp_path):
    """Drive the coroutine synchronously.

    `asyncio.run` rather than pytest-asyncio: that plugin is not a
    dependency here, and every other async handler test in this suite
    uses the same idiom (see test_fetchers_browser_base.py).
    """
    return asyncio.run(handler.download(
        page, ctx, item, str(tmp_path),
        counter=Counter(), total=1, t_start=0.0,
    ))


def test_captures_the_signed_url_and_writes_the_pdf(tmp_path: Path) -> None:
    """The core flow: browser observes the content request, and the bytes
    are fetched through `ctx.request` rather than a download event."""
    handler = EbscoHandler()
    ctx = _ctx(_good_pdf())
    result = _run(handler, _page(), ctx, _item(), tmp_path)
    assert result is not None
    path, url = result
    assert url == SIGNED
    assert path.read_bytes() == _good_pdf()
    ctx.request.get.assert_awaited_once()


def test_rejects_a_truncated_pdf(tmp_path: Path) -> None:
    """Validated in full, not by header alone — EBSCO serves through a CDN
    and a truncated body cached here would be attached as real."""
    handler = EbscoHandler()
    result = _run(handler, _page(), _ctx(_truncated_pdf()), _item(), tmp_path)
    assert result is None
    assert not list(tmp_path.glob("*.pdf"))


def test_rejects_html_served_as_the_content_response(tmp_path: Path) -> None:
    handler = EbscoHandler()
    body = b"<!doctype html><title>Sign in</title>"
    assert _run(handler, _page(), _ctx(body), _item(), tmp_path) is None


def test_gives_up_when_the_viewer_serves_no_pdf(tmp_path: Path) -> None:
    """Abstract-only records exist; the handler must fail rather than
    hang for the full timeout in tests."""
    handler = EbscoHandler()
    handler.response_timeout_ms = 200
    result = _run(handler, _page(emit=None), _ctx(_good_pdf()), _item(), tmp_path)
    assert result is None


def test_skips_an_item_with_no_resolver_target(tmp_path: Path) -> None:
    handler = EbscoHandler()
    item = _item()
    del item["resolver_target_url"]
    page = _page()
    assert _run(handler, page, _ctx(_good_pdf()), item, tmp_path) is None
    page.goto.assert_not_called()


def test_serves_a_cached_pdf_without_touching_the_network(
    tmp_path: Path,
) -> None:
    from fetchers.browser.base import cache_path_for

    handler = EbscoHandler()
    cache_path_for(str(tmp_path), "10.1007/bf00382859").write_bytes(_good_pdf())
    page = _page()
    result = _run(handler, page, _ctx(_good_pdf()), _item(), tmp_path)
    assert result is not None
    assert result[1].startswith("cache://")
    page.goto.assert_not_called()


# ---------------------------------------------------------------------------
# Unattended operation
# ---------------------------------------------------------------------------


def test_no_solve_handlers_skip_the_setup_prompt() -> None:
    """`needs_interactive_solve = False` must suppress the setup question,
    not merely reword the queue line.

    Found by running the handler for real: EBSCO authenticates silently on
    institutional IP, yet the driver still opened "Can you see/reach the
    PDF from this page?" and blocked. Under `--control-file` that stalls an
    unattended run until the timeout expires.
    """
    import inspect

    import enrich_pdfs

    src = inspect.getsource(enrich_pdfs._drive_handler)
    # `needs_solve_for` is the live gate; `needs_interactive_solve` is
    # only the fallback it defers to for handlers predating the hook.
    # Assert on the gate — the old flag name survives inside that
    # fallback, so matching it alone would pass even with the gate gone.
    assert "needs_solve_for" in src, (
        "_drive_handler calls setup() unconditionally; a no-solve handler "
        "will block on a prompt nobody needs to answer"
    )
    # The call must be guarded rather than merely mentioned nearby.
    guard = src.index("needs_solve_for")
    call = src.index("handler.setup(")
    assert guard < call, "the gate is read after setup() is already called"
