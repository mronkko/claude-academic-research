"""APA PsycNET handler — flow tests against a fake Playwright page.

The fake below models what psycnet.apa.org actually does, verified in a
real browser on 2026-08-16 against the two DOIs from the bug report
(`10.1037/apl0000007` → record `2015-01016-001`, `10.1037/a0025231` →
record `2011-19052-001`):

- `https://doi.org/{doi}` lands on `/doiLanding?doi=...`, an Angular page
  whose record link (`/record/2015-01016-001?doi=1`) carries the
  accession number.
- `/fulltext/{accession}.pdf` serves the PDF to an entitled session and
  redirects an unentitled one back to `/record/{accession}`.
- "Get Access" opens an overlay whose "CHECK ACCESS" control navigates to
  `sso.apa.org/apasso/idm/login?CheckAccess=1&UID=...` — **not** to
  `/recordAccess/institutional/`, which the handler used to wait for.

That last line is the bug. The handler waited 15s for a URL PsycNET no
longer produces, swallowed the timeout, then looked for a download button
on the SSO login form and reported `Download button not found`. These
tests pin the corrected flow and, more importantly, pin that a session
stuck at the IdP is *reported as that*.
"""

from __future__ import annotations

import ast
import asyncio
import json
import time
from pathlib import Path

import pytest
from fetchers.browser import Counter, cache_path_for
from fetchers.browser.apa import (
    _FULLTEXT_URL,
    _RECORD_ID_RE,
    ApaHandler,
    _is_landing_for,
)

PDF_BYTES = b"%PDF-1.7\n" + b"x" * 2000 + b"\nstartxref\n9\n%%EOF\n"


class _FakeApiResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def body(self) -> bytes:
        return self._payload


class _FakeRequest:
    """Models `context.request`, which shares the context's cookie jar.

    That sharing is the point: the full-text probe asks over HTTP rather
    than navigating, so an entitled session gets the PDF and an
    unentitled one gets the record page's HTML — without either answer
    costing the fully-rendered page, which used to be thrown away and
    rebuilt before "Get Access" existed again.
    """

    def __init__(self, page: _FakePsycnetPage) -> None:
        self._page = page
        self.calls: list[str] = []

    async def get(self, url: str, timeout: int = 0):
        del timeout
        self.calls.append(url)
        if "/fulltext/" in url and self._page.entitled:
            return _FakeApiResponse(PDF_BYTES)
        return _FakeApiResponse(b"<!doctype html><title>Record</title>")


class _FakeBrowserContext:
    def __init__(self, page: _FakePsycnetPage) -> None:
        self.request = _FakeRequest(page)


@pytest.fixture(autouse=True)
def _short_get_access_wait(monkeypatch):
    """Keep the suite quick.

    `try_click` polls to its deadline when nothing visible turns up, and
    the production budget for "Get Access" is 20s — measured against a
    JS app that renders it about six seconds in. Every failure-path test
    here would otherwise sit out that full budget.
    """
    import fetchers.browser.apa as apa_mod

    monkeypatch.setattr(apa_mod, "_GET_ACCESS_TIMEOUT_MS", 300)

LANDING = "https://psycnet.apa.org/doiLanding?doi=10.1037%2Fapl0000007"
RECORD_ID = "2015-01016-001"
#: A paper cited by the test article. This is the accession a live run
#: actually fetched instead of the right one, ending on its buy page.
STALE_RECORD_ID = "2010-04200-005"
SSO_URL = (
    "https://sso.apa.org/apasso/idm/login?CheckAccess=1&UID=2015-01016-001"
    "&ERIGHTS_TARGET=https%3A%2F%2Fpsycnet.apa.org%2FdoiLanding"
)
#: Where CHECK ACCESS lands an entitled session, verified live. The
#: signed link is minted here and nowhere else — the bare
#: `/fulltext/<id>.pdf` route needs the `auth_id` this page carries.
ACCESS_URL = (
    f"https://psycnet.apa.org/recordAccess/institutional/{RECORD_ID}"
    f"?returnUrl=https%253A%252F%252Fpsycnet.apa.org%252Frecord%252F{RECORD_ID}"
)
SIGNED_FULLTEXT = f"/fulltext/{RECORD_ID}.pdf?auth_id=4168"


# ---------------------------------------------------------------------------
# Fake Playwright page
# ---------------------------------------------------------------------------


class _FakeDownload:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def save_as(self, path: str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(self._payload)


class _FakeDownloadInfo:
    def __init__(self, page: _FakePsycnetPage) -> None:
        self._page = page

    @property
    def value(self):
        async def _await_download():
            payload = self._page.pop_download()
            if payload is None:
                raise TimeoutError("Timeout waiting for download event")
            return _FakeDownload(payload)
        return _await_download()


class _FakeExpectDownload:
    def __init__(self, page: _FakePsycnetPage) -> None:
        self._page = page

    async def __aenter__(self) -> _FakeDownloadInfo:
        return _FakeDownloadInfo(self._page)

    async def __aexit__(self, *exc) -> bool:
        return False                       # never suppress


class _FakeLocator:
    """Models the slice of Playwright's Locator that the code uses.

    `count`/`nth`/`is_visible` exist because `try_click` sweeps every
    candidate for the first *visible* one — it used to take `.first` and
    wait on it, which is why a hidden 0x0 duplicate of PsycNET's "Get
    Access" anchor defeated every selector. `hidden_duplicates` models
    exactly that: matches that resolve but can never be clicked.
    """

    def __init__(self, page: _FakePsycnetPage, selector: str) -> None:
        self._page = page
        self._selector = selector
        self._index = 0

    @property
    def first(self) -> _FakeLocator:
        return self.nth(0)

    def nth(self, index: int) -> _FakeLocator:
        clone = _FakeLocator(self._page, self._selector)
        clone._index = index
        return clone

    async def count(self) -> int:
        if not self._page.matches(self._selector):
            return 0
        return 1 + self._page.hidden_duplicates

    async def is_visible(self) -> bool:
        if not self._page.matches(self._selector):
            return False
        # The live page puts the 0x0 anchors *before* the real one.
        return self._index >= self._page.hidden_duplicates

    async def wait_for(self, state: str = "", timeout: int = 0) -> None:
        del state, timeout
        if not self._page.matches(self._selector):
            raise TimeoutError(f"no element for {self._selector}")

    async def click(self) -> None:
        if not await self.is_visible():
            raise TimeoutError(f"element {self._index} not visible")
        self._page.click(self._selector)

    async def get_attribute(self, name: str) -> str | None:
        del name
        if "/fulltext/" in self._selector:
            # Relative, as PsycNET serves it — the handler must resolve
            # it against the page URL.
            return SIGNED_FULLTEXT if self._page.on_access_page else None
        if "/record/" in self._selector:
            return self._page.record_href
        return None


class _FakePsycnetPage:
    """Minimal stand-in for a Playwright page driving PsycNET's flow.

    `entitled` is what an APA subscription buys: it makes
    `/fulltext/{id}.pdf` serve bytes. `entitled_after_check` models the
    session that only becomes entitled once the access check has run —
    the campus-IP case, where CHECK ACCESS bounces through the IdP and
    straight back to PsycNET.
    """

    def __init__(
        self,
        *,
        entitled: bool = False,
        entitled_after_check: bool = False,
        record_href: str | None = f"/record/{RECORD_ID}?doi=1",
        perms_href: str | None = f"http://rightslink.apa.org/journal/{RECORD_ID}",
        has_get_access: bool = True,
        has_check_access: bool = True,
        pdf_control_works: bool = False,
        hidden_duplicates: int = 0,
        stale_loads: int = 0,
        lands_on_record: bool = False,
    ) -> None:
        self.url = "about:blank"
        self.entitled = entitled
        self.entitled_after_check = entitled_after_check
        self.record_href = record_href
        self.perms_href = perms_href
        self.has_get_access = has_get_access
        self.has_check_access = has_check_access
        self.pdf_control_works = pdf_control_works
        #: Hidden 0x0 matches rendered *before* the real control, as
        #: PsycNET does for "Get Access". Zero keeps every existing test
        #: on the old shape.
        self.hidden_duplicates = hidden_duplicates
        #: How many probes serve a *different* item's landing page
        #: before this one's renders. Defence-in-depth against the race.
        self.stale_loads = stale_loads
        #: Entitled sessions skip doiLanding entirely — doi.org redirects
        #: them straight to `/record/{accession}`. Observed live.
        self.lands_on_record = lands_on_record

        self._overlay_open = False
        self._pending_url: str | None = None
        self._pending_download: bytes | None = None
        self.evaluated: list[str] = []
        self.visited: list[str] = []
        self._probes = 0

    # -- state helpers ---------------------------------------------------

    @property
    def _present(self) -> set[str]:
        tokens: set[str] = set()
        if self.record_href and "psycnet.apa.org" in self.url:
            tokens.add("/record/")
        if self.has_get_access and "sso.apa.org" not in self.url:
            tokens.add("getAccessButton")
            tokens.add("Get Access")
        if self._overlay_open and self.has_check_access:
            tokens.add("psycnet-check-access")
            tokens.add("Check Access")
        if self.pdf_control_works or "/recordaccess/" in self.url.lower():
            tokens.add("/fulltext/")
            tokens.add("Download PDF")
        return tokens

    @property
    def on_access_page(self) -> bool:
        return "/recordaccess/" in self.url.lower()

    def matches(self, selector: str) -> bool:
        return any(token in selector for token in self._present)

    def click(self, selector: str) -> None:
        if "getAccess" in selector or "Get Access" in selector:
            self._overlay_open = True
        elif "check-access" in selector or "Check Access" in selector:
            # A *pending* navigation, not an instant one. This is the
            # difference the `wait_for_url` bug lived in: clicking starts
            # the navigation, and the URL only changes once something
            # actually waits for the destination. A predicate that is
            # already true of the current page (`"psycnet.apa.org" in u`
            # on `/record/...`) returns before the page has moved, and
            # every later step then runs against the wrong page.
            if self.entitled_after_check:
                self.entitled = True
                self._pending_url = ACCESS_URL
            else:
                self._pending_url = SSO_URL
        elif "/fulltext/" in selector or "Download PDF" in selector:
            if self.pdf_control_works:
                self._pending_download = PDF_BYTES

    def pop_download(self) -> bytes | None:
        payload, self._pending_download = self._pending_download, None
        return payload

    # -- Playwright surface ----------------------------------------------

    async def goto(self, url: str, wait_until: str = "", timeout: int = 0):
        del wait_until, timeout
        self.visited.append(url)
        if url == "about:blank":
            self.url = "about:blank"
            return None
        if url.startswith("https://doi.org/") or "/doiLanding" in url:
            # Both entry shapes land the same way. `doi.org` is kept
            # modelled because it is what PsycNET redirects *from* when a
            # link elsewhere sends the browser there; the handler itself
            # now goes straight to the landing view.
            self.url = (
                f"https://psycnet.apa.org/record/{RECORD_ID}"
                if self.lands_on_record else LANDING
            )
            return None
        if "/fulltext/" in url:
            if "auth_id=" in url:
                # The signed link always serves, which is the point of it.
                self._pending_download = PDF_BYTES
                self.url = url
                return None
            if self.entitled:
                self._pending_download = PDF_BYTES
                self.url = url
            else:
                # PsycNET bounces an unentitled session to the record page.
                self.url = f"https://psycnet.apa.org/record/{RECORD_ID}"
            return None
        self.url = url
        return None

    def expect_download(self, timeout: int = 0) -> _FakeExpectDownload:
        del timeout
        return _FakeExpectDownload(self)

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self, selector)

    async def wait_for_selector(
        self, selector: str, state: str = "", timeout: int = 0,
    ):
        del state, timeout
        if not self.matches(selector):
            raise TimeoutError(f"no element for {selector}")
        return _FakeLocator(self, selector)

    async def wait_for_url(self, matcher, timeout: int = 0) -> None:
        del timeout
        if not callable(matcher):
            return
        # Playwright resolves as soon as the *current* URL matches, so a
        # predicate that merely describes where we already are returns
        # before the pending navigation lands — leaving every later step
        # on the wrong page. Order is the whole point of this model.
        if matcher(self.url):
            return
        if self._pending_url is not None and matcher(self._pending_url):
            self.url, self._pending_url = self._pending_url, None
            return
        raise TimeoutError("url never matched")

    async def wait_for_load_state(self, state: str = "", timeout: int = 0) -> None:
        del state, timeout

    async def wait_for_timeout(self, ms: int) -> None:
        del ms

    async def evaluate(self, script: str):
        self.evaluated.append(script)
        if "location.href" not in script:
            return None
        # The landing probe. Serve the stale page first when asked to,
        # exactly as PsycNET does while Angular swaps the view: a
        # previous article's DOM, whose first /record/ anchor is one of
        # its *references* and which carries no `?doi=1` marker.
        self._probes += 1
        if self._probes <= self.stale_loads:
            # A *different* DOI's landing page, fully rendered — the
            # residue of the previous item in the queue.
            return json.dumps({
                "url": "https://psycnet.apa.org/doiLanding?doi=10.1037%2Fa0025231",
                "marked": f"/record/{STALE_RECORD_ID}?doi=1",
                "perms": f"http://rightslink.apa.org/journal/{STALE_RECORD_ID}",
            })
        # `marked` mirrors the real selector
        # `a[href*='/record/'][href*='doi=1']`, so an anchor without the
        # marker — a cited paper — must not resolve it.
        href = self.record_href
        return json.dumps({
            "url": self.url,
            "marked": href if (href and "doi=1" in href) else None,
            "perms": self.perms_href,
        })

    async def title(self) -> str:
        return "Log in - American Psychological Association" \
            if "sso.apa.org" in self.url else "PsycNET Record Display"

    async def content(self) -> str:
        return f"<html><body>fake page at {self.url}</body></html>"

    async def screenshot(self, path: str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\x89PNG\r\n\x1a\n")


def _run(handler: ApaHandler, page, cache_dir: Path, counter: Counter):
    # The fake page's waits are instant, so the landing budget would
    # otherwise be spent as real wall-clock time on the timeout paths.
    handler.landing_timeout_ms = 300
    return asyncio.run(handler.download(
        page, _FakeBrowserContext(page),
        {"doi": "10.1037/apl0000007", "title": "Sinking slowly"},
        cache_dir, counter=counter, total=1, t_start=time.monotonic(),
    ))


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("/record/2015-01016-001?doi=1", "2015-01016-001"),
        ("/record/2011-19052-001?doi=1", "2011-19052-001"),
        ("https://psycnet.apa.org/record/2015-01016-001", "2015-01016-001"),
        ("/record/2001-05874-001A", "2001-05874-001A"),
    ],
)
def test_record_id_extracted_from_landing_page_href(href: str, expected: str) -> None:
    """Both DOIs from the bug report, plus the revision-suffixed form."""
    match = _RECORD_ID_RE.search(href)
    assert match is not None and match.group(1) == expected


def test_record_id_regex_ignores_unrelated_paths() -> None:
    assert _RECORD_ID_RE.search("/search/recent") is None
    assert _RECORD_ID_RE.search("/PsycARTICLES/journal/apl") is None


def test_fulltext_url_is_built_from_the_accession_number() -> None:
    assert _FULLTEXT_URL.format(record_id=RECORD_ID) == (
        "https://psycnet.apa.org/fulltext/2015-01016-001.pdf"
    )


def test_handler_no_longer_waits_for_the_retired_recordaccess_url() -> None:
    """Regression pin on the actual defect.

    `/recordAccess/institutional/` is not a URL PsycNET produces any
    more; waiting for it is what stranded the flow on the SSO login form
    and turned every APA failure into `Download button not found`.

    Checked over string *literals* rather than raw text, because the
    module docstring names both on purpose — the history is why the flow
    looks the way it does.
    """
    module = ast.parse(
        (Path(__file__).resolve().parents[2]
         / "scripts/pipelines/fetchers/browser/apa.py").read_text()
    )
    docstrings = set()
    for node in ast.walk(module):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            docstrings.add(id(first.value))
    literals = [
        node.value for node in ast.walk(module)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]
    assert not [s for s in literals if "recordAccess" in s]
    assert not [s for s in literals if "Download button not found" in s]


# ---------------------------------------------------------------------------
# Flow
# ---------------------------------------------------------------------------


def test_entitled_session_downloads_via_the_direct_fulltext_url(
    tmp_path: Path,
) -> None:
    """The whole point of route 1: no clicks, three fewer failure points."""
    page = _FakePsycnetPage(entitled=True)
    counter = Counter()
    result = _run(ApaHandler(), page, tmp_path, counter)

    assert result is not None
    path, source_url = result
    assert path.read_bytes() == PDF_BYTES
    assert source_url == "https://psycnet.apa.org/fulltext/2015-01016-001.pdf"
    assert counter.ok == 1
    # Never needed the overlay.
    assert not page._overlay_open


def test_access_check_that_grants_entitlement_then_downloads(
    tmp_path: Path,
) -> None:
    """Campus-IP case: CHECK ACCESS bounces through the IdP back to
    PsycNET, and the retried full-text URL then serves the PDF."""
    page = _FakePsycnetPage(entitled=False, entitled_after_check=True)
    counter = Counter()
    result = _run(ApaHandler(), page, tmp_path, counter)

    assert result is not None
    assert counter.ok == 1
    assert page._overlay_open, "should have gone through the click-through route"


def test_session_stranded_at_sso_says_so_instead_of_button_not_found(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """The reported bug, inverted into a test.

    Same observable situation that produced `ERROR: Download button not
    found` for 2/2 items: the access check lands on the IdP. The message
    must now name the IdP and the fix.
    """
    page = _FakePsycnetPage(entitled=False)
    counter = Counter()
    result = _run(ApaHandler(), page, tmp_path, counter)

    assert result is None
    assert counter.failed == 1
    out = capsys.readouterr().out
    assert "sso.apa.org" in out
    assert "Download button not found" not in out
    assert "sign in" in out.lower()


def test_failure_captures_url_title_and_screenshot(tmp_path: Path) -> None:
    """Backlog item 1: the log must be able to tell the three failure
    modes apart, which needs the page state recorded at failure time."""
    page = _FakePsycnetPage(entitled=False)
    _run(ApaHandler(), page, tmp_path, Counter())

    diag = tmp_path / "diagnostics"
    stem = "apa_10.1037_apl0000007"
    assert (diag / f"{stem}.png").exists()
    assert (diag / f"{stem}.html").exists()
    meta = (diag / f"{stem}.txt").read_text()
    assert "sso.apa.org" in meta
    assert "Log in - American Psychological Association" in meta


def test_failure_console_line_names_where_the_browser_ended_up(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    page = _FakePsycnetPage(entitled=False)
    _run(ApaHandler(), page, tmp_path, Counter())
    out = capsys.readouterr().out
    assert "at https://sso.apa.org/" in out
    assert "diagnostics:" in out


def test_missing_get_access_control_is_reported_distinctly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Third of the three formerly-identical failures: right page, no
    control. Must not read like the SSO or the entitlement case.

    The wording changed after a live run showed the old message —
    "PsycNET's landing markup has changed" — was actively misleading:
    the selectors were right and the control simply had not rendered
    yet. It now names the wait and points at the screenshot.
    """
    page = _FakePsycnetPage(entitled=False, has_get_access=False)
    result = _run(ApaHandler(), page, tmp_path, Counter())

    assert result is None
    out = capsys.readouterr().out
    assert "'Get Access' never appeared" in out
    assert "sso.apa.org" not in out


def test_purchase_only_article_is_reported_distinctly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    page = _FakePsycnetPage(entitled=False, has_check_access=False)
    result = _run(ApaHandler(), page, tmp_path, Counter())

    assert result is None
    assert "no 'CHECK ACCESS' control" in capsys.readouterr().out


def test_landing_page_without_a_record_link_is_reported_distinctly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    page = _FakePsycnetPage(entitled=True, record_href=None)
    result = _run(ApaHandler(), page, tmp_path, Counter())

    assert result is None
    assert "never rendered its own record link" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Record-identity safety.
#
# A live signed-in run fetched the buy page for `2010-04200-005` — a
# paper *cited by* the requested article — because the accession number
# was read off the previous item's DOM before the landing view replaced
# it. Attaching that PDF would have put the wrong full text on the right
# Zotero item, which nothing downstream checks.
# ---------------------------------------------------------------------------


def test_page_is_blanked_before_the_article_is_identified(
    tmp_path: Path,
) -> None:
    """The structural half of the fix.

    Every identification below reads the live page, so the previous
    item's DOM must be gone before any of it runs. Blanking first makes
    the mix-up impossible; the checks that follow are defence in depth.
    """
    page = _FakePsycnetPage(entitled=True)
    _run(ApaHandler(), page, tmp_path, Counter())

    assert page.visited[0] == "about:blank"
    # Straight to PsycNET's landing view; `doi.org` only ever redirected
    # here, so the hop cost a third-party round trip per item.
    assert page.visited[1].startswith(
        "https://psycnet.apa.org/doiLanding?doi="
    )
    assert "10.1037%2Fapl0000007" in page.visited[1], (
        "the DOI must be percent-encoded into the query string"
    )


def test_another_items_landing_page_is_never_mistaken_for_this_one(
    tmp_path: Path,
) -> None:
    """Defence in depth: even a fully-rendered landing page belonging to
    a different DOI must not satisfy the probe."""
    page = _FakePsycnetPage(entitled=True, stale_loads=4)
    counter = Counter()
    result = _run(ApaHandler(), page, tmp_path, counter)

    assert result is not None
    _path, source_url = result
    assert RECORD_ID in source_url
    assert STALE_RECORD_ID not in source_url
    assert counter.ok == 1


def test_entitled_session_redirected_straight_to_the_record_page(
    tmp_path: Path,
) -> None:
    """Observed live: a signed-in, entitled session never sees
    doiLanding — doi.org redirects it to `/record/{accession}`. Requiring
    the landing page cost both regression DOIs a 20s timeout."""
    page = _FakePsycnetPage(entitled=True, lands_on_record=True)
    counter = Counter()
    result = _run(ApaHandler(), page, tmp_path, counter)

    assert result is not None
    _path, source_url = result
    assert source_url == f"https://psycnet.apa.org/fulltext/{RECORD_ID}.pdf"
    assert counter.ok == 1


def test_reference_link_without_the_doi_marker_is_not_accepted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """A `/record/` anchor lacking `?doi=1` is a cited paper, not this
    article. Never a fallback — that is exactly how the wrong PDF gets
    fetched."""
    page = _FakePsycnetPage(
        entitled=True,
        record_href=f"/record/{STALE_RECORD_ID}",       # no ?doi=1
        perms_href=None,
    )
    result = _run(ApaHandler(), page, tmp_path, Counter())

    assert result is None
    assert "never rendered its own record link" in capsys.readouterr().out


def test_disagreeing_record_and_permissions_links_refuse_to_guess(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Two independent witnesses to the accession number. If they
    disagree the page is half-rendered; fetching either one risks the
    wrong article."""
    page = _FakePsycnetPage(
        entitled=True,
        perms_href=f"http://rightslink.apa.org/journal/{STALE_RECORD_ID}",
    )
    result = _run(ApaHandler(), page, tmp_path, Counter())

    assert result is None
    out = capsys.readouterr().out
    assert "inconsistent" in out
    assert "Refusing to guess" in out


@pytest.mark.parametrize(
    ("url", "doi", "expected"),
    [
        (LANDING, "10.1037/apl0000007", True),
        # Double-encoded, as it comes back through the SSO round trip.
        ("https://psycnet.apa.org/doiLanding?doi=10.1037%252Fapl0000007",
         "10.1037/apl0000007", True),
        # Previous item's landing page — same shape, different DOI.
        ("https://psycnet.apa.org/doiLanding?doi=10.1037%2Fa0025231",
         "10.1037/apl0000007", False),
        ("https://psycnet.apa.org/record/2015-01016-001",
         "10.1037/apl0000007", False),
        ("https://psycnet.apa.org/buy/2010-04200-005",
         "10.1037/apl0000007", False),
        ("https://sso.apa.org/apasso/idm/login", "10.1037/apl0000007", False),
    ],
)
def test_is_landing_for_ties_the_page_to_the_requested_doi(
    url: str, doi: str, expected: bool,
) -> None:
    assert _is_landing_for(url, doi) is expected


def test_cookie_banner_is_removed_not_accepted(tmp_path: Path) -> None:
    """The OneTrust banner is fixed at the maximum z-index and eats
    clicks. Removing it unblocks automation without consenting to
    tracking on the user's behalf."""
    page = _FakePsycnetPage(entitled=True)
    _run(ApaHandler(), page, tmp_path, Counter())

    joined = " ".join(page.evaluated)
    assert "onetrust-consent-sdk" in joined
    assert "remove()" in joined
    assert "accept" not in joined.lower()


def test_cached_pdf_short_circuits_before_any_navigation(tmp_path: Path) -> None:
    cached = cache_path_for(tmp_path, "10.1037/apl0000007")
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(PDF_BYTES)

    page = _FakePsycnetPage(entitled=False)
    counter = Counter()
    result = _run(ApaHandler(), page, tmp_path, counter)

    assert result is not None
    assert counter.cached == 1
    assert page.visited == []


def test_setup_hint_tells_the_user_about_the_sso_step() -> None:
    """A declared `setup_hint` also forces the setup prompt to ask
    rather than auto-proceed on a Cloudflare cookie — correct here,
    since no cookie proves an APA sign-in happened."""
    hint = ApaHandler().setup_hint
    assert "sso.apa.org" in hint
    assert ApaHandler().clearance_timeout_s > 0  # probe still runs, prompt still asks


def test_setup_hint_warns_that_the_session_does_not_persist() -> None:
    """Observed twice: the APA session cookie is gone after a browser
    restart, so a run that assumes last run's sign-in still holds stops
    at the login form on its first item. Users are told up front."""
    hint = ApaHandler().setup_hint
    assert "ONCE PER RUN" in hint
    assert "/record/" in hint and "doiLanding" in hint


def test_a_hidden_duplicate_control_does_not_defeat_the_click(
    tmp_path: Path,
) -> None:
    """The live defect, in one test.

    PsycNET's record page renders two "Get Access" anchors: a 0x0 one
    with no `offsetParent`, then the real 252x21 one. `try_click` took
    `page.locator(sel).first`, waited 20s for the hidden one to become
    visible, moved on to the next selector — which resolved to the same
    hidden element — and reported that PsycNET's markup had changed, on
    a page where the operator could see and click the button.

    Confirmed against the live site before fixing: `a.list-group-item.pdf`
    matched two elements, index 0 measuring 0x0 with a null
    `offsetParent`.
    """
    page = _FakePsycnetPage(
        entitled_after_check=True, hidden_duplicates=1, pdf_control_works=True,
    )
    result = _run(ApaHandler(), page, tmp_path, Counter())

    assert result is not None, "the visible control was never reached"


def test_the_visible_control_is_still_found_without_duplicates(
    tmp_path: Path,
) -> None:
    """The ordinary shape must keep working — the sweep is a superset of
    the old `.first` behaviour, not a replacement for it."""
    page = _FakePsycnetPage(entitled_after_check=True, pdf_control_works=True)
    assert _run(ApaHandler(), page, tmp_path, Counter()) is not None


def test_the_signed_link_from_the_access_page_is_what_downloads(
    tmp_path: Path,
) -> None:
    """The whole chain, end to end, in the shape the live site has.

    CHECK ACCESS lands on `/recordAccess/institutional/<id>`; that page
    carries `/fulltext/<id>.pdf?auth_id=...`; the bare `/fulltext/` URL
    cannot substitute for it.

    Two bugs hid behind this. `_run_access_check` waited on
    `"psycnet.apa.org" in url` — already true on the record page — so
    `wait_for_url` returned before the navigation and every later step
    ran against the wrong page. And the signed-link step was originally
    placed where the access page is never reached.
    """
    page = _FakePsycnetPage(entitled_after_check=True)
    result = _run(ApaHandler(), page, tmp_path, Counter())

    assert result is not None
    _path, source_url = result
    assert "auth_id=" in source_url, (
        f"downloaded from {source_url!r} rather than the signed link"
    )
    assert any("auth_id=" in v for v in page.visited), page.visited


def test_an_unentitled_session_is_still_reported_as_sso(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """The tightened `wait_for_url` predicate must not swallow the IdP
    case — that diagnosis is the one this handler exists to give."""
    page = _FakePsycnetPage(entitled=False, entitled_after_check=False)
    assert _run(ApaHandler(), page, tmp_path, Counter()) is None
    assert "sso.apa.org" in capsys.readouterr().out


def test_a_bounced_fulltext_probe_does_not_wait_for_a_download(
    tmp_path: Path,
) -> None:
    """The probe's 20s budget is for a download that might arrive. When
    PsycNET has already bounced to `/record/`, none can.

    Paid on the first item of every run and on every item whose session
    has gone cold — 20s each, for an answer the page gave immediately.
    Pinned by asserting the download event is never awaited, since the
    wall-clock saving is not observable against a fake.
    """
    page = _FakePsycnetPage(entitled=False, has_get_access=False)
    awaited: list[str] = []
    original = page.pop_download

    def _tracked():
        awaited.append("download")
        return original()

    page.pop_download = _tracked                     # type: ignore[method-assign]
    _run(ApaHandler(), page, tmp_path, Counter())

    assert awaited == [], (
        "waited on a download event after PsycNET bounced to /record/"
    )


def test_a_real_download_still_interrupts_navigation(tmp_path: Path) -> None:
    """The fast-fail must never fire on the success path. An entitled
    session's `goto` raises, because the download event interrupts the
    navigation — that is the case the guard deliberately excludes."""
    page = _FakePsycnetPage(entitled=True)
    result = _run(ApaHandler(), page, tmp_path, Counter())
    assert result is not None, "the fast-fail swallowed a real download"


def test_the_fulltext_probe_never_navigates(tmp_path: Path) -> None:
    """The answer to "why can't the overlay open as soon as the page has
    loaded?" — it can, once nothing throws the page away to ask a
    question.

    Probing by navigation left the fully-rendered record page, was
    bounced back to `/record/<id>`, and then waited for Angular to
    rebuild the entire view before "Get Access" existed again. A live
    run showed 10-20s of staring at the record page before the overlay
    appeared. `ctx.request` shares the context's cookies, so the same
    question can be asked without moving the page.
    """
    page = _FakePsycnetPage(entitled=False, has_get_access=False)
    _run(ApaHandler(), page, tmp_path, Counter())

    assert not any("/fulltext/" in v for v in page.visited), (
        f"the probe navigated: {page.visited}"
    )


def test_an_entitled_session_never_opens_the_overlay(tmp_path: Path) -> None:
    """The fast path stays fast — and is now faster still, since it
    costs no navigation at all rather than one plus a download event."""
    page = _FakePsycnetPage(entitled=True)
    result = _run(ApaHandler(), page, tmp_path, Counter())

    assert result is not None
    assert not page._overlay_open, "opened the overlay despite entitlement"
