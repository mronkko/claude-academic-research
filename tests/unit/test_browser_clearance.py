"""The setup prompt should only fire when it has something to ask.

The browser pass asks the user one question per publisher before any
download: *can you see the PDF from this page?* It exists because of
Cloudflare — but the Chromium profile is persistent, so on the second
run against the same publisher the clearance cookie is usually still
valid and there is nothing on screen to solve. The user was asked
anyway, every run, and non-interactive JS challenges cleared themselves
while the script sat waiting for an Enter keystroke that meant nothing.

So the probe answers the mechanical half. What it must never do is
answer the *judgement* half — whether the user actually has access — in
the negative, because a wrong "skip" loses the whole publisher silently.
A wrong "proceed" costs one failed download and then
`_prompt_on_first_failure` puts the same decision back in front of the
human, with evidence. These tests pin that asymmetry.
"""

from __future__ import annotations

import asyncio

from fetchers.browser import PublisherHandler
from fetchers.browser.base import (
    _has_clearance_cookie,
    wait_for_clearance,
)


class _FakeLocator:
    def __init__(self, visible: bool) -> None:
        self._visible = visible

    @property
    def first(self) -> _FakeLocator:
        return self

    async def is_visible(self, timeout: int = 0) -> bool:
        del timeout
        return self._visible


class _FakeContext:
    def __init__(self, cookies: list[dict]) -> None:
        self._cookies = cookies

    async def cookies(self) -> list[dict]:
        return self._cookies


class _FakePage:
    """Just enough Playwright surface for the clearance probe."""

    def __init__(
        self,
        *,
        url: str = "https://journals.sagepub.com/doi/10.1177/x",
        title: str = "An article about something",
        cookies: list[dict] | None = None,
        visible_selectors: tuple[str, ...] = (),
    ) -> None:
        self.url = url
        self._title = title
        self.context = _FakeContext(cookies or [])
        self._visible = set(visible_selectors)
        self.goto_calls: list[str] = []

    async def title(self) -> str:
        return self._title

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(selector in self._visible)

    async def goto(self, url: str, **kwargs) -> None:
        del kwargs
        self.goto_calls.append(url)


def _cleared_page(**kw) -> _FakePage:
    return _FakePage(
        cookies=[{"name": "cf_clearance", "domain": ".sagepub.com"}], **kw
    )


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# The probe itself
# ---------------------------------------------------------------------------


def test_a_cleared_page_with_a_cookie_reports_clear() -> None:
    assert _run(wait_for_clearance(_cleared_page(), timeout_s=0.1)) is True


def test_no_cookie_is_not_clearance() -> None:
    """Absence of a challenge is not evidence a challenge was passed —
    a page can be quiet because it has not loaded yet."""
    page = _FakePage()
    assert _run(wait_for_clearance(page, timeout_s=0.1)) is False


def test_a_visible_challenge_widget_blocks_clearance() -> None:
    page = _cleared_page(visible_selectors=("#challenge-form",))
    assert _run(wait_for_clearance(page, timeout_s=0.1)) is False


def test_a_challenge_title_blocks_clearance_even_with_a_cookie() -> None:
    """A stale cookie plus `Just a moment...` on screen means the
    challenge is being re-run, not that it passed."""
    page = _cleared_page(title="Just a moment...")
    assert _run(wait_for_clearance(page, timeout_s=0.1)) is False


def test_a_cookie_for_another_publisher_does_not_count() -> None:
    """One persistent profile serves every publisher in a run. Without
    the host check, clearing Sage would silently clear Academy of
    Management too."""
    page = _FakePage(
        url="https://journals.aom.org/doi/10.5465/x",
        cookies=[{"name": "cf_clearance", "domain": ".sagepub.com"}],
    )
    assert _run(_has_clearance_cookie(page)) is False


def test_a_parent_domain_cookie_covers_the_subdomain() -> None:
    page = _FakePage(
        url="https://journals.sagepub.com/doi/10.1177/x",
        cookies=[{"name": "cf_clearance", "domain": ".sagepub.com"}],
    )
    assert _run(_has_clearance_cookie(page)) is True


def test_a_page_that_raises_never_reports_clear() -> None:
    """"I cannot tell" has to route to the human, not past them."""

    class _Exploding(_FakePage):
        async def title(self) -> str:
            raise RuntimeError("page closed")

    page = _Exploding(cookies=[{"name": "cf_clearance", "domain": ".sagepub.com"}])
    assert _run(wait_for_clearance(page, timeout_s=0.1)) is False


def test_clearance_arriving_mid_poll_is_picked_up() -> None:
    """The self-clearing JS challenge: the whole reason to poll rather
    than probe once."""

    class _ClearsLater(_FakePage):
        def __init__(self) -> None:
            super().__init__(title="Just a moment...")
            self.context = _FakeContext(
                [{"name": "cf_clearance", "domain": ".sagepub.com"}],
            )
            self._checks = 0

        async def title(self) -> str:
            self._checks += 1
            return "Just a moment..." if self._checks < 3 else "The article"

    page = _ClearsLater()
    assert _run(
        wait_for_clearance(page, timeout_s=5.0, poll_interval_s=0.01)
    ) is True


# ---------------------------------------------------------------------------
# How setup() uses it
# ---------------------------------------------------------------------------


class _Handler(PublisherHandler):
    name = "fake"
    display_name = "Fake Publisher"
    doi_prefixes = ("10.9999/",)
    url_template = "https://journals.sagepub.com/doi/{doi}"
    # The real default is a wait for a human-scale event; here it only
    # needs to be long enough to prove the fall-through happens.
    clearance_timeout_s = 0.05

    async def download(self, page, ctx, item, cache_dir, *, counter, total,
                       t_start):
        raise AssertionError("not exercised here")


class _HandlerWithHint(_Handler):
    name = "fake_hint"
    setup_hint = "Sign in with your institutional account first."


def test_setup_skips_the_prompt_when_the_session_is_already_clear(capsys) -> None:
    page = _cleared_page()
    result = _run(_Handler().setup(page, "10.9999/x"))

    assert result == "proceed"
    assert page.goto_calls, "the setup URL is still opened"
    out = capsys.readouterr().out
    assert "proceeding without asking" in out
    assert "Can you see/reach the PDF" not in out


def test_setup_still_asks_when_no_clearance_is_evident(monkeypatch) -> None:
    asked: list[str] = []

    def _fake_read(prompt: str) -> str:
        asked.append(prompt)
        return "y"

    monkeypatch.setattr("fetchers.browser.base._read_user_line", _fake_read)
    result = _run(_Handler().setup(_FakePage(), "10.9999/x"))

    assert result == "proceed"
    assert asked, "an unproven session must still reach the user"


def test_a_handler_with_a_setup_hint_always_asks(monkeypatch) -> None:
    """A hint is the handler saying this publisher needs something beyond
    Cloudflare — an SSO login, a cookie banner. No cookie proves that
    happened, so the cookie must not stand in for it."""
    asked: list[str] = []

    def _fake_read(prompt: str) -> str:
        asked.append(prompt)
        return "y"

    monkeypatch.setattr("fetchers.browser.base._read_user_line", _fake_read)
    _run(_HandlerWithHint().setup(_cleared_page(), "10.9999/x"))

    assert asked, "a handler declaring a setup_hint must not auto-proceed"


def test_the_probe_can_be_disabled_per_handler(monkeypatch) -> None:
    asked: list[str] = []
    monkeypatch.setattr(
        "fetchers.browser.base._read_user_line",
        lambda prompt: (asked.append(prompt), "y")[1],
    )

    class _AlwaysAsk(_Handler):
        name = "fake_always"
        clearance_timeout_s = 0

    _run(_AlwaysAsk().setup(_cleared_page(), "10.9999/x"))
    assert asked


def test_the_default_wait_is_short_enough_to_be_worth_it() -> None:
    """Long enough for a self-clearing JS challenge, short enough that a
    publisher needing a real solve reaches the user quickly. A default
    that drifted up would turn every publisher into a silent stall."""
    assert 5 <= PublisherHandler.clearance_timeout_s <= 30


def test_the_probe_never_answers_skip(monkeypatch) -> None:
    """The asymmetry that makes this safe: the probe can only ever say
    "proceed". Every other outcome goes to the user, whose answer is the
    only one that can skip a publisher."""
    monkeypatch.setattr(
        "fetchers.browser.base._read_user_line", lambda prompt: "n",
    )
    assert _run(_Handler().setup(_FakePage(), "10.9999/x")) == "skip"
