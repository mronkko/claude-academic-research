"""Zotero Connector fallback handler.

The per-publisher handlers (AoM, Wiley, Sage, …) only know the
direct-publisher URL for each paper. When the library's SFX resolver
reports that full text is available on a third-party platform
(EBSCOhost, JSTOR, ProQuest, Project MUSE, …) we have no hand-coded
download path — writing one per platform is an ongoing maintenance tax
(each platform's page structure drifts over time).

The Zotero community already maintains translators for hundreds of
these platforms as part of the Zotero Connector. This handler opens
the SFX target URL in a Playwright-driven Chromium with the user's
Zotero Connector extension loaded, then invokes the same save path
the toolbar button uses — `Zotero.Connector_Browser.saveWithTranslator`
inside the extension's Manifest V3 service worker.

The Connector saves the article as a NEW Zotero item (it has no way
to know which existing item corresponds to the DOI). This handler
deduplicates by polling Zotero for the new item, then calling
`ZoteroClient.merge_duplicate_item` to move children into the
existing item and trash the duplicate parent.

Design reference:
  - Proof-of-concept: temp/open_zotero_browser.py (tried 5 approaches;
    only the service-worker `saveWithTranslator` call worked).
  - Notes: temp/ZOTERO_AUTOMATION_NOTES.md.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import functools
import os
import re
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from .base import (
    Counter,
    PublisherHandler,
    _read_user_line,
)

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page, Worker

# Chrome extension ID for the Zotero Connector. Stable — Zotero ships
# the Connector under this ID on all three platforms. The per-version
# subdirectory lives inside this folder.
_CONNECTOR_EXT_ID = "ekhagklcjbdpajgpjgmbionohlpdbjgc"

# Zotero Desktop connector ping endpoint. Returns 200 with a small JSON
# payload when the desktop app is running and its connector server is
# enabled (default). If this is unreachable, translators can load but
# saveWithTranslator will never actually deposit anything.
_CONNECTOR_PING_URL = "http://127.0.0.1:23119/connector/ping"

#: How long to wait for Zotero Desktop to write the Connector's save.
#:
#: Was 120s, and a live run over 304 items showed saves landing *at* that
#: deadline — the log repeatedly printed "117s elapsed, ~2s remaining"
#: and then declared failure for items that had in fact saved, leaving an
#: unmerged duplicate holding the PDF. The wait covers Zotero fetching
#: the PDF as well as writing the record, so it scales with file size and
#: publisher latency, not just with the user clicking a picker.
_SAVE_POLL_TIMEOUT_S = 240.0


def _default_extension_search_paths() -> list[Path]:
    """Platform-default folders the Zotero Connector unpacks into.

    Returned in probe order — first existing path wins. macOS first
    (the plugin's current primary platform), then Linux, then Windows.
    """
    home = Path.home()
    candidates: list[Path] = [
        home / "Library" / "Application Support" / "Google" / "Chrome"
        / "Default" / "Extensions" / _CONNECTOR_EXT_ID,
        home / ".config" / "google-chrome" / "Default"
        / "Extensions" / _CONNECTOR_EXT_ID,
    ]
    # Windows: %LOCALAPPDATA%\Google\Chrome\User Data\Default\Extensions\<id>
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidates.append(
            Path(local_appdata) / "Google" / "Chrome" / "User Data"
            / "Default" / "Extensions" / _CONNECTOR_EXT_ID,
        )
    return candidates


def resolve_connector_extension_path(
    explicit: str | Path | None = None,
) -> Path | None:
    """Locate an unpacked Zotero Connector extension on disk.

    Resolution order:
      1. `explicit` (from `[zotero_connector] extension_dir` in config),
         if given. When it points at the extension base folder we pick
         the highest-versioned subdir; when it already points at a
         version subdir we return it as-is.
      2. Platform defaults (macOS → Linux → Windows).

    **An explicit path that does not resolve falls through to step 2**
    rather than failing outright. Chrome auto-updates the Connector and
    deletes the superseded version's folder, so a config value pinned to
    a version subdirectory (`.../<ext-id>/5.0.200_0`) turns into a dead
    path at the next update. Returning None there reported "extension
    not found" while a working install sat one directory up, and the
    only cure was hand-editing config.toml. Preferring the explicit
    value without trusting it to the exclusion of a working install is
    what makes that self-healing.

    Returns None when nothing is found; callers surface a user-facing
    install hint.
    """
    return _resolve(explicit, [])


def _latest_version_subdir(base: Path, blocked: list[Path]) -> Path | None:
    """`base` itself when it is a version folder, else its highest
    version subfolder; None when neither exists or it cannot be read.

    A folder the OS refuses to list is appended to `blocked` and treated
    as absent, so resolution moves on to the other candidates. macOS
    privacy protection does this to Chrome's app data when the calling
    terminal or editor has not been granted access: the folder exists,
    and only listing it fails. Raised, that PermissionError escaped from
    the handler's constructor and ended an attended run after Pass 2.
    """
    try:
        if not base.exists():
            return None
        # An extension base folder contains one subdir per installed
        # version (e.g. "5.0.130_0"). If the caller passed a path that
        # already looks like a version folder (contains manifest.json),
        # return it directly.
        if (base / "manifest.json").exists():
            return base
        subs = [d for d in base.iterdir() if d.is_dir()]
    except OSError:
        blocked.append(base)
        return None
    if not subs:
        return None
    # Numerically: as strings "5.0.99_0" beats "5.0.215_0".
    subs.sort(key=lambda p: [int(n) for n in re.findall(r"\d+", p.name)])
    return subs[-1]


def _resolve(explicit: str | Path | None, blocked: list[Path]) -> Path | None:
    if explicit:
        resolved = _latest_version_subdir(Path(explicit).expanduser(), blocked)
        if resolved is not None:
            return resolved
        # Fall through to the platform defaults rather than returning
        # None — see the docstring. A stale pin must not mask a working
        # install.
    for candidate in _default_extension_search_paths():
        result = _latest_version_subdir(candidate, blocked)
        if result is not None:
            return result
    return None


def connector_extension_problem(explicit: str | Path | None = None) -> str | None:
    """Why the Connector extension cannot be used although it is
    installed, or None.

    Only the "exists but this process may not read it" case: a missing
    install has its own message where the handler starts. Callers ask
    this before any work, since an attended run that reaches the
    Connector pass only to fail there has already cost the user a
    session.
    """
    blocked: list[Path] = []
    if _resolve(explicit, blocked) is not None or not blocked:
        return None
    where = "\n".join(f"    {p}" for p in dict.fromkeys(blocked))
    return (
        "The Zotero Connector extension is installed, but this process is "
        "not allowed to read its folder:\n"
        f"{where}\n"
        "  macOS privacy protection blocks Chrome's app data from the app "
        "that launched this run (Terminal, iTerm, VS Code, …). Either:\n"
        "  • grant that app access in System Settings → Privacy & Security "
        "→ Full Disk Access (or App Management), then restart it; or\n"
        "  • copy the extension's version folder somewhere readable and "
        "point [zotero_connector] extension_dir at the copy."
    )


class PendingMerges:
    """Connector saves waiting for cloud sync before they can be merged.

    A JSON list in the cache directory, so a pair queued by one run is
    merged by the next: that is what the old "will be auto-merged next
    time" message promised and nothing implemented. Rows are
    `{"keeper", "new_key", "doi", "queued_at"}`; one per `new_key`.
    """

    FILENAME = "connector_pending_merges.json"

    def __init__(self, cache_dir) -> None:
        self.path = Path(cache_dir) / self.FILENAME
        self._rows: list[dict] = []
        if self.path.exists():
            try:
                import json
                rows = json.loads(self.path.read_text())
                self._rows = [r for r in rows if isinstance(r, dict)]
            except Exception:  # noqa: BLE001 — a corrupt queue is empty
                self._rows = []

    def rows(self) -> list[dict]:
        return list(self._rows)

    def keepers(self) -> set[str]:
        return {r.get("keeper", "") for r in self._rows} - {""}

    def new_keys(self) -> frozenset[str]:
        return frozenset(r.get("new_key", "") for r in self._rows) - {""}

    def add(self, *, keeper: str, new_key: str, doi: str) -> None:
        if new_key in self.new_keys():
            return
        self._rows.append({
            "keeper": keeper, "new_key": new_key, "doi": doi,
            "queued_at": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
        })
        self._save()

    def remove(self, new_key: str) -> None:
        self._rows = [r for r in self._rows if r.get("new_key") != new_key]
        self._save()

    def _save(self) -> None:
        import json
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f".json.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self._rows, indent=1))
        tmp.replace(self.path)


#: How long a queued save may sit without growing a PDF child before the
#: queue gives up on it. Upload lag was minutes, not hours; a save still
#: PDF-less after this is a metadata-only page, and keeping it queued
#: would stop its keeper from ever being tried again.
PENDING_GRACE_S = 6 * 3600


def settle_pending_merges(
    zot, pending: PendingMerges, *, merge, wait_s: float, on_merged,
    on_given_up=None, sweep_every_s: float = 15.0,
) -> None:
    """Merge every queued pair whose new item has reached the merge's
    surface (`_merge_surface`): Desktop when local writes are on, else
    the cloud.

    Sweeps until the queue is empty or `wait_s` has passed (0: one
    sweep). `merge(keeper, new_key)` does the merge and returns its
    stats; `on_merged(row, stats)` lets the caller log and finish a
    `--replace` swap. A pair whose merge raises stays queued.
    """
    deadline = time.monotonic() + wait_s
    first = True
    while pending.rows() and (first or time.monotonic() < deadline):
        if not first:
            time.sleep(sweep_every_s)
        first = False
        for row in pending.rows():
            try:
                visible = bool(_merge_surface(zot).item(row["new_key"]))
            except Exception:  # noqa: BLE001 — not synced yet
                visible = False
            if not visible:
                continue
            has_pdf = _pdf_child_settled(zot, row["new_key"])
            if not has_pdf:
                if _age_s(row) > PENDING_GRACE_S:
                    pending.remove(row["new_key"])
                    print(f"  Queued save {row['new_key']} still has no PDF after "
                          f"{PENDING_GRACE_S // 3600} h; giving up on it (left in "
                          f"place, not trashed). {row['keeper']} will be tried "
                          f"again.", flush=True)
                    if on_given_up is not None:
                        on_given_up(row)
                continue
            try:
                stats = merge(row["keeper"], row["new_key"])
            except Exception as e:  # noqa: BLE001
                print(f"  Queued merge {row['new_key']} → {row['keeper']} "
                      f"failed: {str(e)[:100]}; left queued.", flush=True)
                continue
            pending.remove(row["new_key"])
            on_merged(row, stats)
        if pending.rows() and time.monotonic() < deadline:
            left = int(deadline - time.monotonic())
            print(f"  {len(pending.rows())} queued merge(s) still waiting for "
                  f"cloud sync (~{left}s left)…", flush=True)


def _age_s(row: dict) -> float:
    try:
        queued = _dt.datetime.fromisoformat(row.get("queued_at", ""))
    except ValueError:
        return 0.0
    return (_dt.datetime.now(_dt.UTC) - queued).total_seconds()


class ZoteroConnectorHandler(PublisherHandler):
    """Fallback PDF handler that delegates to the Zotero Connector.

    The routing layer picks this handler for items where:
      1. No direct-publisher handler claims the DOI, OR
      2. A direct-publisher handler exists but SFX indicates the
         library's full-text route goes through a different platform
         (EBSCOhost, JSTOR, …), OR
      3. A direct-publisher handler failed and the user opted into
         the Connector retry bucket.

    Each item's routing decision includes a pre-selected
    `resolver_target_url` — the full-text target URL from the library's
    SFX response, wrapped in any EZproxy or institutional proxy the
    library requires. The handler opens that URL; the user solves any
    Cloudflare / SSO; the translator fires; Zotero saves.
    """

    name = "connector"
    display_name = "Zotero Connector (fallback)"
    # Catch-all — no DOI prefix matching. The routing layer picks this
    # handler explicitly, not via `resolve_by_doi`.
    doi_prefixes = ()
    # No direct-access domain; this handler trusts the routing layer
    # to have chosen a URL it can reach.
    direct_access_domains = ()
    concurrency = 1
    # Zotero's translators can take a few seconds to parse each page;
    # spacing keeps the connector server from queueing up saves.
    delay_s = 2.0
    attaches_directly = True

    def __init__(self, extension_path: str | Path | None = None) -> None:
        """`extension_path` overrides auto-detection. Defaults to the
        platform-standard Chrome Default-profile extension folder."""
        #: Move the Connector's snapshot and keyword tags into the keeper
        #: too, not only the PDF (`--connector-keep-extras`).
        self.keep_extras = False
        #: Stats of the last merge, read by the caller to finish a
        #: `--replace` swap with the PDF that actually arrived.
        self.last_merge: dict = {}
        #: Per-item wait for the new item to reach the cloud
        #: (`--connector-sync-timeout`).
        self.sync_timeout_s: float = 30.0
        #: Saves whose sync outlasted that wait; None disables queueing.
        self.pending: PendingMerges | None = None
        #: "merge_pending" when the last item was queued rather than
        #: merged or failed; read by the caller for the log status.
        self.last_outcome = ""
        self._explicit_extension_path = extension_path
        self.extension_path = resolve_connector_extension_path(extension_path)
        # Hosts the user has already confirmed in this run — once a
        # host is here, subsequent items on the same host fire
        # automatically (session stays authenticated, so the first
        # item's login / reCAPTCHA carries over).
        self._confirmed_hosts: set[str] = set()
        # Hosts the user asked to skip entirely (e.g. a platform
        # they know they have no access to).
        self._skipped_hosts: set[str] = set()
        # Signing-in proxies (`ezproxy.jyu.fi`) found logged out this run.
        # Their remaining items are not opened: each would land on the
        # same login page. See `_proxy_login_page`.
        self._logged_out_proxies: set[str] = set()

    # ------------------------------------------------------------------
    # PublisherHandler overrides — __init_subclass__ enforces that leaf
    # handlers set `name` and `doi_prefixes`. We set name but leave
    # doi_prefixes empty; silence the validator by marking this class
    # as intermediate-final (the routing layer never DOI-matches it).
    # ------------------------------------------------------------------

    _is_intermediate_base = True  # bypass doi_prefixes check

    # ------------------------------------------------------------------
    # Setup — verify Connector extension exists, Zotero Desktop is up,
    # service worker is ready. Opens the first item's SFX URL so the
    # user can solve any institutional challenge before the first save.
    # ------------------------------------------------------------------

    def merge_saved_item(
        self, zot, keeper: str, new_key: str, *, verify_s: float = 5.0,
    ) -> dict:
        """Merge the Connector's saved item into `keeper`.

        By default only the PDF moves: the keeper's metadata came from
        elsewhere, so the translator's HTML snapshot and its keyword tags
        are not wanted there (`--connector-keep-extras` restores the old
        everything-moves behaviour).
        """
        stats = zot.merge_duplicate_item(
            keeper, new_key,
            union_tags=self.keep_extras,
            child_content_types=None if self.keep_extras else ("application/pdf",),
        )
        # Read the moved PDFs back, twice, a few seconds apart. A merge
        # counts, and --replace may delete the old copy, only if they are
        # still under the keeper: Zotero Desktop overwrote two re-parents
        # that had looked successful. Read where the merge wrote (Desktop
        # when a local key is set), not the cloud: the cloud catches up
        # only when Desktop next syncs.
        for _ in range(2):
            time.sleep(verify_s)
            for key in stats.get("moved_pdf_keys") or []:
                parent = zot.parent_of(key)
                if parent != keeper:
                    raise MergeNotVerified(
                        f"{key} is under {parent}, not {keeper}, after the merge",
                    )
        return stats

    async def setup(self, page: Page, first_doi: str) -> str:
        del page, first_doi   # URL/DOI aren't needed for the intro banner
        if self.extension_path is None:
            problem = connector_extension_problem(self._explicit_extension_path)
            if problem:
                print(f"\nERROR: {problem}", flush=True)
                return "skip"
            print(
                "\nERROR: Zotero Connector extension not found.\n"
                "  Install it from https://www.zotero.org/download/connectors/\n"
                "  in Google Chrome, then re-run the setup wizard so the\n"
                "  plugin can locate the extension folder.",
                flush=True,
            )
            return "skip"

        self._print_connector_banner()

        answer = await asyncio.to_thread(
            _read_user_line,
            "\n>>> Ready to start? "
            "[Y]es = proceed, [n]o = skip Connector fallback: ",
        )
        if answer.strip().lower() in ("n", "no", "s", "skip"):
            return "skip"
        return "proceed"

    def _print_connector_banner(self) -> None:
        print(
            f"\nLoading Zotero Connector extension from:\n"
            f"  {self.extension_path}",
            flush=True,
        )
        print(
            "\n"
            + "*" * 70 + "\n"
            "*  Zotero Connector fallback — automated PDF retrieval\n"
            + "*" * 70 + "\n"
            "*  BEFORE YOU CONTINUE: in Zotero Desktop's left pane,\n"
            "*  click on the TARGET library (the group this pipeline\n"
            "*  is working on). Connector saves go to whichever library\n"
            "*  is selected in Zotero Desktop, NOT automatically to the\n"
            "*  script's configured group. Picking the wrong library\n"
            "*  here lands every save in the wrong place.\n"
            "*\n"
            "*  A second Chromium window has opened with the Zotero\n"
            "*  Connector extension loaded. For each remaining item the\n"
            "*  script will:\n"
            "*\n"
            "*    1. Open the library-routed URL (via SFX) in the browser.\n"
            "*    2. Wait up to 30s for the Connector to detect the page\n"
            "*       and load a translator.\n"
            "*    3. Automatically fire 'Save to Zotero' — do NOT click\n"
            "*       the Connector toolbar button yourself.\n"
            "*    4. Wait up to 30s for Zotero Desktop to create the new\n"
            "*       item, and then up to 20s for it to sync to the cloud.\n"
            "*    5. Merge the new PDF into your existing Zotero item\n"
            "*       (preserves the original item key, BibTeX key, tags,\n"
            "*       and collection memberships).\n"
            "*\n"
            "*  What you may need to do:\n"
            "*    - On the FIRST item from each host (e.g. first jstor,\n"
            "*      first ebscohost, …), solve any institutional login,\n"
            "*      reCAPTCHA, or Cloudflare challenge in the Chromium\n"
            "*      window. The script will then prompt you with:\n"
            "*        [Enter] save this + auto-fire the rest of the host\n"
            "*        s       skip every item on this host this run\n"
            "*      The authenticated session persists, so subsequent\n"
            "*      items from the same host fire without a prompt.\n"
            "*    - Occasionally Zotero pops a 'Select which items'\n"
            "*      picker when the SFX URL lands on a search-results\n"
            "*      page (EBSCO, JSTOR). Click the row whose title /\n"
            "*      DOI matches the one the script just printed, then\n"
            "*      press OK. The save is picked up automatically.\n"
            "*\n"
            "*  What NOT to do:\n"
            "*    - Do NOT close the Chromium window while the script\n"
            "*      is running.\n"
            "*    - Do NOT click the Zotero Connector toolbar button.\n"
            "*    - Do NOT quit Zotero Desktop — the saves go through\n"
            "*      its local connector on localhost:23119.\n"
            "*\n"
            "*  If a translator fails, the item is logged as\n"
            "*  'connector_no_translator' and the script moves on.\n"
            + "*" * 70,
            flush=True,
        )

    # ------------------------------------------------------------------
    # download() is inherited-abstract but never called for
    # attaches_directly=True handlers; the driver calls
    # download_and_attach instead. Provide a stub that raises so a
    # mis-routed call surfaces loudly.
    # ------------------------------------------------------------------

    async def download(self, page, ctx, item, cache_dir, *,
                       counter, total, t_start):
        del page, ctx, item, cache_dir, counter, total, t_start
        raise NotImplementedError(
            "ZoteroConnectorHandler attaches directly; call "
            "download_and_attach(), not download().",
        )

    # ------------------------------------------------------------------
    # Direct-attach path — the driver calls this instead of download().
    # ------------------------------------------------------------------

    async def download_and_attach(
        self,
        page: Page,
        ctx: BrowserContext,
        service_worker: Worker,
        item: dict,
        zot,
        *,
        counter: Counter,
        total: int,
        t_start: float,
    ) -> bool:
        """Save `item` via the Connector and merge the new Zotero item
        into the existing one.

        Returns True on success (merge stats logged by the driver),
        False on any failure. Never raises.
        """
        del ctx, t_start      # unused; service_worker drives the save
        self.last_outcome = ""
        self.last_merge = {}
        doi = item["doi"]
        title = (item.get("title") or "")[:50]
        target_url = item.get("resolver_target_url")

        print(
            f"\n  ┌─ [{counter.done + 1}/{total}] {title}\n"
            f"  │  DOI: {doi}\n"
            f"  │  URL: {target_url or '(missing)'}",
            flush=True,
        )
        if item.get("resolver_target_note"):
            print(f"  │  ({item['resolver_target_note']})", flush=True)

        if not target_url:
            print("  └─ SKIP: no resolver target URL assigned.", flush=True)
            counter.failed += 1
            return False

        from fetchers.library_resolver import effective_host
        item_host = effective_host(target_url)

        proxy = _proxy_base(target_url)
        if proxy and proxy in self._logged_out_proxies:
            self.last_outcome = "login_required"
            print(f"  └─ NOT TRIED: {proxy} is logged out (found earlier "
                  f"this run).", flush=True)
            counter.failed += 1
            return False

        # User-skipped host → drop every item for this host without
        # even opening its page.
        if item_host in self._skipped_hosts:
            print(
                f"  └─ SKIPPED: host {item_host!r} was marked "
                f"skip-all earlier in this run.",
                flush=True,
            )
            counter.failed += 1
            return False

        print("  │  Opening page…", flush=True)
        try:
            await page.goto(target_url, wait_until="domcontentloaded",
                            timeout=30000)
        except Exception as e:
            # Carried out for classification: the Connector is the
            # last rung, so its failures are logged UNAVAILABLE — the
            # cause that licenses a full-text exclusion. A dead
            # network must not reach that verdict.
            self.last_error = str(e)
            print(f"  └─ FAIL: goto error: {str(e)[:80]}", flush=True)
            counter.failed += 1
            return False

        # EBSCO openurl endpoints (and a few other library resolvers)
        # respond with an intermediate list page that JS-redirects to
        # the article detail after a couple of seconds. `goto` returns
        # on first DOMContentLoaded, so without this dwell the
        # translator poll fires during the pre-redirect view and
        # Zotero sees a multi-item page → picker. A 3-second wait
        # catches typical redirect chains at negligible cost.
        await asyncio.sleep(3.0)

        # A logged-out proxy session has to be caught here. Otherwise the
        # login form reaches the translator poll, "no translator" follows,
        # and the item is logged ACCESS_BLOCKED, telling the review that
        # the library cannot reach an article it can. EZproxy keeps its
        # session in a browser-session cookie, so a login does not survive
        # a relaunch of the Connector profile. Twice on 2026-09-23 a
        # restarted run wrote such rows (6, then 2), which had to be
        # removed by hand.
        if proxy and await _proxy_login_page(page, target_url):
            if sys.stdin.isatty():
                await asyncio.to_thread(
                    _read_user_line,
                    f"  │  {proxy} is asking you to sign in. Sign in in the\n"
                    f"  │  Chromium window, wait for the article page, then\n"
                    f"  │  press [Enter]: ",
                )
            else:
                # A signed-in redirect can still be on the proxy host after
                # the 3 s dwell. Mistaking it costs every remaining item
                # behind this proxy, so give it longer before concluding.
                for _ in range(10):
                    await asyncio.sleep(1.0)
                    if not await _proxy_login_page(page, target_url):
                        break
            if await _proxy_login_page(page, target_url):
                self._logged_out_proxies.add(proxy)
                self.last_outcome = "login_required"
                print(
                    f"  └─ LOGIN REQUIRED: landed on {proxy}'s sign-in page.\n"
                    f"         Not an access verdict, so nothing goes in the\n"
                    f"         failure log. The remaining {proxy} items are\n"
                    f"         skipped this run. Sign in in the Connector\n"
                    f"         window before \"Ready to start?\" and re-run.",
                    flush=True,
                )
                counter.failed += 1
                return False

        # One-prompt-per-host confirmation. The translator otherwise
        # fires too eagerly on pages that briefly render reCAPTCHA
        # (JSTOR) or a multi-item search list (EBSCO) before
        # redirecting to the article page. After the first item on
        # a host is confirmed, the authenticated session persists
        # across the rest of that host's items — no more prompts.
        #
        # Skipped on non-TTY runs (CI / piped stdin).
        if sys.stdin.isatty() and item_host not in self._confirmed_hosts:
            answer = await asyncio.to_thread(
                _read_user_line,
                f"  │  First item on host {item_host!r}. In the Chromium\n"
                "  │  window, solve any reCAPTCHA / login and wait for\n"
                "  │  the article page to load.  Once the article is\n"
                "  │  visible:\n"
                "  │    [Enter] save this item, then auto-fire every\n"
                "  │            remaining item on the same host\n"
                "  │    s       skip every item on this host this run\n"
                "  │  > ",
            )
            choice = answer.strip().lower()
            if choice in ("s", "skip"):
                self._skipped_hosts.add(item_host)
                print(
                    f"  └─ SKIPPED: host {item_host!r} added to run-scoped\n"
                    f"         skip list (applies to this + every remaining\n"
                    f"         item on the same host).",
                    flush=True,
                )
                counter.failed += 1
                return False
            self._confirmed_hosts.add(item_host)

        # Bail out of a search-result page before the translator sees
        # it. Firing the save here raises Zotero's item picker, which
        # blocks the run until a human clicks — and with the poll window
        # now at 240s, each such item would stall four minutes before
        # failing anyway. Try the exact-title route first; skip if that
        # cannot identify the article.
        if _is_result_list(page.url):
            print("  │  Landed on a search-result list — the resolver could "
                  "not identify\n  │  the article. Trying an exact title "
                  "match…", flush=True)
            if await _click_matching_result(page, item.get("title", "")):
                print(f"  │  Matched by title → {page.url[:80]}", flush=True)
            else:
                print(
                    "  └─ SKIPPED: search-result page with no exact title\n"
                    "         match. Not firing the translator: it would\n"
                    "         raise Zotero's item picker and block, and any\n"
                    "         non-exact pick would attach a different\n"
                    "         article's PDF. Logged as an ILL candidate.",
                    flush=True,
                )
                counter.failed += 1
                return False

        # Wait for the Connector to parse the page and load a
        # translator. When the user's institutional SSO or Cloudflare
        # challenge is in the way, this poll will time out.
        print(
            "  │  Waiting for Connector to load a translator "
            "(up to 30s)…",
            flush=True,
        )
        translator_count = await _wait_for_translators(
            service_worker, timeout_s=30,
        )
        if translator_count == 0:
            print(
                "  └─ FAIL: no translator detected. This usually means\n"
                "         the page needs authentication (EZproxy / SSO)\n"
                "         or the Connector doesn't support this platform.",
                flush=True,
            )
            counter.failed += 1
            return False
        print(f"  │  Translator ready ({translator_count} available). "
              f"Firing save…", flush=True)

        # Fire the save. This returns fast; the actual write to Zotero
        # Desktop happens async through the extension's internal queue.
        #
        # Robust tab resolution: `chrome.tabs.query({active: true,
        # currentWindow: true})` can return the wrong tab from a
        # background service worker (Connector popup, unfocused
        # window, …). Instead, pass Playwright's actual page URL in
        # and find the tab whose URL matches — `tabs[0]` in the naïve
        # query sometimes points at a Connector popup on JSTOR.
        page_url = page.url
        try:
            save_result = await service_worker.evaluate(
                """
                async (pageUrl) => {
                    let targetHost = '';
                    try { targetHost = new URL(pageUrl).host; } catch (_) {}
                    const allTabs = await chrome.tabs.query({});
                    // Prefer an exact URL match, then same-host match.
                    let t = allTabs.find(x => x.url === pageUrl);
                    if (!t) {
                        t = allTabs.find(x => {
                            try { return new URL(x.url).host === targetHost; }
                            catch (_) { return false; }
                        });
                    }
                    if (!t) {
                        return {
                            ok: false, reason: 'no-matching-tab',
                            pageUrl, targetHost,
                            tabs: allTabs.map(x => x.url),
                        };
                    }
                    if (typeof Zotero === 'undefined'
                        || !Zotero.Connector_Browser) {
                        return {ok: false, reason: 'no-zotero-object'};
                    }
                    try {
                        Zotero.Connector_Browser.saveWithTranslator(
                            t, 0, {fallbackOnFailure: true},
                        );
                        return {ok: true, tabId: t.id, tabUrl: t.url};
                    } catch (e) {
                        return {ok: false, reason: String(e)};
                    }
                }
                """,
                page_url,
            )
        except Exception as e:
            print(f"  └─ FAIL: service-worker evaluate error: "
                  f"{str(e)[:80]}", flush=True)
            counter.failed += 1
            return False
        if not save_result or not save_result.get("ok"):
            reason = (save_result or {}).get("reason", "unknown")
            extra = ""
            if reason == "no-matching-tab":
                tabs_list = (save_result or {}).get("tabs", [])
                extra = (
                    f"\n         page URL: "
                    f"{(save_result or {}).get('pageUrl', '')}\n"
                    f"         tabs in Chromium: {tabs_list}"
                )
            print(f"  └─ FAIL: save call rejected: {reason}{extra}",
                  flush=True)
            counter.failed += 1
            return False
        # Log which tab we fired against — helps diagnose any future
        # "no item appeared" case.
        print(
            f"  │  Save fired on tab {save_result.get('tabId')} "
            f"({save_result.get('tabUrl', '')[:80]}).",
            flush=True,
        )

        # Poll LOCAL Zotero for the new item — Zotero Desktop writes
        # here first, then syncs to cloud. Timeout is 120s because an
        # OpenURL that redirects to a list page (common on EBSCO)
        # triggers Zotero's item picker, and the user needs time to
        # click through it.
        print(f"  │  Waiting for Zotero Desktop to save item "
              f"(up to {int(_SAVE_POLL_TIMEOUT_S)}s)…", flush=True)
        new_key = await asyncio.to_thread(
            functools.partial(
                _poll_for_new_item, zot, doi, item["item_key"],
                _SAVE_POLL_TIMEOUT_S, title=item.get("title", ""),
                exclude=self.pending.new_keys() if self.pending else frozenset(),
            ),
        )
        if new_key is None:
            self.last_outcome = "saved_nothing"
            # What to blame depends on something we already know. Once
            # anything has saved in this run, the library selection is
            # demonstrably correct, and leading with "check the left
            # pane" sends the user to inspect a setting that is fine.
            # Reported live against a run where the actual reason was
            # simply no access to those articles — off-VPN, paywalled,
            # or unentitled, which the translator cannot distinguish
            # because all three hand it a page with no PDF on it.
            saved = counter.ok + counter.queued
            if saved:
                print(
                    f"  └─ FAIL: the translator saved nothing.\n"
                    f"         {saved} item"
                    f"{'' if saved == 1 else 's'} already saved this "
                    f"run, so the library\n"
                    f"         selection is correct — it is not that.\n"
                    f"         Most likely you cannot reach this article:\n"
                    f"           - no subscription, or off the entitled\n"
                    f"             network (VPN/proxy down);\n"
                    f"           - the page offered metadata only.\n"
                    f"         Logged as ACCESS_BLOCKED, not as \"no full\n"
                    f"         text exists\" — it stays in the review as an\n"
                    f"         interlibrary-loan candidate.",
                    flush=True,
                )
            else:
                print(
                    "  └─ FAIL: no new item appeared in the target group.\n"
                    "         Nothing has saved yet this run, so check the\n"
                    "         first cause before the others:\n"
                    "           - Zotero Desktop has a DIFFERENT library\n"
                    "             selected in the left pane (the most\n"
                    "             common cause — check now).\n"
                    "           - You cannot reach this article: no\n"
                    "             subscription, or off the entitled network.\n"
                    "           - The translator saved the item under a\n"
                    "             different DOI, or saved metadata only.\n"
                    "           - The save was rejected silently; check\n"
                    "             Zotero Desktop's Debug Output Log.",
                    flush=True,
                )
            counter.failed += 1
            return False
        wait_s = int(self.sync_timeout_s)
        local = _merges_locally(zot)
        if local:
            print(f"  │  New item saved locally ({new_key}). Merging in "
                  f"Zotero Desktop; no cloud sync needed.", flush=True)
        else:
            print(f"  │  New item saved locally ({new_key}). "
                  f"Waiting for cloud sync (up to {wait_s}s)…", flush=True)

        # Without a local key the merge runs on the cloud API, so the new
        # item must have synced first. (With one, `_merge_surface` makes
        # this a local read that succeeds at once.) Usually that takes seconds; under concurrent
        # writers Zotero Desktop's upload lagged ~7 minutes, and a fixed
        # 30 s wait failed every item of a run. So a slow sync no longer
        # fails the item: the pair is queued and merged once it syncs,
        # at the end of this pass or at the start of the next one.
        synced = await asyncio.to_thread(
            _wait_for_cloud_sync, zot, new_key, self.sync_timeout_s,
        )
        if not synced:
            if self.pending is not None:
                self.pending.add(
                    keeper=item["item_key"], new_key=new_key, doi=doi,
                )
                self.last_outcome = "merge_pending"
                counter.queued += 1
                print(
                    f"  └─ QUEUED: {new_key} is saved in Zotero Desktop but not\n"
                    f"         on the cloud yet ({wait_s}s). Queued; it is merged\n"
                    f"         into {item['item_key']} as soon as it syncs — later in\n"
                    f"         this run, or at the start of the next Connector pass.",
                    flush=True,
                )
                return False
            print(
                f"  └─ FAIL: new item {new_key} is in Zotero Desktop but\n"
                f"         hasn't synced to the cloud in {wait_s}s. Merge aborted.",
                flush=True,
            )
            counter.failed += 1
            return False

        # Wait for the attachment record to appear as a child. We
        # don't require md5 — the merge just PATCHes `parentItem`
        # on the attachment, which works at any sync stage. The
        # stub-vs-real race on next run is handled by `pdf_map()`
        # skipping recently-added attachments.
        print(f"  │  {'Parent found' if local else 'Parent synced'}. "
              f"Waiting for the PDF attachment record "
              f"(up to {wait_s}s)…", flush=True)
        has_pdf = await asyncio.to_thread(
            _wait_for_child_attachment, zot, new_key, self.sync_timeout_s,
        )
        if not has_pdf and self.pending is not None:
            # Queue rather than merge. The PDF record can lag its parent
            # under upload load, and merging then moved nothing and
            # trashed the only item holding the PDF. The queue merges once
            # a PDF child is visible, and gives up on a save that never
            # grows one (a genuinely metadata-only page).
            self.pending.add(keeper=item["item_key"], new_key=new_key, doi=doi)
            self.last_outcome = "merge_pending"
            counter.queued += 1
            print(
                f"  └─ QUEUED: {new_key} is saved but its PDF is not yet\n"
                f"         ({wait_s}s). Queued; merged into {item['item_key']}\n"
                f"         once the PDF syncs.",
                flush=True,
            )
            return False

        # Merge the new item into the existing one.
        print(f"  │  Merging into keeper {item['item_key']}…", flush=True)
        try:
            # By default only the PDF moves: the keeper's metadata came
            # from elsewhere, so the translator's HTML snapshot and its
            # keyword tags are not wanted there (`--connector-keep-extras`
            # restores the old everything-moves behaviour).
            stats = await asyncio.to_thread(
                self.merge_saved_item, zot, item["item_key"], new_key,
            )
            self.last_merge = stats
        except Exception as e:
            if self.pending is not None:
                # The temporary item is still live and holds the PDF, so
                # a failed merge (a 412 that outlasted the retries, a
                # transient API error) is a retry, not a verdict.
                self.pending.add(keeper=item["item_key"], new_key=new_key, doi=doi)
                self.last_outcome = "merge_pending"
                counter.queued += 1
                print(f"  └─ QUEUED: merge errored ({str(e)[:80]}); queued to "
                      f"retry into {item['item_key']}.", flush=True)
                return False
            print(f"  └─ FAIL: merge errored: {str(e)[:100]}", flush=True)
            counter.failed += 1
            return False

        moved = stats.get("moved", 0)
        dup = stats.get("skipped_dupe_attachments", 0)
        tags = stats.get("tags_added", 0)
        colls = stats.get("collections_added", 0)

        if moved == 0 and dup == 0 and stats.get("kept_unmoved_pdf") and self.pending:
            # A PDF appeared after the merge listed children; the merge
            # left the item in place rather than trash it. Try again later.
            self.pending.add(keeper=item["item_key"], new_key=new_key, doi=doi)
            self.last_outcome = "merge_pending"
            counter.queued += 1
            print(f"  └─ QUEUED: {new_key}'s PDF arrived mid-merge; queued to "
                  f"merge into {item['item_key']}.", flush=True)
            return False

        if moved == 0 and dup == 0:
            # Translator saved metadata but no PDF attachment. The
            # merge technically succeeded, but there's nothing to
            # attach to the keeper.
            print(
                "  └─ PARTIAL: Connector saved but no PDF found.\n"
                "         Translator produced metadata only for this\n"
                "         page. Try a different SFX target or save\n"
                "         the PDF manually.",
                flush=True,
            )
            counter.failed += 1
            return False

        counter.ok += 1
        print(
            f"  └─ ATTACHED: {moved} child"
            f"{'ren' if moved != 1 else ''} moved"
            f"{f', {dup} dupe-skipped' if dup else ''}"
            f"{f', +{tags} tags' if tags else ''}"
            f"{f', +{colls} collections' if colls else ''}.",
            flush=True,
        )
        return True


# ---------------------------------------------------------------------------
# Service worker / Zotero helpers
# ---------------------------------------------------------------------------


async def wait_for_service_worker(
    ctx: BrowserContext, *, timeout_s: float = 15,
) -> Worker | None:
    """Poll `ctx.service_workers` until at least one worker appears.

    Extensions boot lazily — the service worker may not exist until
    the first page load kicks off the extension lifecycle. The POC
    polled on a 1-second interval for 15 seconds; same here.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if ctx.service_workers:
            return ctx.service_workers[0]
        await asyncio.sleep(1.0)
    return None


async def _wait_for_translators(
    service_worker: Worker, *, timeout_s: float = 30,
) -> int:
    """Poll the active tab's Connector translator list until non-empty.

    Returns the final translator count (0 means timed out). Polling
    is done inside the service worker because the Connector stores
    per-tab state there.
    """
    deadline = time.monotonic() + timeout_s
    last = 0
    while time.monotonic() < deadline:
        try:
            last = await service_worker.evaluate(
                """
                async () => {
                    const tabs = await chrome.tabs.query({
                        active: true, currentWindow: true,
                    });
                    const t = tabs[0];
                    if (!t || typeof Zotero === 'undefined'
                        || !Zotero.Connector_Browser) return 0;
                    const info = Zotero.Connector_Browser.getTabInfo(t.id);
                    return info && info.translators ?
                        info.translators.length : 0;
                }
                """,
            )
        except Exception:
            last = 0
        if last:
            return last
        await asyncio.sleep(0.5)
    return last


def ping_zotero_desktop(session, timeout_s: float = 3.0) -> bool:
    """Return True if Zotero Desktop's connector server is reachable.

    Called before running a batch so a clear error appears before we
    open a browser and try 50 saves that silently drop.
    """
    try:
        resp = session.get(_CONNECTOR_PING_URL, timeout=timeout_s)
    except Exception:
        return False
    return resp.status_code == 200


def _normalise_title(text: str) -> str:
    """Lower-case, alphanumeric-only form for comparing two titles.

    Publishers and Zotero translators disagree about case, punctuation
    and diacritics for the same article ("RUSSIAN MINERS BOW TO THE
    ANGEL OF HISTORY" vs "Russian Miners Bow to the Angel of History"),
    so an exact string compare is useless here.
    """
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


#: URL signatures of *search-result* pages, per host. Reaching one of
#: these means the resolver's OpenURL failed to identify the article and
#: the platform fell back to a query.
#:
#: Live case that motivated this: a BMJ item routed to JSTOR produced
#: `doBasicSearch?Query=sn:09598138 AND surname:"Iacobucci" AND year:2017`
#: — **172 results**, matched on ISSN, surname and year only, with
#: JSTOR's own banner admitting "your inbound link did not have an exact
#: match in our database". Firing the translator at that page makes
#: Zotero raise its "Select which items" picker and block until a human
#: chooses, which defeats an unattended run; and choosing from 172
#: loosely-matched hits risks attaching a *different article's* PDF,
#: which is worse than attaching nothing.
#:
#: Deliberately keyed on hosts we have evidence for rather than a
#: generic "looks like a list" heuristic — EBSCO's result pages resolve
#: to the article by themselves and must not be caught here.
_RESULT_LIST_URL_MARKERS: dict[str, tuple[str, ...]] = {
    "jstor.org": ("/action/doBasicSearch", "/action/doAdvancedSearch",
                  "/action/showBasicSearch"),
}


def _is_result_list(url: str) -> bool:
    low = (url or "").lower()
    for host, markers in _RESULT_LIST_URL_MARKERS.items():
        if host in low and any(m.lower() in low for m in markers):
            return True
    return False


async def _click_matching_result(page, title: str) -> bool:
    """On a result list, navigate to the entry whose title matches.

    Only an exact normalised-title match counts. A "closest match" would
    be precisely the wrong thing here: the reason we are on this page is
    that the resolver could not identify the article, so a fuzzy second
    guess would attach some other paper's PDF with no signal that
    anything went wrong.
    """
    want = _normalise_title(title)
    if not want:
        return False
    try:
        href = await page.evaluate(
            """
            (want) => {
                const norm = s => (s || "").toLowerCase()
                    .replace(/[^a-z0-9]+/g, " ").trim();
                for (const a of document.querySelectorAll("a")) {
                    if (norm(a.textContent) === want) return a.href;
                }
                return "";
            }
            """,
            want,
        )
    except Exception:
        return False
    if not href:
        return False
    try:
        await page.goto(href, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
    except Exception:
        return False
    return True


def _poll_for_new_item(
    zot, doi: str, keeper_key: str, timeout_s: float,
    *,
    hint_every_s: float = 15.0,
    title: str = "",
    exclude: frozenset[str] | set[str] = frozenset(),
) -> str | None:
    """Return the item_key of the item the Connector just created.

    Matches on DOI **or** title, and that is not redundant: Zotero's
    translators routinely save a record with no DOI field at all —
    three of five orphans left by one live run had none — so a
    DOI-only match declared failure for items that had saved
    perfectly, abandoning a duplicate that held the PDF.

    The recency window applies to the **title** path only. A DOI is
    specific enough to identify the article on its own, but a title is
    not: without the window a title match would happily return some
    pre-existing copy elsewhere in the library and the caller would
    merge the wrong pair. `keeper_key` is excluded on both paths.

    Polls LOCAL Zotero — Zotero Desktop writes new items here
    immediately after the Connector saves. Cloud sync happens
    separately; callers that need the item via the cloud API must
    then call `_wait_for_cloud_sync`.

    When the SFX URL redirects to a list page (EBSCO / JSTOR search
    results are the common cases), Zotero pops a "Select which items"
    picker that blocks on user input, so the timeout must leave room
    for a human to click through it. While waiting, every
    `hint_every_s` seconds a reminder is printed so a quiet terminal
    doesn't look hung.
    """
    needle = doi.strip().lower()
    want_title = _normalise_title(title)
    # A few seconds of slack: the local clock and Zotero's dateAdded
    # (UTC, second resolution) need not agree exactly, and losing the
    # real save to a one-second skew would reintroduce the bug.
    cutoff = (
        _dt.datetime.now(_dt.UTC) - _dt.timedelta(seconds=10)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    start = time.monotonic()
    deadline = start + timeout_s
    next_hint_at = start + hint_every_s
    while time.monotonic() < deadline:
        try:
            # Newest items only. Listing every journal article took 95 s
            # per poll on a 25,703-article library, so a save Zotero had
            # finished in seconds was reported minutes later. It also
            # stops an older copy with the same DOI from being taken for
            # the item that was just saved.
            items = zot.recent_items()
        except Exception:
            items = []
        for it in items:
            # `exclude`: earlier saves still queued for merging. Same DOI,
            # among the newest items, and not the save just made.
            if it.get("key") == keeper_key or it.get("key") in exclude:
                continue
            data = it.get("data", {})
            if data.get("itemType") in ("attachment", "note", "annotation"):
                continue
            it_doi = (data.get("DOI") or "").strip().lower()
            if needle and it_doi == needle:
                return it["key"]
            if (
                want_title
                and (data.get("dateAdded") or "") >= cutoff
                and _normalise_title(data.get("title")) == want_title
            ):
                return it["key"]
        now = time.monotonic()
        if now >= next_hint_at:
            elapsed = int(now - start)
            remaining = int(deadline - now)
            print(
                f"  │    …still waiting ({elapsed}s elapsed, "
                f"~{remaining}s remaining). "
                f"If Zotero shows a 'Select which items' picker,\n"
                f"  │    choose the matching article and it will save.",
                flush=True,
            )
            next_hint_at = now + hint_every_s
        time.sleep(1.0)
    return None


#: Host labels and path fragments of sign-in pages. A proxy hands a
#: logged-out reader to its own login form or on to the institution's
#: identity provider (JYU: ezproxy.jyu.fi -> login.jyu.fi, SAML).
_LOGIN_HOST_LABELS = frozenset({"login", "idp", "sso", "shibboleth", "auth"})
_LOGIN_PATH_MARKERS = ("/login", "/idp/", "/saml", "/sso", "/shibboleth")


def _proxy_base(target_url: str) -> str:
    """`ezproxy.jyu.fi` for a route through a signing-in proxy, else "".

    Covers both EZproxy shapes: the wrapper `ezproxy.jyu.fi/login?url=…`
    and the rewritten host `www-sciencedirect-com.ezproxy.jyu.fi`.
    """
    from fetchers.resolvers.base import (
        INTERACTIVE_PROXY_LABELS,
        needs_interactive_login,
    )

    if not needs_interactive_login(target_url):
        return ""
    labels = (urlparse(target_url).hostname or "").lower().split(".")
    for i, label in enumerate(labels):
        if label in INTERACTIVE_PROXY_LABELS:
            return ".".join(labels[i:])
    return ""


def is_proxy_login_url(page_url: str, target_url: str) -> bool:
    """Whether `page_url`, reached from a proxied `target_url`, is a
    sign-in page rather than the proxied article.

    Signed in, EZproxy rewrites the publisher onto a subdomain of itself
    (`www-sciencedirect-com.ezproxy.jyu.fi`). The bare proxy host is its
    own login or menu page. Anywhere else is off the proxy, and that
    counts only when it looks like an identity provider. Only proxied
    routes are judged at all, because most publisher pages carry a login
    link of their own.
    """
    proxy = _proxy_base(target_url)
    if not proxy or not page_url:
        return False
    parsed = urlparse(page_url)
    host = (parsed.hostname or "").lower()
    if host == proxy:
        return True
    if host.endswith("." + proxy):
        return False
    path = (parsed.path or "").lower()
    return bool(
        _LOGIN_HOST_LABELS & set(host.split("."))
        or any(m in path for m in _LOGIN_PATH_MARKERS)
    )


async def _proxy_login_page(page, target_url: str) -> bool:
    """`is_proxy_login_url` on the page as it stands.

    URL only, no password-field probe. Publisher pages routinely carry a
    hidden login form, and a false positive here is not cheap, because
    it skips every remaining item behind that proxy.
    """
    try:
        return is_proxy_login_url(page.url, target_url)
    except Exception:  # noqa: BLE001
        return False


def _merges_locally(zot) -> bool:
    """True when this client merges in Zotero Desktop (local writes on).

    `is True`, not truthiness: test doubles are MagicMocks, whose every
    attribute is truthy, and they model the cloud path.
    """
    return getattr(zot, "local_writes_enabled", False) is True


def _merge_surface(zot):
    """The pyzotero client the merge reads and writes: Desktop's local
    API when local writes are on, the cloud otherwise.

    Every wait below polls this surface, not the cloud. With the merge
    (re-parent and trash, e849081) running in Desktop, the cloud has no
    part in it. Observed on 2026-09-23 on six live Connector saves, the
    cloud lagged the local save by 167-208 s under the run's own upload
    load. So the 30 s cloud wait queued every single item, while the PDF
    child was on the local API within 6-14 s with md5 and mtime already
    set. That wait was most of the ~1.4 min per item.

    Desktop's later upload does not disturb a local re-parent. The
    2026-09-23 lost-PDF race was a *cloud* re-parent that Desktop's
    still-pending push then overwrote with its own, older local state.
    A change made in Desktop is that local state. Desktop does bump the
    attachment's local version once the file upload finishes, which is
    why the merge's read-back (`merge_saved_item`) checks the parent and
    not the version.
    """
    return zot.local if _merges_locally(zot) else zot.cloud


def _wait_for_cloud_sync(zot, item_key: str, timeout_s: float) -> bool:
    """Block until `item_key` is visible on the merge's surface — the
    Zotero cloud API unless local writes are on (`_merge_surface`).

    Zotero Desktop saves items locally first and replicates to the
    cloud on its auto-sync cadence, which under upload load can take
    minutes. A cloud merge started before sync completes gets a spurious
    404. Poll every second until the item is visible, or give up after
    `timeout_s`.
    """
    surface = _merge_surface(zot)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if surface.item(item_key):
                return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def _wait_for_child_attachment(
    zot, item_key: str, timeout_s: float,
) -> bool:
    """Block until `item_key` has a settled PDF child on the merge's
    surface (`_merge_surface`).

    This is enough to proceed with the merge: the merge PATCHes
    `parentItem` on the attachment record, which works regardless
    of whether file bytes have uploaded or md5 has populated. We
    don't wait for md5 here — for large PDFs that upload can take
    several minutes and the script would hang pointlessly. The
    stub-vs-real-PDF race that md5 used to guard against is solved
    at the `pdf_map()` side by skipping recently-added attachments
    (see `ZoteroClient.pdf_map`).

    Returns True once an attachment child is visible; False on
    timeout. False is ambiguous (translator may be metadata-only,
    or sync is genuinely slow) — callers should proceed with the
    merge and let it log PARTIAL when no child exists.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            children = _merge_surface(zot).children(item_key) or []
        except Exception:
            children = []
        if _has_pdf_child(children) and _pdf_child_settled(zot, item_key):
            return True
        time.sleep(1.0)
    return False


class MergeNotVerified(Exception):
    """The moved PDF was not under the keeper when read back."""


def _pdf_child_settled(zot, item_key: str, *, stable_s: float = 3.0) -> bool:
    """A PDF child of `item_key` whose upload has finished: md5 set, and
    its version the same across two reads `stable_s` apart.

    Merging earlier is how two keepers lost their PDF on 2026-09-23. The
    attachment was visible with no md5 while Zotero Desktop was still
    uploading it; our re-parent landed, and Desktop's later push of that
    attachment put the old parent back — under the temporary item we had
    just trashed — after --replace had deleted the keeper's old copy.

    On the local surface md5 means something weaker: Desktop sets it the
    moment the file is written, before any upload. That is enough there,
    since a re-parent made in Desktop is not exposed to the push race
    (see `_merge_surface`). The version check still catches a record
    Desktop is actively rewriting.
    """
    surface = _merge_surface(zot)
    try:
        pdfs = [
            c for c in surface.children(item_key) or []
            if _has_pdf_child([c]) and (c.get("data", {}) or {}).get("md5")
        ]
        if not pdfs:
            return False
        time.sleep(stable_s)
        for c in pdfs:
            again = surface.item(c.get("key"))
            if (again.get("version") != c.get("version")
                    or not (again.get("data", {}) or {}).get("md5")):
                return False
        return True
    except Exception:  # noqa: BLE001 — unknown is not settled
        return False


def _has_pdf_child(children) -> bool:
    """A PDF attachment among `children`. Any attachment used to count,
    but the translator's HTML snapshot can reach the cloud before the
    PDF, and a merge started then moves nothing."""
    return any(
        (c.get("data", {}) or {}).get("itemType") == "attachment"
        and (c.get("data", {}) or {}).get("contentType") == "application/pdf"
        for c in children or []
    )


__all__ = [
    "ZoteroConnectorHandler",
    "ping_zotero_desktop",
    "MergeNotVerified",
    "PendingMerges",
    "connector_extension_problem",
    "settle_pending_merges",
    "resolve_connector_extension_path",
    "wait_for_service_worker",
]
