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
import json
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

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

    @property
    def done(self) -> int:
        return self.ok + self.cached + self.failed


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
    """Click the first selector that resolves to a visible element.

    Returns True on the first successful click, False if every selector
    fails. Used by the multi-step flows (APA PsycNET) where the button's
    class changes per-user.
    """
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            await loc.wait_for(state="visible", timeout=timeout)
            await loc.click()
            return True
        except Exception:
            continue
    return False


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
    return await playwright.chromium.launch_persistent_context(
        user_data_dir=str(user_data_dir),
        headless=False,
        accept_downloads=True,
        viewport={"width": 1200, "height": 900},
        args=args,
    )


def _wait_for_user(prompt: str) -> None:
    """Block until the user presses Enter on the controlling TTY.

    Reads from /dev/tty so a piped stdin doesn't auto-consume the
    prompt — the browser flow is interactive by design.
    """
    sys.stdout.write(prompt)
    sys.stdout.flush()
    try:
        with open("/dev/tty") as tty:
            tty.readline()
    except Exception:
        sys.stdin.readline()


def _read_user_line(prompt: str) -> str:
    """Prompt and return the single line the user typed (stripped).

    Same /dev/tty-first behaviour as `_wait_for_user`; used when the
    answer matters (e.g. y/n for access confirmation), not just the
    Enter keystroke.
    """
    sys.stdout.write(prompt)
    sys.stdout.flush()
    try:
        with open("/dev/tty") as tty:
            return tty.readline().strip()
    except Exception:
        return sys.stdin.readline().strip()


# ---------------------------------------------------------------------------
# Handler base classes.
# ---------------------------------------------------------------------------


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
    concurrency: int = 1
    delay_s: float = 1.0
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
        """
        url = self._setup_url_for(first_doi)
        if url:
            print(f"\nOpening: {url}", flush=True)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            except Exception:
                # The landing page may not fully load if it's a Cloudflare
                # challenge — the user sees it anyway and solves it.
                pass
        self._print_setup_banner()

        answer = await asyncio.to_thread(
            _read_user_line,
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

    Works for publishers where a Cloudflare-blessed session lets the
    Playwright request client through (Emerald, Sage). Faster than
    page-nav because requests can run concurrently.
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
            hint = "CF challenge"
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
                try:
                    await page.goto(url, wait_until="commit", timeout=15000)
                except Exception:
                    # Expected — the download event interrupts navigation.
                    pass
            dl = await dl_info.value
            await dl.save_as(str(out))
        except Exception as e:
            counter.failed += 1
            print(
                f"  {progress_tag(counter, total, t_start)} "
                f"ERROR: {str(e)[:70]}",
                flush=True,
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
