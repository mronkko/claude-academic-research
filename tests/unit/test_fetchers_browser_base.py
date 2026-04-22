"""Unit tests for fetchers/browser/base.py and the registry stub.

These tests don't launch Playwright — they exercise the ABC enforcement,
registry dispatch, and the small pure helpers (progress_tag, is_cached).
Live-browser behaviour is covered by tests/live/test_browser_publishers.py.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fetchers.browser import (
    Counter,
    PageNavigationHandler,
    PublisherHandler,
    RequestHandler,
    all_handlers,
    cache_path_for,
    is_cached,
    progress_tag,
    resolve_by_doi,
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def test_counter_done_is_sum_of_ok_cached_failed() -> None:
    c = Counter(ok=3, cached=2, failed=1)
    assert c.done == 6


def test_progress_tag_handles_zero_done() -> None:
    c = Counter()
    tag = progress_tag(c, total=5, t_start=time.monotonic())
    assert "0/5" in tag
    assert "elapsed" in tag


def test_progress_tag_shows_avg_and_eta_once_done_is_positive() -> None:
    c = Counter(ok=2)
    # t_start 4s ago → avg 2s/item, 3 remaining → ~6s left
    tag = progress_tag(c, total=5, t_start=time.monotonic() - 4.0)
    assert "2/5" in tag
    assert "avg" in tag
    assert "left" in tag


def test_cache_path_for_escapes_slash_and_colon_in_doi() -> None:
    p = cache_path_for("/tmp/cache", "10.1002/smj.1234:v2")
    assert p.name == "10.1002_smj.1234_v2.pdf"
    assert p.parent == Path("/tmp/cache")


def test_is_cached_false_when_missing(tmp_path: Path) -> None:
    assert is_cached(tmp_path / "nope.pdf") is False


def test_is_cached_false_when_too_small(tmp_path: Path) -> None:
    p = tmp_path / "tiny.pdf"
    p.write_bytes(b"%PDF-1.0")            # <1KB
    assert is_cached(p) is False


def test_is_cached_true_on_real_pdf(tmp_path: Path) -> None:
    p = tmp_path / "real.pdf"
    p.write_bytes(b"%PDF-1.7\n" + b"x" * 2000)
    assert is_cached(p) is True


def test_is_cached_false_on_html_masquerade(tmp_path: Path) -> None:
    """A Cloudflare challenge page that's >1KB but not PDF must still
    be rejected — the magic-bytes check is the real gate."""
    p = tmp_path / "cf.html"
    p.write_bytes(b"<!DOCTYPE html>" + b"<" * 2000)
    assert is_cached(p) is False


# ---------------------------------------------------------------------------
# ABC enforcement
# ---------------------------------------------------------------------------


def test_publisher_handler_cannot_be_instantiated_directly() -> None:
    """Abstract — can't create without implementing `download`."""
    with pytest.raises(TypeError):
        PublisherHandler()                # type: ignore[abstract]


def test_request_handler_subclass_needs_name_and_prefixes() -> None:
    with pytest.raises(TypeError, match="missing class attr"):
        class BadNoName(RequestHandler):
            doi_prefixes = ("10.0/",)
            url_template = "https://example.com/{doi}"


def test_request_handler_subclass_needs_prefixes() -> None:
    with pytest.raises(TypeError, match="missing class attr"):
        class BadNoPrefixes(RequestHandler):
            name = "bad"
            url_template = "https://example.com/{doi}"


def test_leaf_subclass_with_all_attrs_instantiates() -> None:
    class GoodHandler(RequestHandler):
        name = "good"
        display_name = "Good Publisher"
        doi_prefixes = ("10.9999/",)
        url_template = "https://example.com/{doi}"

    h = GoodHandler()
    assert h.name == "good"
    assert h.matches_doi("10.9999/paper.123")
    assert not h.matches_doi("10.1000/other")


def test_request_handler_itself_is_not_a_leaf() -> None:
    """`RequestHandler` has no `name` but is an intermediate base and
    must NOT trigger the __init_subclass__ check.  If it did, this
    import would have failed at module load."""
    # Reaching this line means import succeeded; add an assertion so
    # pytest logs something useful.
    assert RequestHandler.name == ""


def test_page_navigation_handler_itself_is_not_a_leaf() -> None:
    assert PageNavigationHandler.name == ""


# ---------------------------------------------------------------------------
# Registry dispatch
# ---------------------------------------------------------------------------


def test_all_handlers_returns_list() -> None:
    """In step 1 the registry is empty; later steps populate it. The
    function shape must stay stable either way."""
    result = all_handlers()
    assert isinstance(result, list)


def test_resolve_by_doi_returns_none_on_empty_registry() -> None:
    assert resolve_by_doi("10.1234/x.y") is None


def test_resolve_by_doi_dispatches_by_prefix() -> None:
    class FooHandler(RequestHandler):
        name = "foo"
        doi_prefixes = ("10.1001/", "10.1002/")
        url_template = "https://foo/{doi}"

    class BarHandler(RequestHandler):
        name = "bar"
        doi_prefixes = ("10.2000/",)
        url_template = "https://bar/{doi}"

    handlers: list[PublisherHandler] = [FooHandler(), BarHandler()]

    foo = resolve_by_doi("10.1001/a", handlers)
    assert foo is not None and foo.name == "foo"
    foo2 = resolve_by_doi("10.1002/b", handlers)
    assert foo2 is not None and foo2.name == "foo"
    bar = resolve_by_doi("10.2000/c", handlers)
    assert bar is not None and bar.name == "bar"
    assert resolve_by_doi("10.9999/d", handlers) is None


def test_resolve_by_doi_uses_first_match_on_collision() -> None:
    """If two handlers claim overlapping prefixes (shouldn't happen in
    the real registry), the first one wins — deterministic behaviour
    matters more than correctness of the ambiguous case."""
    class FirstH(RequestHandler):
        name = "first"
        doi_prefixes = ("10.1111/",)
        url_template = "https://a/{doi}"

    class SecondH(RequestHandler):
        name = "second"
        doi_prefixes = ("10.1111/",)
        url_template = "https://b/{doi}"

    handlers: list[PublisherHandler] = [FirstH(), SecondH()]
    result = resolve_by_doi("10.1111/x", handlers)
    assert result is not None and result.name == "first"


# ---------------------------------------------------------------------------
# RequestHandler.download (mock ctx.request; no real network)
# ---------------------------------------------------------------------------


class _TestRequestHandler(RequestHandler):
    name = "_test"
    doi_prefixes = ("10.9999/",)
    url_template = "https://example.com/{doi}.pdf"


def test_request_handler_returns_path_on_pdf_body(tmp_path: Path) -> None:
    h = _TestRequestHandler()
    item = {"doi": "10.9999/a", "title": "Test"}
    counter = Counter()

    async def fake_body():
        return b"%PDF-1.7\n" + b"x" * 2000

    fake_resp = MagicMock()
    fake_resp.body = fake_body
    fake_resp.status = 200

    ctx = MagicMock()

    async def fake_get(_url, timeout=0):
        del timeout
        return fake_resp
    ctx.request.get = fake_get

    result = asyncio.run(h.download(
        page=None, ctx=ctx, item=item, cache_dir=tmp_path,
        counter=counter, total=1, t_start=time.monotonic(),
    ))
    assert result is not None
    path, source = result
    assert path.exists()
    assert counter.ok == 1
    assert source.endswith("/10.9999/a.pdf")


def test_request_handler_returns_none_on_non_pdf(tmp_path: Path) -> None:
    h = _TestRequestHandler()
    item = {"doi": "10.9999/a", "title": "Test"}
    counter = Counter()

    async def fake_body():
        return b"<html><body>Cloudflare challenge: just a moment</body></html>"

    fake_resp = MagicMock()
    fake_resp.body = fake_body
    fake_resp.status = 403

    ctx = MagicMock()
    async def fake_get(_url, timeout=0):
        del timeout
        return fake_resp
    ctx.request.get = fake_get

    result = asyncio.run(h.download(
        page=None, ctx=ctx, item=item, cache_dir=tmp_path,
        counter=counter, total=1, t_start=time.monotonic(),
    ))
    assert result is None
    assert counter.failed == 1


def test_request_handler_uses_cache_on_second_call(tmp_path: Path) -> None:
    """If the PDF is already cached, download() short-circuits without
    hitting the network. Prevents a rate-limited re-run from being
    slower than necessary."""
    h = _TestRequestHandler()
    item = {"doi": "10.9999/cached", "title": "Cached"}

    cached = cache_path_for(tmp_path, "10.9999/cached")
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(b"%PDF-1.7\n" + b"y" * 2000)

    counter = Counter()
    ctx = MagicMock()
    ctx.request.get = MagicMock(side_effect=AssertionError("must not hit network"))

    result = asyncio.run(h.download(
        page=None, ctx=ctx, item=item, cache_dir=tmp_path,
        counter=counter, total=1, t_start=time.monotonic(),
    ))
    assert result is not None
    assert counter.cached == 1
    assert counter.ok == 0
