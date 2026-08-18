"""PsycNET's institutional-access page is success, not a failure shape.

Live, on JYU VPN, every APA item failed with "No 'Get Access' control on
the PsycNET page ... Either the page did not finish rendering or
PsycNET's landing markup has changed". The saved screenshot said
otherwise: *"Your access to this content through Jyvaskylan yliopisto
has been verified!"*, above a spinner.

What actually happens:

1. `_FULLTEXT_URL` builds `/fulltext/<record_id>.pdf`, which is not a
   complete URL — the real one carries a per-session `auth_id`
   (`/fulltext/1997-06412-019.pdf?auth_id=4168&returnUrl=...`).
2. Navigating to the bare form redirects to
   `/recordAccess/institutional/<record_id>`, which verifies the
   institution and then renders a "Download PDF" link holding the signed
   URL, several seconds later.
3. The handler treated the failed probe as "not entitled" and ran
   `_run_access_check` on whatever page it was on — but that is now the
   access modal, which has no "Get Access" control, so it blamed the
   markup.

Confirmed by driving the real site: the record page renders "Get Access"
as an `<a href="#">` about six seconds after domcontentloaded, its
overlay offers "Check Access", and that lands on the access page above.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fetchers.browser import apa as apa_mod


class _Locator:
    def __init__(self, href: str | None, raises: bool = False) -> None:
        self._href, self._raises = href, raises
        self.first = self

    async def wait_for(self, **kw):
        if self._raises:
            raise TimeoutError("never attached")

    async def get_attribute(self, name):
        return self._href


class _Page:
    def __init__(self, url: str, href: str | None, raises: bool = False) -> None:
        self.url = url
        self._loc = _Locator(href, raises)
        self.navigated: list[str] = []

    def locator(self, selector: str):
        assert "/fulltext/" in selector and ".pdf" in selector, selector
        return self._loc


def _run(coro):
    return asyncio.run(coro)


def test_the_signed_link_is_taken_off_the_access_page(tmp_path, monkeypatch):
    """The auth_id is the whole point — a bare /fulltext/ URL never
    downloads, which is what sent the handler down the wrong path."""
    signed = ("/fulltext/1997-06412-019.pdf?auth_id=4168"
              "&returnUrl=https%253A%252F%252Fpsycnet.apa.org")
    page = _Page(
        "https://psycnet.apa.org/recordAccess/institutional/1997-06412-019",
        signed,
    )
    seen: list[str] = []

    async def _fake_download(pg, url, out):
        seen.append(url)
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"%PDF-1.4\n")
        return True

    monkeypatch.setattr(apa_mod, "_download_from", _fake_download)
    got = _run(apa_mod._download_from_access_page(page, tmp_path / "x.pdf"))

    assert got.startswith("https://psycnet.apa.org/fulltext/")
    assert "auth_id=4168" in got, "the signed parameter was dropped"
    assert seen == [got]


def test_a_relative_href_is_resolved_against_the_page(tmp_path, monkeypatch):
    """PsycNET serves the link relative; handing that to `goto` unchanged
    would navigate nowhere useful."""
    page = _Page(
        "https://psycnet.apa.org/recordAccess/institutional/1997-06412-019",
        "/fulltext/1997-06412-019.pdf?auth_id=1",
    )

    async def _fake_download(pg, url, out):
        assert url.startswith("https://psycnet.apa.org/"), url
        return True

    monkeypatch.setattr(apa_mod, "_download_from", _fake_download)
    assert _run(apa_mod._download_from_access_page(page, tmp_path / "x.pdf"))


def test_a_modal_that_never_loads_reports_nothing(tmp_path, monkeypatch):
    """An empty string, not an exception: the caller turns it into a
    message naming the access page, which is the useful diagnosis."""
    page = _Page(
        "https://psycnet.apa.org/recordAccess/institutional/1997-06412-019",
        None, raises=True,
    )
    monkeypatch.setattr(
        apa_mod, "_download_from",
        lambda *a, **k: pytest.fail("should not download"),
    )
    assert _run(apa_mod._download_from_access_page(page, tmp_path / "x.pdf")) == ""


def test_the_handler_recognises_the_access_path() -> None:
    """Matched case-insensitively against the lower-cased page URL —
    PsycNET spells it `/recordAccess/`, and a case-sensitive compare
    against `page.url.lower()` would never fire."""
    url = ("https://psycnet.apa.org/recordAccess/institutional/"
           "1997-06412-019?returnUrl=x").lower()
    assert apa_mod._ACCESS_PATH in url


def test_the_signed_link_is_tried_before_the_bare_fulltext_url() -> None:
    """Ordering, and a correction to an earlier attempt at this fix.

    That attempt keyed the institutional-access branch off `page.url`
    straight after the `/fulltext/` probe — but measured live, a bare
    `/fulltext/<id>.pdf` redirects to `/record/<id>`, not to the access
    page, so the branch was unreachable and shipped as dead code.

    The access page is reached only *after* "Check Access" succeeds. So
    `_download_from_access_page` belongs immediately after
    `_run_access_check` returns, and must come before the retry of the
    bare full-text URL — that URL cannot work alone, since the real one
    carries a per-session `auth_id`.
    """
    import inspect

    src = inspect.getsource(apa_mod.ApaHandler.download)
    check = src.index("_run_access_check(page)")
    signed = src.index("_download_from_access_page(page, out)")
    retry = src.rindex("_download_from(page, fulltext_url, out)")
    assert check < signed < retry, (
        "the signed-link step must sit between the access check and the "
        "bare full-text retry"
    )


def test_the_access_path_is_not_used_as_a_post_probe_branch() -> None:
    """Guards the specific dead-code mistake, which cost a whole
    debugging cycle: `_ACCESS_PATH` describes where "Check Access"
    lands, never where the `/fulltext/` probe lands."""
    import inspect

    src = inspect.getsource(apa_mod.ApaHandler.download)
    assert "_ACCESS_PATH" not in src, (
        "download() branches on _ACCESS_PATH again — the /fulltext/ probe "
        "redirects to /record/<id>, so that branch cannot fire"
    )


# --- 5. consent is the user's act, never the tool's -------------------


def test_the_handler_never_accepts_cookies_on_the_users_behalf() -> None:
    """Consent under GDPR/ePrivacy has to come from the data subject.

    A tool that clicks "Accept All Cookies" and persists that into a
    browser profile is not obtaining consent; it is manufacturing a
    record of one, for every user who installs the plugin. The banner is
    removed from the DOM so it cannot intercept clicks, and answered by
    a human during `setup()` or not at all.

    Pinned as a refusal because the practical pressure to flip it is
    real: PsycNET appears to gate its access check on OneTrust
    resolving, so accepting would plainly make retrieval work.
    """
    import inspect

    # Match the *controls*, not the words. The phrase "Accept All
    # Cookies" appears in this module's prose explaining why it is not
    # clicked, so a naive string search flags the explanation itself.
    src = inspect.getsource(apa_mod).lower()
    for selector in (
        "onetrust-accept-btn",
        "onetrust-pc-btn-handler",
        "accept-recommended-btn",
        "ot-pc-refuse-all",
    ):
        assert selector not in src, (
            f"the handler appears to answer the consent banner itself "
            f"({selector!r}); consent must be the user's act"
        )


def test_an_unanswered_banner_is_its_own_diagnosis() -> None:
    """"No entitlement" was the wrong thing to tell a user whose real
    problem was an unanswered consent dialog — they had access to both
    test articles and could download either one by hand."""
    import inspect

    src = inspect.getsource(apa_mod.ApaHandler.download)
    assert "no-consent" in src
    # Phrase-matching across an f-string that the formatter may rewrap is
    # brittle; match the two halves that carry the meaning.
    assert "cookie" in src and "consent" in src
    assert "will not answer it for you" in src


def test_setup_asks_for_the_cookie_decision_first() -> None:
    """`setup()` is the one moment a human is guaranteed to be present,
    so it is the only place the question can honestly be asked."""
    hint = apa_mod.ApaHandler.setup_hint.lower()
    assert "cookie" in hint
    assert hint.index("cookie") < hint.index("sign in")
