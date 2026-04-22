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
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

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

    Returns None when nothing is found; callers surface a user-facing
    install hint.
    """
    def _latest_version_subdir(base: Path) -> Path | None:
        if not base.exists():
            return None
        # An extension base folder contains one subdir per installed
        # version (e.g. "5.0.130_0"). If the caller passed a path that
        # already looks like a version folder (contains manifest.json),
        # return it directly.
        if (base / "manifest.json").exists():
            return base
        subs = [d for d in base.iterdir() if d.is_dir()]
        if not subs:
            return None
        subs.sort(key=lambda p: p.name)
        return subs[-1]

    if explicit:
        path = Path(explicit).expanduser()
        return _latest_version_subdir(path)

    for candidate in _default_extension_search_paths():
        result = _latest_version_subdir(candidate)
        if result is not None:
            return result
    return None


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
    `sfx_target_url` — the full-text target URL from the library's
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
        self.extension_path = resolve_connector_extension_path(extension_path)
        # Hosts the user has already confirmed in this run — once a
        # host is here, subsequent items on the same host fire
        # automatically (session stays authenticated, so the first
        # item's login / reCAPTCHA carries over).
        self._confirmed_hosts: set[str] = set()
        # Hosts the user asked to skip entirely (e.g. a platform
        # they know they have no access to).
        self._skipped_hosts: set[str] = set()

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

    async def setup(self, page: Page, first_doi: str) -> str:
        del page, first_doi   # URL/DOI aren't needed for the intro banner
        if self.extension_path is None:
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
        doi = item["doi"]
        title = (item.get("title") or "")[:50]
        target_url = item.get("sfx_target_url")

        print(
            f"\n  ┌─ [{counter.done + 1}/{total}] {title}\n"
            f"  │  DOI: {doi}\n"
            f"  │  URL: {target_url or '(missing)'}",
            flush=True,
        )

        if not target_url:
            print("  └─ SKIP: no SFX target URL assigned.", flush=True)
            counter.failed += 1
            return False

        from fetchers.library_resolver import _effective_host
        item_host = _effective_host(target_url)

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
            print(f"  └─ FAIL: goto error: {str(e)[:80]}", flush=True)
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
        print("  │  Waiting for Zotero Desktop to save item "
              "(up to 120s)…", flush=True)
        new_key = await asyncio.to_thread(
            _poll_for_new_item, zot, doi, item["item_key"], 120,
        )
        if new_key is None:
            print(
                "  └─ FAIL: no new item appeared in the target group.\n"
                "         Common causes:\n"
                "           - Zotero Desktop has a DIFFERENT library\n"
                "             selected in the left pane (the most\n"
                "             common cause — check now).\n"
                "           - The translator saved the item under a\n"
                "             different DOI, or saved metadata only.\n"
                "           - The save was rejected silently; check\n"
                "             Zotero Desktop's Debug Output Log.",
                flush=True,
            )
            counter.failed += 1
            return False
        print(f"  │  New item saved locally ({new_key}). "
              f"Waiting for cloud sync (up to 30s)…", flush=True)

        # Wait for cloud sync before merging — the merge uses the
        # cloud API and will 404 if the item hasn't replicated yet.
        # 30s covers typical Zotero Desktop sync cadence; a handful
        # of items in the AI Entrepreneurship library sat at ~25s.
        synced = await asyncio.to_thread(
            _wait_for_cloud_sync, zot, new_key, 30,
        )
        if not synced:
            print(
                f"  └─ FAIL: new item {new_key} is in Zotero Desktop but\n"
                "         hasn't synced to the cloud in 30s. Merge\n"
                "         aborted. Re-run after sync completes; the\n"
                "         item will be auto-merged next time.",
                flush=True,
            )
            counter.failed += 1
            return False

        # Wait for the PDF attachment — with md5 populated — to
        # appear as a child. Three sync stages: parent metadata,
        # attachment shell, file bytes (md5 lands at stage 3). We
        # need stage 3 before merging — merging at stage 2 locks in
        # a record whose md5 is empty, which the next run's
        # `pdf_map` classifies as a 'stub' and deletes (thrashing
        # until md5 eventually catches up).
        print("  │  Parent synced. Waiting for PDF attachment with "
              "md5 (up to 120s)…", flush=True)
        has_child = await asyncio.to_thread(
            _wait_for_child_attachment, zot, new_key, 120,
        )
        if not has_child:
            print(
                "  │  No attachment with md5 after 120s — translator\n"
                "  │  may be metadata-only, the file upload may have\n"
                "  │  stalled, or Zotero Desktop may be slow to sync.\n"
                "  │  Proceeding with merge so the duplicate is trashed;\n"
                "  │  merge will report PARTIAL if no PDF child exists\n"
                "  │  to move.",
                flush=True,
            )

        # Merge the new item into the existing one.
        print(f"  │  Merging into keeper {item['item_key']}…", flush=True)
        try:
            stats = await asyncio.to_thread(
                zot.merge_duplicate_item, item["item_key"], new_key,
            )
        except Exception as e:
            print(f"  └─ FAIL: merge errored: {str(e)[:100]}", flush=True)
            counter.failed += 1
            return False

        moved = stats.get("moved", 0)
        dup = stats.get("skipped_dupe_attachments", 0)
        tags = stats.get("tags_added", 0)
        colls = stats.get("collections_added", 0)

        if moved == 0 and dup == 0:
            # Translator saved metadata but no PDF attachment. The
            # merge technically succeeded, but there's nothing to
            # attach to the keeper.
            print(
                f"  └─ PARTIAL: Connector saved but no PDF found.\n"
                f"         Translator produced metadata only for this\n"
                f"         page. Try a different SFX target or save\n"
                f"         the PDF manually.",
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
) -> "Worker | None":
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


def _poll_for_new_item(
    zot, doi: str, keeper_key: str, timeout_s: float,
    *,
    hint_every_s: float = 15.0,
) -> str | None:
    """Return the item_key of a newly-created Zotero item whose DOI
    matches `doi` and whose key is NOT `keeper_key`.

    Polls LOCAL Zotero — Zotero Desktop writes new items here
    immediately after the Connector saves. Cloud sync happens
    separately; callers that need the item via the cloud API must
    then call `_wait_for_cloud_sync`.

    When the SFX URL redirects to a list page (EBSCO / JSTOR search
    results are the common cases), Zotero pops a "Select which items"
    picker that blocks on user input. The poll's timeout must be
    generous enough for the user to click through that UI; 120s is
    the caller's default. While waiting, `hint_every_s` seconds pass
    a reminder is printed so a quiet terminal doesn't look hung.
    """
    needle = doi.strip().lower()
    start = time.monotonic()
    deadline = start + timeout_s
    next_hint_at = start + hint_every_s
    while time.monotonic() < deadline:
        try:
            items = zot.journal_articles()
        except Exception:
            items = []
        for it in items:
            if it.get("key") == keeper_key:
                continue
            it_doi = (it.get("data", {}).get("DOI") or "").strip().lower()
            if it_doi == needle:
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


def _wait_for_cloud_sync(zot, item_key: str, timeout_s: float) -> bool:
    """Block until `item_key` is visible via the Zotero cloud API.

    Zotero Desktop saves items locally first and replicates to the
    cloud on its auto-sync cadence (typically 1–10s). Our merge
    routine uses the cloud API; calling it before sync completes
    produces a spurious 404. Poll `zot.cloud.item(key)` every second
    until it returns successfully, or give up after `timeout_s`.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if zot.cloud.item(item_key):
                return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def _wait_for_child_attachment(
    zot, item_key: str, timeout_s: float,
) -> bool:
    """Block until `item_key` has at least one child attachment that
    carries a non-empty `md5`, visible via the Zotero cloud API.

    When Zotero Desktop saves via the Connector, several stages
    sync separately:
      1. Parent metadata record.
      2. Attachment shell (contentType + filename + url, but no md5
         yet because the file bytes haven't been uploaded).
      3. Attachment bytes uploaded; md5 populated on the same record.

    Merging at stage 2 locks in a record without md5. The next
    pipeline run then classifies that attachment as a 'stub' (see
    `ZoteroClient.pdf_map` — it keys on empty md5) and deletes it.
    Waiting for md5 before merging avoids this churn AND ensures
    our PATCH doesn't race with Desktop's md5 update (which would
    412 on version mismatch).

    Returns True when such an attachment appears, False on timeout.
    False is ambiguous (translator may be metadata-only, PDF may
    still be uploading, or Zotero Desktop may have stalled) —
    callers should proceed with the merge and let it log PARTIAL.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            children = zot.cloud.children(item_key) or []
        except Exception:
            children = []
        for c in children:
            data = c.get("data", {}) or {}
            if (data.get("itemType") == "attachment"
                    and data.get("md5")):
                return True
        time.sleep(1.0)
    return False


__all__ = [
    "ZoteroConnectorHandler",
    "ping_zotero_desktop",
    "resolve_connector_extension_path",
    "wait_for_service_worker",
]
