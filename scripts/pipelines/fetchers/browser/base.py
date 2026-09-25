"""Base classes and shared helpers for browser-based PDF handlers.

Each publisher that needs a Playwright-driven browser session gets its
own handler class in this sub-package. The base provides:

- `PublisherHandler` — the abstract interface every handler implements.
- `RequestHandler` — intermediate for publishers whose authenticated
  session lets us fetch PDFs via `ctx.request.get()` directly (fast,
  concurrent).
- `PageNavigationHandler` — intermediate for publishers whose Cloudflare
  rejects non-browser requests even with cookies; downloads happen via
  `page.goto(url)` + `expect_download` event.

The per-publisher subclasses in sibling modules need only declare
`name`, `doi_prefixes`, and `url_template` to get a working download
flow. Subclasses whose flow is not URL-substitution (INFORMS, OUP, APA)
inherit directly from `PublisherHandler` and implement `download`
from scratch.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from . import interaction
from .interaction import ask_in_daemon

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page


# ---------------------------------------------------------------------------
# Counter + display helpers (shared by all handlers).
# ---------------------------------------------------------------------------


@dataclass
class Counter:
    ok: int = 0
    cached: int = 0
    failed: int = 0
    #: Connector saves waiting for cloud sync before their merge.
    queued: int = 0

    @property
    def done(self) -> int:
        return self.ok + self.cached + self.failed + self.queued


def progress_tag(counter: Counter, total: int, t_start: float) -> str:
    """Compact `[N/total | <elapsed>s | avg X.Xs/item | ~Ys left]` string.

    Used in per-item output so the user can see throughput and ETA
    while a long publisher run is in flight.
    """
    elapsed = time.monotonic() - t_start
    done = counter.done
    if done == 0:
        return f"[{done}/{total} | {elapsed:.0f}s elapsed]"
    avg = elapsed / done
    remaining = (total - done) * avg
    return (
        f"[{done}/{total} | {elapsed:.0f}s | "
        f"avg {avg:.1f}s/item | ~{remaining:.0f}s left]"
    )


# ---------------------------------------------------------------------------
# PDF cache helpers.
# ---------------------------------------------------------------------------


def cache_path_for(cache_dir: str | Path, doi: str) -> Path:
    safe = doi.replace("/", "_").replace(":", "_")
    return Path(cache_dir) / f"{safe}.pdf"


def is_cached(path: Path) -> bool:
    """True when `path` holds what looks like a real PDF (size > 1KB and
    starts with `%PDF-`)."""
    if not path.exists() or path.stat().st_size < 1000:
        return False
    try:
        with path.open("rb") as f:
            return f.read(5) == b"%PDF-"
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Playwright glue.
# ---------------------------------------------------------------------------


async def try_click(page: Page, *selectors: str, timeout: int = 8000) -> bool:
    """Click the first *visible* element any selector resolves to.

    Returns True on the first successful click, False if nothing visible
    turns up before `timeout`. Used by the multi-step flows (APA
    PsycNET) where the control's class changes per-user.

    **`.first` is not good enough, and that cost a live run.** This used
    to take `page.locator(sel).first` and wait for *it* to become
    visible. PsycNET's record page renders two "Get Access" anchors — a
    0x0 one with no `offsetParent`, then the real 252x21 one. `.first`
    picked the hidden one every time, waited out the full timeout, moved
    to the next selector, and resolved to the same hidden element again.
    All four selectors failed, 20s each, on a page where the operator
    could see and click the button. The reported diagnosis — that
    PsycNET had changed its markup — was wrong twice over: the markup
    was unchanged and the selectors matched it.

    So: sweep every candidate of every selector and click the first one
    that is actually visible. `timeout` is now a budget for the whole
    search rather than per-selector, which also caps the worst case at
    `timeout` instead of `timeout x len(selectors)`.
    """
    deadline = time.monotonic() + timeout / 1000
    while True:
        for sel in selectors:
            loc = page.locator(sel)
            try:
                count = await loc.count()
            except Exception:
                continue
            for i in range(count):
                candidate = loc.nth(i)
                try:
                    if not await candidate.is_visible():
                        continue
                    await candidate.click()
                    return True
                except Exception:
                    # Visible a moment ago, gone or covered now — try the
                    # next candidate rather than abandoning the sweep.
                    continue
        if time.monotonic() >= deadline:
            return False
        try:
            await page.wait_for_timeout(250)
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Failure diagnostics.
# ---------------------------------------------------------------------------
#
# Every page-driven flow ends with "click the thing that yields a PDF",
# and when that click finds nothing the exception says exactly that and
# nothing more. `Download button not found` is emitted identically
# whether the browser is sitting on the right article page with a stale
# selector, or three steps away on a login screen it was silently
# redirected to. A live APA run failed 2/2 items that way; the real
# cause (an access check bouncing the session to `sso.apa.org`) was not
# recoverable from the log, because nothing recorded where the browser
# actually ended up.
#
# `capture_page_diagnostics` records that: final URL, page title, a
# screenshot, and the rendered HTML. It is cheap, runs only on the
# failure path, and every page-driven handler routes its failures
# through `PublisherHandler.report_failure` so none of them can forget.

#: Sub-directory of the PDF cache where failure artefacts are written.
#: Under the cache dir rather than the project tree because these are
#: run-local debris, sized in megabytes, that nobody wants committed.
DIAGNOSTICS_DIRNAME = "diagnostics"


def _diagnostic_stem(handler: str, doi: str) -> str:
    """Filename stem for one item's diagnostic artefacts.

    Same slash/colon escaping as `cache_path_for`, prefixed by handler so
    two publishers can never collide on a shared DOI suffix.
    """
    safe = doi.replace("/", "_").replace(":", "_") or "unknown-doi"
    return f"{handler or 'browser'}_{safe}"


async def capture_page_diagnostics(
    page: Page,
    cache_dir: str | Path,
    *,
    handler: str,
    doi: str,
    note: str = "",
) -> str:
    """Record where a failed browser flow actually ended up.

    Writes `<cache_dir>/diagnostics/<handler>_<doi>.{png,html,txt}` and
    returns a one-line `at <url> ("<title>")` summary for the console —
    the part that makes the failure classifiable without opening
    anything. Returns "" when even the URL could not be read.

    Never raises. This runs on the failure path, where an exception
    would replace the caller's real error with a worse one.
    """
    url = ""
    title = ""
    try:
        url = page.url or ""
    except Exception:
        pass
    try:
        title = (await page.title()) or ""
    except Exception:
        pass

    diag_dir = Path(cache_dir) / DIAGNOSTICS_DIRNAME
    stem = _diagnostic_stem(handler, doi)
    saved = False
    try:
        diag_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        diag_dir = None  # type: ignore[assignment]

    if diag_dir is not None:
        try:
            await page.screenshot(path=str(diag_dir / f"{stem}.png"))
            saved = True
        except Exception:
            pass
        try:
            (diag_dir / f"{stem}.html").write_text(
                await page.content(), encoding="utf-8",
            )
            saved = True
        except Exception:
            pass
        try:
            (diag_dir / f"{stem}.txt").write_text(
                "\n".join([
                    f"handler: {handler}",
                    f"doi:     {doi}",
                    f"url:     {url}",
                    f"title:   {title}",
                    f"error:   {note}",
                ]) + "\n",
                encoding="utf-8",
            )
            saved = True
        except Exception:
            pass

    if not url and not title:
        return ""
    summary = f"at {url or '<unknown url>'}"
    if title:
        summary += f' ("{title[:70]}")'
    if saved and diag_dir is not None:
        summary += f" — diagnostics: {diag_dir / stem}.*"
    return summary


def _write_chromium_prefs(user_data_dir: Path) -> None:
    """Force the bundled Chromium to download PDFs instead of opening
    them in the built-in viewer.

    Without this the `expect_download` event never fires — the PDF
    renders inline and the handler times out. The pref is written into
    the persistent profile's Default/Preferences file so it survives
    across invocations.
    """
    default_dir = user_data_dir / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)
    prefs_file = default_dir / "Preferences"
    prefs: dict[str, Any] = {}
    if prefs_file.exists():
        try:
            prefs = json.loads(prefs_file.read_text())
        except Exception:
            prefs = {}
    prefs.setdefault("plugins", {})["always_open_pdf_externally"] = True
    prefs_file.write_text(json.dumps(prefs))


#: Install command, filled with the Playwright version that will drive the
#: browser. Each release drives exactly one Chromium build, and the plugin
#: runs the newest release (uv resolves it when it rebuilds a script
#: environment), so the build on disk can fall behind. When it does, the
#: user is asked to install the matching one: see `chromium_install_problem`.
PLAYWRIGHT_INSTALL_CMD = "uvx playwright@{version} install chromium"


def _playwright_version() -> str:
    """The installed playwright package's version, or "latest"."""
    try:
        from importlib.metadata import version
        return version("playwright")
    except Exception:  # noqa: BLE001 — only feeds a hint
        return "latest"


def chromium_install_problem(executable_path: str, version: str) -> str | None:
    """None when this Playwright's Chromium build is on disk; otherwise
    the message asking the user to install it."""
    if executable_path and Path(executable_path).exists():
        return None
    cmd = PLAYWRIGHT_INSTALL_CMD.format(version=version)
    return (
        f"The browser pass runs Playwright {version}, whose Chromium build "
        f"is not installed yet (Playwright updates bring a new browser "
        f"build).\n"
        f"  Install it once, then re-run:\n"
        f"    {cmd}\n"
        f"  It downloads ~150 MB from Playwright's CDN."
    )


def check_chromium_installed() -> str | None:
    """`chromium_install_problem` for the Playwright in this environment.

    Asked up front, so a missing browser costs seconds rather than a
    whole Zotero read and resolver pre-flight before launch fails.
    Returns None when the check itself cannot run: `launch_context` still
    reports the same problem at launch, so failing open loses nothing.
    """
    import subprocess
    import sys

    # A subprocess, because tearing down a Playwright driver this soon
    # after starting it prints asyncio "Task was destroyed" noise into
    # the run's output, and the path is all we want from it.
    probe = (
        "from playwright.sync_api import sync_playwright\n"
        "with sync_playwright() as pw: print(pw.chromium.executable_path)"
    )
    try:
        out = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True, timeout=60,
        )
    except Exception:  # noqa: BLE001
        return None
    lines = out.stdout.strip().splitlines()
    if out.returncode != 0 or not lines:
        return None
    return chromium_install_problem(lines[-1].strip(), _playwright_version())


async def launch_context(
    playwright,
    cache_dir: str | Path,
    *,
    extensions: list[str | Path] | None = None,
) -> BrowserContext:
    """Persistent Chromium context with the PDF-download pref set.

    The profile lives in `<cache_dir>/.chrome-profile` so Cloudflare
    cookies and institutional SSO state survive between publisher runs
    in the same session.

    When `extensions` is given, each path is passed to Chromium via
    `--load-extension` (and `--disable-extensions-except`) so that only
    those extensions are active. Used by the Zotero Connector handler
    to load the user's Connector extension while still running
    headfully so they can solve Cloudflare challenges.
    """
    user_data_dir = Path(cache_dir) / ".chrome-profile"
    if extensions:
        # Isolate the Connector profile from the publisher-direct
        # profile — extensions loaded here would otherwise show up in
        # every subsequent browser run.
        user_data_dir = Path(cache_dir) / ".chrome-profile-connector"
    user_data_dir.mkdir(parents=True, exist_ok=True)
    _write_chromium_prefs(user_data_dir)
    args = ["--disable-blink-features=AutomationControlled"]
    if extensions:
        paths = ",".join(str(p) for p in extensions)
        args.extend([
            f"--disable-extensions-except={paths}",
            f"--load-extension={paths}",
        ])
    try:
        return await playwright.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=False,
            accept_downloads=True,
            viewport={"width": 1200, "height": 900},
            args=args,
        )
    except Exception as e:
        if "Executable doesn't exist" in str(e):
            raise RuntimeError(
                chromium_install_problem("", _playwright_version())
            ) from e
        raise


# ---------------------------------------------------------------------------
# Cloudflare clearance detection.
# ---------------------------------------------------------------------------
#
# Most of the prompts this pass fires ask a question the browser can
# already answer. The Chromium profile is persistent, so the second run
# against a publisher usually starts with a valid `cf_clearance` cookie
# and no challenge on screen — nothing for the user to solve — and the
# user is asked anyway. Non-interactive JS challenges are the same story
# within a single run: they clear themselves in a few seconds while the
# script sits at a prompt waiting for an Enter that means nothing.
#
# What is deliberately NOT automated is the judgement: whether the user
# can actually reach the PDF from this page. The probe only ever
# short-circuits the prompt in the affirmative, and a wrong "proceed"
# costs one failed download before `_prompt_on_first_failure` puts the
# same decision back in front of the human, with evidence. A wrong
# "skip" would cost the whole publisher silently, so the probe is never
# allowed to produce one — every uncertain answer falls through to the
# prompt.

#: Cookie Cloudflare issues once a challenge has been cleared. Its
#: presence for the page's own host is the only positive evidence
#: available that this session is through.
CLEARANCE_COOKIE = "cf_clearance"

#: Elements that exist only while an unsolved challenge is on screen.
#: Checked for visibility, not presence: the Turnstile widget is left in
#: the DOM, hidden, on pages that have already passed.
CHALLENGE_SELECTORS = (
    "#challenge-form",
    "#challenge-running",
    "#cf-chl-widget",
    "iframe[src*='challenges.cloudflare.com']",
)

#: Titles Cloudflare's interstitials use. Cheaper than a DOM query and
#: catches the variants whose markup differs by deployment.
CHALLENGE_TITLE_MARKERS = (
    "just a moment",
    "attention required",
    "checking your browser",
    "verifying you are human",
)


async def _challenge_is_showing(page: Page) -> bool:
    """True when the page looks like an unsolved Cloudflare interstitial.

    Errors resolve to True — "I cannot tell" must route to the human,
    not past them.
    """
    try:
        title = (await page.title() or "").lower()
    except Exception:
        return True
    if any(marker in title for marker in CHALLENGE_TITLE_MARKERS):
        return True
    for selector in CHALLENGE_SELECTORS:
        try:
            if await page.locator(selector).first.is_visible(timeout=250):
                return True
        except Exception:
            continue
    return False


async def _has_clearance_cookie(page: Page) -> bool:
    """True when a `cf_clearance` cookie covers the page's own host.

    The host check is load-bearing. One persistent profile serves every
    publisher in a run, so a cookie left by Sage would otherwise read as
    clearance for Academy of Management and skip a challenge nobody
    solved.
    """
    try:
        host = (urlparse(page.url).hostname or "").lower()
        cookies = await page.context.cookies()
    except Exception:
        return False
    if not host:
        return False
    for cookie in cookies or ():
        if cookie.get("name") != CLEARANCE_COOKIE:
            continue
        domain = str(cookie.get("domain") or "").lstrip(".").lower()
        if domain and (host == domain or host.endswith(f".{domain}")):
            return True
    return False


async def wait_for_clearance(
    page: Page, *, timeout_s: float, poll_interval_s: float = 0.5,
) -> bool:
    """Poll until the session is demonstrably clear, or give up.

    Both conditions must hold at the same moment: no challenge visible,
    and a clearance cookie for this host. Returns False on timeout, which
    the caller reads as "ask the user" rather than as failure.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        if not await _challenge_is_showing(page) and await _has_clearance_cookie(page):
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(poll_interval_s)


# ---------------------------------------------------------------------------
# Reading the publisher's own page.
# ---------------------------------------------------------------------------
#
# Requested from a live JYU run, 2026-09-25: the setup prompt asked "can
# you see/reach the PDF from this page?" about pages that already said,
# in a sentence, that the institution has no access. So the page is read
# first, and the human is asked only when it says nothing conclusive.
#
# Two rules keep this from becoming the silent skip the clearance probe
# above is forbidden to produce:
#
# - **A verdict is about one item.** A conclusive page fails *that* item
#   fast, with the page's words attached; it never skips the publisher.
#   One unentitled Sage journal says nothing about the next.
# - **Markers are sentences, per publisher, about this article.** "Get
#   access" and "Download PDF" are menu chrome on pages we *can* read —
#   Sage's and Springer's no-access pages both contain "Download PDF".
#   Where the sentence does not itself show the institution was
#   recognised, `recognised_markers` must also match, or a signed-out
#   session (which a sign-in would fix) reads as a verdict.
#
# There is deliberately no "PDF link present" class: text cannot tell
# it apart, for the reason just given.

PAGE_GONE = "gone"
PAGE_NO_ENTITLEMENT = "no_entitlement"
PAGE_UNKNOWN = "unknown"

#: File, under the diagnostics dir, that collects every conclusive page
#: so a later no-coverage verdict can cite what the publisher said.
OBSERVATIONS_FILENAME = "page_observations.jsonl"


@dataclass
class PageObservation:
    url: str
    title: str
    klass: str = PAGE_UNKNOWN
    #: The matched sentence, or `HTTP <status>` for a gone page.
    phrase: str = ""

    @property
    def conclusive(self) -> bool:
        return self.klass in (PAGE_GONE, PAGE_NO_ENTITLEMENT)

    def describe(self) -> str:
        return f'{self.klass}: "{self.phrase}" at {self.url}'


async def classify_page(
    page: Page,
    resp=None,
    *,
    denial_markers: tuple[str, ...] = (),
    recognised_markers: tuple[str, ...] = (),
) -> PageObservation:
    """Classify a publisher *landing* page. Never raises.

    `resp` is the navigation's response, for the 404/410 check. Pass it
    only for a landing page: a 404 on a URL the handler built is about
    the handler, not the article (see `setup`).

    Reads `textContent`, not `innerText`: Sage's article-level sentence
    ("Institution … does not have access to this article") sits in a
    drawer that is hidden until opened.
    """
    import re

    url = ""
    title = ""
    try:
        url = page.url or ""
    except Exception:  # noqa: BLE001
        pass
    try:
        title = (await page.title()) or ""
    except Exception:  # noqa: BLE001
        pass
    if (status := gone_status(resp)) is not None:
        return PageObservation(url, title, PAGE_GONE, f"HTTP {status}")
    if not denial_markers:
        return PageObservation(url, title)
    try:
        # Scripts dropped: a sentence in a JS template is not the page
        # saying it. (None of the saved pages had one; cheap insurance.)
        text = await page.evaluate(
            "() => { if (!document.body) return '';"
            " const b = document.body.cloneNode(true);"
            " b.querySelectorAll('script,style,noscript,template')"
            ".forEach(e => e.remove());"
            " return b.textContent; }"
        )
    except Exception:  # noqa: BLE001
        return PageObservation(url, title)
    text = " ".join(str(text or "").split())
    for pattern in denial_markers:
        m = re.search(pattern, text, re.IGNORECASE)
        if not m:
            continue
        if recognised_markers and not any(
            re.search(r, text, re.IGNORECASE) for r in recognised_markers
        ):
            return PageObservation(url, title)
        return PageObservation(url, title, PAGE_NO_ENTITLEMENT, m.group(0))
    return PageObservation(url, title)


def record_observation(
    cache_dir: str | Path, *, handler: str, doi: str, obs: PageObservation,
) -> None:
    """Append one observation to the run's JSONL. Never raises."""
    try:
        path = Path(cache_dir) / DIAGNOSTICS_DIRNAME / OBSERVATIONS_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "handler": handler, "doi": doi, "class": obs.klass,
                "phrase": obs.phrase, "url": obs.url, "title": obs.title,
            }, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


class PageVerdict(RuntimeError):
    """The publisher's page answered for this item; no download is coming."""

    def __init__(self, obs: PageObservation) -> None:
        super().__init__(f"the page says {obs.describe()}")
        self.observation = obs


def _wait_for_user(prompt: str) -> None:
    """Block until the user acknowledges the prompt.

    Delegates to the installed `InteractionChannel`. It defaults to
    `TtyChannel`, which is the original `/dev/tty` behaviour, so a run
    from a real terminal is unchanged. `enrich_pdfs.py` swaps in a
    control-file or auto-skip channel when asked, which is what lets an
    agent drive this pass without a controlling terminal — see
    `interaction.py` for why the transport was worth separating from the
    human.
    """
    interaction.get_channel().wait_for_user(prompt)


def _read_user_line(prompt: str) -> str:
    """Prompt and return the single line the user typed (stripped).

    Used where the answer matters (the y/n/A access confirmation), not
    just the Enter keystroke. Same channel indirection as
    `_wait_for_user`.
    """
    return interaction.get_channel().read_line(prompt)


# ---------------------------------------------------------------------------
# Handler base classes.
# ---------------------------------------------------------------------------


#: Chromium / Playwright network-layer error substrings, lower-cased.
#: These say the request never reached the server — the machine's
#: connectivity failed — so they are the one class of download failure
#: that carries no information whatsoever about the article.
TRANSPORT_ERROR_MARKERS: tuple[str, ...] = (
    "err_internet_disconnected",
    "err_name_not_resolved",
    "err_network_changed",
    "err_connection_reset",
    "err_connection_refused",
    "err_connection_timed_out",
    "err_connection_closed",
    "err_address_unreachable",
    "err_proxy_connection_failed",
    "err_network_io_suspended",
)


def is_transport_error(text: str) -> bool:
    """True when a failure message names a network-layer error.

    Exists because a lost connection is indistinguishable, at the call
    site, from "this publisher has nothing for this article" — both
    surface as `download()` returning None. A live run lost the network
    for four minutes and burned 193 items at ~1.2 s each, every one of
    them recorded as a fetch failure and classified UNAVAILABLE, which
    is the one cause that licenses a full-text exclusion. Not one of
    those items had been asked about.
    """
    low = (text or "").lower()
    return any(marker in low for marker in TRANSPORT_ERROR_MARKERS)


#: Playwright's own timeout wording, from `ctx.request.get(...)` and
#: `page.goto(...)`: "Timeout 60000ms exceeded."
_TIMEOUT_MARKERS: tuple[str, ...] = ("timeout", "timed out")


def is_download_timeout(text: str) -> bool:
    """True when a fetch timed out rather than being answered.

    Deliberately **not** folded into `TRANSPORT_ERROR_MARKERS`. That list
    feeds the outage breaker, which aborts the whole pass after a few
    consecutive hits; a publisher that is merely slow would trip it and
    strand the queue. This is the narrower question the *classifier*
    needs: was there an answer at all?

    Live evidence for it existing at all: a 60 s timeout on EBSCO's
    signed CDN URL — issued only *after* the viewer had loaded and
    handed over that URL, so the article demonstrably exists and is
    reachable — was classified UNAVAILABLE, the one cause that licenses
    an FE6 exclusion. `10.1287/orsc.11.4.367.14601` sat one adjudication
    pass from exclusion because a download ran long.
    """
    low = (text or "").lower()
    return any(marker in low for marker in _TIMEOUT_MARKERS)


class NetworkOutage(RuntimeError):
    """Raised when consecutive transport failures show the network is gone.

    Carries no verdict about any item. The caller stops the pass and
    leaves un-attempted items unlogged, so a re-run picks them up rather
    than a re-read of the log concluding they do not exist.
    """


#: Playwright's wording when the page, its context or the whole browser
#: is gone — "Download.save_as: Target page, context or browser has been
#: closed", seen live when Chrome for Testing 153 crashed (SIGSEGV)
#: ~14 s after launch on macOS 27, 2026-09-23.
_BROWSER_GONE_MARKERS: tuple[str, ...] = (
    "target page, context or browser has been closed",
    "browser has been closed",
    "browser has disconnected",
    "target closed",
)


def is_browser_gone(text: str) -> bool:
    """True when a failure says the browser itself died, not the item."""
    low = (text or "").lower()
    return any(marker in low for marker in _BROWSER_GONE_MARKERS)


def gone_status(resp) -> int | None:
    """The status when a navigation landed on a page that does not exist.

    Only 404 and 410. A 403 or 503 is what Cloudflare and Imperva serve
    while their challenge runs, and the page clears itself afterwards;
    treating those as final would fail every item behind a bot wall.
    """
    status = getattr(resp, "status", None)
    return status if status in (404, 410) else None


class BrowserGone(NetworkOutage):
    """The browser crashed or closed mid-run.

    One is conclusive — every later item would fail the same way — and,
    like an outage, it says nothing about any article. Before this, each
    remaining item failed in turn and the first failure asked the user
    whether they could reach the PDF, which pointed at the article.
    A subclass so every `except NetworkOutage` already stops the pass.
    """


async def ask_while_open(page, fn, *args):
    """`ask_in_daemon(fn, *args)`, abandoned with BrowserGone if `page`'s
    browser closes first.

    2026-09-24: Chrome for Testing 153 crashed on launch on macOS 27, and
    the run went on to prompt about a window that no longer existed —
    and, before `ask_in_daemon`, could not be interrupted there either.
    """
    # `is True` and the awaitable check: test doubles are MagicMocks.
    try:
        closed = page.is_closed() is True
    except Exception:  # noqa: BLE001 — a dead page may raise here too
        closed = False
    if closed:
        raise BrowserGone("the browser closed before the prompt")
    try:
        waiter = page.context.wait_for_event("close", timeout=0)
    except Exception:  # noqa: BLE001
        waiter = None
    if not inspect.isawaitable(waiter):
        return await ask_in_daemon(fn, *args)
    prompt = asyncio.ensure_future(ask_in_daemon(fn, *args))
    gone = asyncio.ensure_future(waiter)
    try:
        await asyncio.wait({prompt, gone}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (prompt, gone):
            if not t.done():
                t.cancel()
    if prompt.done() and not prompt.cancelled():
        return prompt.result()
    print("\n  The browser closed while waiting for your answer.", flush=True)
    raise BrowserGone("the browser closed while waiting at a prompt")


class PublisherHandler(ABC):
    """One handler per publisher. Subclasses set:

    - ``name``          — short identifier used in CSV and CLI.
    - ``display_name``  — human-readable name for console output.
    - ``doi_prefixes``  — DOI prefixes routed to this handler.
    - ``url_template``  — first-page URL; ``{doi}`` is substituted.
    - ``concurrency``   — max in-flight `download()` calls.
    - ``delay_s``       — delay inserted before each call (rate-limit courtesy).

    The default `setup()` opens the first URL and prompts the user to
    solve any Cloudflare challenge / sign in. Subclasses override it
    when an extra step (e.g. a cookie-banner click) is needed.

    `download()` is the only abstract method. Two intermediate bases
    (`RequestHandler`, `PageNavigationHandler`) provide the two most
    common implementations so simple publishers need only set class
    attributes.
    """

    name: str = ""
    display_name: str = ""
    doi_prefixes: tuple[str, ...] = ()
    url_template: str = ""
    #: Message from the most recent failed `download()`. Handlers print
    #: their own diagnostics and return None, which loses the reason
    #: before the orchestrator can classify it; this carries the reason
    #: back so a lost connection is not filed as a missing article. Set
    #: on every failure path, cleared on entry.
    last_error: str = ""
    #: Regexes for a sentence this publisher's page shows when *this
    #: article* is not licensed to the recognised institution. Copied
    #: from real pages, never guessed — see `classify_page`. Empty means
    #: the page is never read for a verdict.
    denial_markers: tuple[str, ...] = ()
    #: Regexes, one of which must also match before a denial counts,
    #: for publishers whose denial sentence does not itself show that
    #: the institution was recognised.
    recognised_markers: tuple[str, ...] = ()
    #: The conclusive page behind the most recent failure, or None.
    #: Cleared by the driver before each item, like `last_error`.
    last_observation: PageObservation | None = None
    #: Verdict string for the driver's classifier and retry note; set
    #: from a conclusive `last_observation`. EBSCO sets its own.
    last_verdict: str = ""
    # Optional: URL the setup phase opens in the browser. Defaults to
    # `url_template`. Override when the download URL would trigger an
    # immediate auto-download (e.g. Emerald's `?download=true` PDF URL),
    # which consumes the one-shot token and leaves the browser at
    # about:blank before the user even sees the CF challenge. The
    # landing page is usually the right choice for these publishers.
    setup_url_template: str = ""
    # Domains (hostname suffixes) that indicate SFX-reported full-text
    # access is actually reachable via THIS handler. If the library
    # reports access via an unrelated platform (JSTOR, EBSCOhost,
    # ProQuest) the SFX pre-flight will treat the item as inaccessible
    # — our handler only knows the direct-publisher path. Empty tuple
    # disables the domain filter (any full-text target counts).
    direct_access_domains: tuple[str, ...] = ()
    #: Max in-flight `download()` calls for this publisher — how many
    #: tabs `enrich_pdfs.effective_lanes` will drive it with. It caps
    #: `--browser-workers`, so it is the real ceiling and the flag can
    #: never raise a publisher past it.
    #:
    #: **1 is a finding, not an unset default.** Every publisher here is
    #: behind Cloudflare or Imperva, and several modules record what a
    #: live run measured — Sage resets sessions above ~30 requests a
    #: minute, T&F and Wiley reject `ctx.request` outright. N parallel
    #: requests from one IP is exactly the shape those systems look for,
    #: and the cost of guessing wrong is not one item: it is the
    #: publisher for the run, plus the Cloudflare clearance sitting in
    #: the shared profile that every other lane depends on. Raise this
    #: per publisher, on evidence from a live run. `EbscoHandler` is the
    #: worked example.
    concurrency: int = 1
    delay_s: float = 1.0
    # True when a run against this publisher normally requires the user
    # to clear a Cloudflare challenge or institutional SSO by hand
    # before any download works. Purely declarative — the run behaves
    # identically either way; this drives the up-front queue listing so
    # the user is told which challenges to expect *before* walking away
    # from the terminal. A live run silently under-reported this: the
    # user was told to solve Sage and AoM, was never told APA was also
    # queued, and 10 APA items were skipped without a single attempt.
    #
    # A *static* answer, which is only right for handlers whose route is
    # fixed. See `needs_solve_for` for the ones whose is not.
    needs_interactive_solve: bool = True
    # How long `setup()` waits for a Cloudflare challenge to clear on
    # its own before falling back to asking. Covers the two cases where
    # the prompt has nothing to ask about: a persistent profile that
    # still holds valid clearance, and a non-interactive JS challenge
    # that resolves in a few seconds. Set to 0 to always ask.
    clearance_timeout_s: float = 12.0
    # When True, the handler does not produce a local PDF file — it
    # attaches directly to Zotero via its own code path (the Zotero
    # Connector translator). The driver calls
    # `handler.download_and_attach(...)` instead of the standard
    # `download()` + `zot.attach_pdf()` pipeline. All existing handlers
    # leave this False (they return a local path).
    attaches_directly: bool = False

    # Intermediate base classes (`RequestHandler`, `PageNavigationHandler`)
    # set this to True so `__init_subclass__` skips the name/prefix
    # validation. Leaf handler classes leave it False (the default).
    _is_intermediate_base: bool = False

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Intermediate bases skip validation — only leaf handlers are
        # required to set `name` and `doi_prefixes`.
        if cls.__dict__.get("_is_intermediate_base", False):
            return
        if not cls.name:
            raise TypeError(f"{cls.__name__} missing class attr `name`")
        if not cls.doi_prefixes:
            raise TypeError(f"{cls.__name__} missing class attr `doi_prefixes`")
        # url_template may be empty for handlers that build URLs dynamically
        # (e.g. OUP reads it from the landing page), so don't enforce it.

    def matches_doi(self, doi: str) -> bool:
        return any(doi.startswith(p) for p in self.doi_prefixes)

    async def observe(self, page: Page, resp=None) -> PageObservation:
        """Classify `page` with this publisher's markers, and remember a
        conclusive answer as this item's verdict."""
        obs = await classify_page(
            page, resp,
            denial_markers=self.denial_markers,
            recognised_markers=self.recognised_markers,
        )
        if obs.conclusive:
            self.last_observation = obs
            self.last_verdict = f"{obs.klass}: {obs.phrase}"
            display = self.display_name or self.name
            print(f"  {display}: the page answers for this item — "
                  f"{obs.describe()}", flush=True)
        return obs

    # ------------------------------------------------------------------
    # Default setup — open first URL, prompt user.
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Per-publisher UX hints shown in the setup banner. Subclasses
    # override `setup_hint` with anything the user needs to do beyond
    # the generic Cloudflare-then-press-Enter flow — e.g. AoM often
    # needs an additional sign-in with an institutional account even
    # after CF is solved.
    # ------------------------------------------------------------------

    setup_hint: str = ""

    def _setup_url_for(self, doi: str) -> str:
        """URL opened in the browser during `setup()`.

        Falls back to `url_template` when `setup_url_template` isn't set.
        """
        tmpl = self.setup_url_template or self.url_template
        return tmpl.format(doi=doi) if tmpl else ""

    def needs_solve_for(self, items: list[dict]) -> bool:
        """Whether *this* queue needs an interactive solve before lanes open.

        `needs_interactive_solve` answers for the handler; this answers
        for the work. They differ whenever the route is chosen per item
        rather than baked into the handler: `EbscoHandler` reaches one
        library's holdings on institutional IP and another's through an
        EZproxy that demands SSO, so the same handler needs a human for
        one queue and not the next.

        Getting it wrong is costly in both directions — a needless
        prompt stalls an unattended run until the control-file timeout,
        and a missing one sends every lane into a login page at once —
        so the decision is made from the queue rather than declared.
        """
        return self.needs_interactive_solve

    async def setup(self, page: Page, first_doi: str) -> str:
        """Open the first URL and block until the user signals ready.

        Returns one of:
          - ``"proceed"`` — run downloads for this publisher.
          - ``"skip"`` — skip every item this run (no config change).
          - ``"always_skip"`` — skip every item this run AND persist
            the publisher to `[library] no_access`, so future runs
            jump straight to the Connector fallback without asking.

        Legacy bool returns from subclasses are accepted:
        ``True`` → "proceed", ``False`` → "skip".

        The prompt at the end of the banner exists so the user — the
        only reliable authority on whether the PDF is actually
        reachable from their session — can bail out early instead of
        waiting for N × 30s of download timeouts. The "Always skip"
        answer is for the case where the landing page makes it
        obvious there's no access (e.g. INFORMS's "Purchase $30"
        page with no Download PDF button) — the user knows now and
        shouldn't need to sit through a failed download to persist
        it.

        The prompt is skipped entirely when the clearance probe can
        show there is nothing to solve — see `_cleared_without_asking`.
        """
        url = self._setup_url_for(first_doi)
        if url:
            print(f"\nOpening: {url}", flush=True)
            resp = None
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            except Exception:
                # The landing page may not fully load if it's a Cloudflare
                # challenge — the user sees it anyway and solves it.
                pass
            landing_resp = resp
            if (status := gone_status(resp)) is not None:
                # A built URL the publisher does not serve — Springer
                # chapters opened at /article/ did this. Asking the user
                # whether they can reach the PDF from a "Page not found"
                # makes them diagnose our URL, so say what happened and
                # show them the DOI's real landing page instead.
                fallback = f"https://doi.org/{first_doi}"
                print(f"  HTTP {status} at {url} — opening {fallback} "
                      f"instead.", flush=True)
                fallback_resp = None
                try:
                    fallback_resp = await page.goto(
                        fallback, wait_until="domcontentloaded", timeout=20000,
                    )
                except Exception:
                    pass
                landing_resp = fallback_resp
                # Which side is wrong depends on the DOI's own landing
                # page. Wiley 10.1111/j.1440-1835.2005.tb00363.x was a
                # 404 at doi.org too, and calling that a plugin bug sent
                # the report to the wrong place.
                if gone_status(fallback_resp) is not None:
                    print(
                        "  The DOI's own landing page is gone as well: the "
                        "publisher does not serve this DOI at all. That is "
                        "about this item, not about your access to the "
                        "others.",
                        flush=True,
                    )
                elif fallback_resp is not None:
                    print(
                        "  The DOI's landing page loads, so the handler "
                        "built a URL this publisher does not use for it (a "
                        "plugin bug, not an access problem).",
                        flush=True,
                    )
            if (await self.observe(page, landing_resp)).conclusive:
                # Nothing for a human to add: the page has answered for
                # this item. "proceed", not "skip" — the verdict is this
                # item's, and every other item reads its own page.
                print("  Not asking: the remaining items are each tried "
                      "and read on their own.", flush=True)
                return "proceed"
            if await self._cleared_without_asking(page):
                return "proceed"
        self._print_setup_banner()

        answer = await ask_while_open(
            page, _read_user_line,
            "\n>>> Can you see/reach the PDF from this page?\n"
            "    [Y]es        — proceed with downloads\n"
            "    [n]o         — skip this publisher this run\n"
            "    [A]lways-skip — skip AND persist to config.toml so "
            "future runs\n"
            "                   jump straight to the Connector fallback\n"
            "> ",
        )
        a = answer.strip()
        if a == "A" or a.lower() in ("always", "always_skip", "always-skip"):
            return "always_skip"
        if a.lower() in ("n", "no", "s", "skip"):
            return "skip"
        return "proceed"

    async def _cleared_without_asking(self, page: Page) -> bool:
        """True when this publisher's session needs nothing from the user.

        Two conditions, and the first is the interesting one:

        - **`setup_hint` is empty.** A hint is the handler's own
          declaration that this publisher wants a step beyond Cloudflare
          — an institutional sign-in at AoM, a cookie banner at Emerald.
          No cookie proves any of that happened, so a handler that
          declares one always asks.
        - the clearance probe found a `cf_clearance` cookie for this host
          and no challenge on screen, within `clearance_timeout_s`.

        Anything else — a timeout, an error, a challenge still up — falls
        through to the prompt. The probe removes the question only when
        it can answer it.
        """
        if self.setup_hint or self.clearance_timeout_s <= 0:
            return False
        if not await wait_for_clearance(page, timeout_s=self.clearance_timeout_s):
            return False
        display = self.display_name or self.name
        print(
            f"  {display}: Cloudflare clearance already in place and no "
            f"challenge on screen — proceeding without asking.",
            flush=True,
        )
        return True

    def _print_setup_banner(self) -> None:
        display = self.display_name or self.name
        print("\n" + "*" * 70)
        print(f"*  {display} — preparing the browser session")
        print("*" * 70)
        print("*  A browser window titled 'Google Chrome for Testing' has")
        print("*  opened on your desktop. This is a separate, automated")
        print("*  browser used only by this script — NOT your regular")
        print("*  Chrome or Safari. Please do not close it while the script")
        print("*  is running. If you close it by accident, stop the script")
        print("*  (Ctrl-C) and re-run the same command.")
        print("*")
        print("*  To give the script an authenticated session you need to:")
        print("*    1. Click the 'Google Chrome for Testing' window.")
        print("*    2. If Cloudflare shows a challenge, solve it.")
        print("*    3. If the publisher asks you to sign in, log in with")
        print("*       your institutional account. The script reuses this")
        print("*       session for every paper from this publisher.")
        if self.setup_hint:
            print("*")
            for line in self.setup_hint.splitlines():
                print(f"*    {line}")
        print("*    4. When the page shows an article or a download, click")
        print("*       back to THIS terminal and press Enter.")
        print("*" * 70, flush=True)

    # ------------------------------------------------------------------
    # Failure reporting.
    # ------------------------------------------------------------------

    async def report_failure(
        self,
        exc: BaseException | str,
        *,
        counter: Counter,
        total: int,
        t_start: float,
        page: Page | None = None,
        cache_dir: str | Path | None = None,
        doi: str = "",
    ) -> None:
        """Count one failed item and print it with its page context.

        Every page-driven handler funnels its `except` block here so the
        console line names *where the browser was* alongside what went
        wrong. Without that pairing the two are guesswork: the same
        "button not found" text covers a stale selector on the right
        page and a silent redirect to a login screen, and only the URL
        separates them.

        `page` / `cache_dir` are optional so request-mode handlers —
        whose failure is an HTTP response, not a page state — can reuse
        the counting and formatting without capturing a misleading
        screenshot of whatever the shared page happens to show.
        """
        counter.failed += 1
        # The driver reads this to tell "no PDF here" from "nothing was
        # asked" (a dead network, a crashed browser). Only the Connector
        # and EBSCO used to set it, so neither breaker could fire for a
        # page-navigation handler.
        self.last_error = str(exc)
        detail = ""
        if page is not None and cache_dir is not None:
            detail = await capture_page_diagnostics(
                page, cache_dir, handler=self.name, doi=doi, note=str(exc),
            )
        print(
            f"  {progress_tag(counter, total, t_start)} ERROR: {str(exc)[:200]}",
            flush=True,
        )
        if detail:
            print(f"    {detail}", flush=True)

    # ------------------------------------------------------------------
    # Per-item download — the heart of each handler.
    # ------------------------------------------------------------------

    @abstractmethod
    async def download(
        self,
        page: Page,
        ctx: BrowserContext,
        item: dict,
        cache_dir: str | Path,
        *,
        counter: Counter,
        total: int,
        t_start: float,
    ) -> tuple[Path, str] | None:
        """Download one PDF.

        Returns (path, source_url) on success, None on failure. The
        driver handles retries/uploads/logging around this call.
        """


def normalise_setup_result(result: bool | str) -> str:
    """Back-compat shim for handlers whose setup() returns bool.

    True  → "proceed"
    False → "skip"
    str   → passed through (must be one of the three documented values).
    """
    if isinstance(result, bool):
        return "proceed" if result else "skip"
    return result


# ---------------------------------------------------------------------------
# RequestHandler — ctx.request.get() (fast, works when CF allows it).
# ---------------------------------------------------------------------------


class RequestHandler(PublisherHandler):
    """Handler that downloads PDFs via `ctx.request.get(url)`.

    Works only where the publisher lets the Playwright request client
    through. Cloudflare's clearance is earned by the page and does not
    reliably carry to that client — Sage failed 88/88 this way on
    2026-09-23 and moved to `PageNavigationHandler` — so no handler uses
    this today. Faster than page-nav when it works, because requests can
    run concurrently.
    """

    _is_intermediate_base = True

    async def download(self, page, ctx, item, cache_dir, *, counter, total, t_start):
        del page                          # unused in this flow
        doi = item["doi"]
        out = cache_path_for(cache_dir, doi)
        if is_cached(out):
            counter.cached += 1
            return out, f"cache://{out}"
        url = self.url_template.format(doi=doi)
        try:
            resp = await ctx.request.get(url, timeout=60000)
            body = await resp.body()
        except Exception as e:
            counter.failed += 1
            self.last_error = str(e)
            print(
                f"  {progress_tag(counter, total, t_start)} "
                f"ERROR: {str(e)[:70]}",
                flush=True,
            )
            return None

        if body[:5] == b"%PDF-":
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(body)
            counter.ok += 1
            title = (item.get("title") or "")[:50]
            print(
                f"  {progress_tag(counter, total, t_start)} "
                f"ok ({len(body) // 1024}KB) {title}",
                flush=True,
            )
            return out, url

        # Not a PDF — figure out what happened for diagnostics.
        preview = body[:2000].decode("utf-8", errors="replace").lower()
        if "just a moment" in preview or "cf-chl" in preview or "cloudflare" in preview:
            # The request client, not the page, was challenged: a window
            # showing the article with no challenge on screen is the
            # usual picture, and solving anything there will not help.
            hint = "CF challenge to the request client; the page's clearance does not carry to it"
        elif "client challenge" in preview or "incapsula" in preview:
            # Imperva/Incapsula JS interstitial — Springer's block. Named
            # explicitly because it is otherwise indistinguishable from a
            # generic failure: it arrives as HTTP 200 with a ~3 KB HTML
            # body, so it was reported as the useless "other (3038B)" and
            # read like a broken publisher rather than a bot wall.
            hint = "Imperva JS challenge"
        elif "access" in preview and (
            "denied" in preview or "not available" in preview or "subscri" in preview
        ):
            hint = "no subscription"
        elif "purchase" in preview or "buy" in preview or "rent" in preview:
            hint = "paywall"
        else:
            hint = f"other ({len(body)}B)"
        counter.failed += 1
        title = (item.get("title") or "")[:35]
        print(
            f"  {progress_tag(counter, total, t_start)} "
            f"failed {resp.status} [{hint}] {title}",
            flush=True,
        )
        # Save one diagnostic sample per publisher run so the user can
        # inspect the HTML if everything 403s.
        if counter.failed == 1:
            diag = Path(cache_dir) / "pdf_403_sample.html"
            try:
                diag.write_bytes(body)
                print(f"    (saved sample → {diag})", flush=True)
            except Exception:
                pass
        return None


# ---------------------------------------------------------------------------
# PageNavigationHandler — page.goto() + download event.
# ---------------------------------------------------------------------------


class PageNavigationHandler(PublisherHandler):
    """Handler that downloads PDFs via `page.goto()` + `expect_download`.

    Required for publishers whose Cloudflare rejects `ctx.request` even
    with valid cookies (Taylor & Francis, Wiley, AoM). Slower than
    request-mode because it serialises through the single page.

    The bundled Chromium profile has `plugins.always_open_pdf_externally`
    set, so navigation to a PDF URL fires a download event instead of
    opening the built-in viewer.
    """

    _is_intermediate_base = True

    async def download(self, page, ctx, item, cache_dir, *, counter, total, t_start):
        del ctx                           # unused; we drive `page` directly
        doi = item["doi"]
        out = cache_path_for(cache_dir, doi)
        if is_cached(out):
            counter.cached += 1
            return out, f"cache://{out}"
        url = self.url_template.format(doi=doi)
        try:
            async with page.expect_download(timeout=30000) as dl_info:
                resp = None
                try:
                    resp = await page.goto(url, wait_until="commit", timeout=15000)
                except Exception:
                    # Expected — the download event interrupts navigation.
                    pass
                if (status := gone_status(resp)) is not None:
                    # No download is coming from a "Page not found";
                    # waiting out the 30 s only to report a timeout hid
                    # what went wrong. Raising cancels the waiter.
                    raise RuntimeError(f"HTTP {status}: no page at {url}")
                if resp is not None and self.denial_markers:
                    # goto returned, so the PDF URL rendered a page
                    # instead of starting a download — Sage and Emerald
                    # redirect an unlicensed PDF to the abstract. Read it
                    # rather than wait 30 s for an event that is not
                    # coming. No `resp`: this URL is the handler's.
                    with contextlib.suppress(Exception):
                        await page.wait_for_load_state(
                            "domcontentloaded", timeout=10000,
                        )
                    obs = await self.observe(page)
                    if obs.conclusive:
                        raise PageVerdict(obs)
            dl = await dl_info.value
            await dl.save_as(str(out))
        except Exception as e:
            await self.report_failure(
                e, counter=counter, total=total, t_start=t_start,
                page=page, cache_dir=cache_dir, doi=doi,
            )
            return None

        if not is_cached(out):
            try:
                out.unlink(missing_ok=True)
            except Exception:
                pass
            counter.failed += 1
            title = (item.get("title") or "")[:45]
            print(
                f"  {progress_tag(counter, total, t_start)} "
                f"not a PDF {title}",
                flush=True,
            )
            return None

        counter.ok += 1
        size = out.stat().st_size
        title = (item.get("title") or "")[:50]
        print(
            f"  {progress_tag(counter, total, t_start)} "
            f"ok ({size // 1024}KB) {title}",
            flush=True,
        )
        return out, url


# ---------------------------------------------------------------------------
# PdfLinkNavigationHandler — landing page → extract PDF href → download.
# ---------------------------------------------------------------------------
class PdfLinkNavigationHandler(PublisherHandler):
    """Handler for platforms whose PDF URL can't be built from the DOI.

    Silverchair sites (OUP's academic.oup.com, AAA's
    publications.aaahq.org) put an opaque numeric article ID in the
    PDF path, so `url_template` points at the article *landing page*
    instead. The flow:

      1. Navigate to the landing page (`url_template`).
      2. Extract the PDF anchor's href (`pdf_link_selector`).
      3. Navigate to that href; `plugins.always_open_pdf_externally`
         turns the navigation into a download event. (`ctx.request`
         returns 403 — CF rejects non-browser requests.)
    """

    _is_intermediate_base = True

    # CSS selector(s) probed for the PDF anchor on the landing page.
    pdf_link_selector: str = (
        "a[href*='article-pdf'][href*='.pdf'], a[href*='/pdf/'][href$='.pdf']"
    )
    # How long to poll for the PDF anchor. The article toolbar renders
    # client-side after domcontentloaded; a fixed short wait proved
    # flaky on slow loads, so we poll up to this budget instead.
    pdf_link_timeout_ms: int = 15000

    def fallback_pdf_urls(self, doi: str) -> list[str]:
        """PDF URLs to try when the landing page yields no anchor.

        Default is empty, which keeps the single-candidate behaviour
        every existing subclass had. Override for platforms where the
        PDF URL *is* derivable even though the landing page is
        unreliable — the motivating case is a DOI whose `doi.org`
        redirect still points at a retired host, so there is no page to
        read a link off at all. See `CambridgeHandler`.

        Deliberately synchronous and network-free: it runs inside the
        browser handler, where an extra HTTP client would sidestep the
        shared `build_session()` retry policy.
        """
        del doi
        return []

    async def download(
        self, page, ctx, item, cache_dir,
        *, counter: Counter, total: int, t_start: float,
    ) -> tuple[Path, str] | None:
        del ctx
        doi = item["doi"]
        out = cache_path_for(cache_dir, doi)
        if is_cached(out):
            counter.cached += 1
            return out, f"cache://{out}"

        url = self.url_template.format(doi=doi)
        try:
            candidates: list[str] = []
            try:
                await page.goto(url, wait_until="domcontentloaded",
                                timeout=30000)
                # A denial on the landing page means no anchor is coming;
                # without this the selector wait ran its full 15 s first.
                # Text only, not the status: a 404 at doi.org is exactly
                # what `fallback_pdf_urls` exists to route around.
                obs = await self.observe(page)
                if obs.conclusive:
                    raise PageVerdict(obs)
                await page.wait_for_selector(
                    self.pdf_link_selector, state="attached",
                    timeout=self.pdf_link_timeout_ms,
                )
                # `a.href` (not get_attribute) so relative hrefs come
                # back absolute.
                candidates.append(await page.locator(
                    self.pdf_link_selector,
                ).first.evaluate("a => a.href"))
            except PageVerdict:
                raise
            except Exception:
                # A dead landing page is not fatal when the platform's
                # PDF URL can be derived; only the absence of *any*
                # candidate is.
                pass
            for extra in self.fallback_pdf_urls(doi):
                if extra and extra not in candidates:
                    candidates.append(extra)
            if not candidates:
                raise RuntimeError(
                    f"PDF link not found on "
                    f"{self.display_name or self.name} landing page within "
                    f"{self.pdf_link_timeout_ms // 1000}s"
                ) from None

            dl = None
            pdf_href = ""
            for cand in candidates:
                try:
                    async with page.expect_download(timeout=30000) as dl_info:
                        try:
                            await page.goto(cand, wait_until="commit",
                                            timeout=15000)
                        except Exception:
                            pass  # download event interrupts navigation
                    dl = await dl_info.value
                    pdf_href = cand
                    break
                except Exception:
                    continue
            if dl is None:
                raise RuntimeError(
                    f"No candidate URL produced a download for {doi} "
                    f"({len(candidates)} tried)"
                )
            out.parent.mkdir(parents=True, exist_ok=True)
            await dl.save_as(str(out))
        except Exception as e:
            await self.report_failure(
                e, counter=counter, total=total, t_start=t_start,
                page=page, cache_dir=cache_dir, doi=doi,
            )
            return None

        if not is_cached(out):
            try:
                out.unlink(missing_ok=True)
            except Exception:
                pass
            counter.failed += 1
            title = (item.get("title") or "")[:45]
            print(
                f"  {progress_tag(counter, total, t_start)} "
                f"not a PDF {title}",
                flush=True,
            )
            return None

        counter.ok += 1
        size = out.stat().st_size
        title = (item.get("title") or "")[:50]
        print(
            f"  {progress_tag(counter, total, t_start)} "
            f"ok ({size // 1024}KB) {title}",
            flush=True,
        )
        return out, pdf_href
