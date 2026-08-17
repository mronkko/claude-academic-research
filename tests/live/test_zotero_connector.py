"""Live pre-flight tests for the Zotero Connector fallback.

The Connector phase of `enrich_pdfs.py` needs three things that no
unit test can vouch for on a given machine:

  1. The Zotero Connector extension unpacked on disk (installed in the
     user's regular Chrome — the script side-loads it from there).
  2. Zotero Desktop running with its connector server enabled.
  3. The extension actually booting inside the bundled Playwright
     Chromium (service worker starts).

A machine that fails any of these silently loses the whole Connector
phase — every third-party-platform item gets logged as unreachable.
Opt in with `pytest -m live_browser`; the third test opens a visible
Chromium window briefly.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytestmark = pytest.mark.live_browser

REPO_ROOT = Path(__file__).resolve().parents[2]

_INSTALL_HINT = (
    "Zotero Connector extension not found on disk. Install it in your "
    "regular Google Chrome (https://www.zotero.org/download/connectors) "
    "— the pipeline side-loads the unpacked extension from Chrome's "
    "profile — or set [zotero_connector] extension_dir in "
    "~/.config/academic-research/config.toml to its unpacked location."
)


def _explicit_extension_dir() -> str | None:
    from core.config_loader import get
    return get("zotero_connector", "extension_dir") or None


def _resolve_extension() -> Path | None:
    from fetchers.browser import resolve_connector_extension_path
    return resolve_connector_extension_path(_explicit_extension_dir())


def test_connector_extension_found_on_disk() -> None:
    ext = _resolve_extension()
    assert ext is not None, _INSTALL_HINT
    assert (ext / "manifest.json").exists(), (
        f"{ext} exists but has no manifest.json — not an unpacked "
        f"extension directory. {_INSTALL_HINT}"
    )


def test_zotero_desktop_is_running() -> None:
    import requests
    from fetchers.browser import ping_zotero_desktop
    assert ping_zotero_desktop(requests.Session()), (
        "Zotero Desktop is not running (or its connector server on "
        "localhost:23119 is disabled). Start Zotero Desktop and check "
        "Settings → Advanced → 'Allow other applications on this "
        "computer to communicate with Zotero'."
    )


def test_connector_loads_in_playwright_chromium() -> None:
    """Side-load the extension into the bundled Chromium and verify its
    Manifest V3 service worker boots — the mechanism the Connector
    save flow depends on."""
    pytest.importorskip(
        "playwright.async_api",
        reason="live_browser tests need playwright installed",
    )
    from fetchers.browser import launch_context, wait_for_service_worker
    from playwright.async_api import async_playwright

    ext = _resolve_extension()
    assert ext is not None, _INSTALL_HINT

    cache_dir = REPO_ROOT / ".pytest-playwright-cache"
    cache_dir.mkdir(exist_ok=True)

    async def run() -> bool:
        async with async_playwright() as p:
            ctx = await launch_context(p, cache_dir, extensions=[ext])
            try:
                # A page load kicks off the extension lifecycle.
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await page.goto("https://www.zotero.org", timeout=30000)
                worker = await wait_for_service_worker(ctx, timeout_s=20)
                return worker is not None
            finally:
                await ctx.close()

    loop = asyncio.new_event_loop()
    try:
        booted = loop.run_until_complete(run())
    finally:
        loop.close()
    assert booted, (
        f"Zotero Connector service worker did not start within 20s in "
        f"the bundled Chromium (extension loaded from {ext}). The "
        f"Connector save flow cannot work without it — check that the "
        f"extension version supports Manifest V3 side-loading."
    )


# ---------------------------------------------------------------------------
# Direct end-to-end save + merge test.
# ---------------------------------------------------------------------------

# An open-access PLOS ONE article on bacteria screening — deliberately
# far from this plugin's social-science audience, so the pre-flight
# "DOI must not already be in the library" guard rarely trips. The
# article page needs no Cloudflare solve and no institutional access,
# yet drives the identical translator-detection → saveWithTranslator →
# poll → cloud-sync → merge chain a paywalled SFX target would.
_OA_TEST_DOI = "10.1371/journal.pone.0310979"
_OA_TEST_URL = f"https://journals.plos.org/plosone/article?id={_OA_TEST_DOI}"


def test_connector_full_save_and_merge() -> None:
    """Drive `ZoteroConnectorHandler.download_and_attach` for real.

    Transiently writes to the Zotero library that Zotero Desktop has
    selected: creates a stub item carrying the test DOI, lets the
    Connector save the OA article, verifies the save merges into the
    stub (PDF child moved), then deletes the stub. The Connector-saved
    duplicate parent ends up in the library trash (merge puts it
    there) — empty the trash whenever convenient.

    Interactive: the per-host confirmation prompt fires once for
    journals.plos.org — press Enter when the article page is visible.
    """
    pytest.importorskip(
        "playwright.async_api",
        reason="live_browser tests need playwright installed",
    )
    import time

    import zotero_io
    from fetchers.browser import (
        Counter,
        ZoteroConnectorHandler,
        launch_context,
        ping_zotero_desktop,
        wait_for_service_worker,
    )
    from playwright.async_api import async_playwright

    ext = _resolve_extension()
    assert ext is not None, _INSTALL_HINT

    import requests
    if not ping_zotero_desktop(requests.Session()):
        pytest.fail("Zotero Desktop is not running — start it and re-run.")

    # Target library. Defaults to the personal "My Library" — always
    # present, and test artefacts never land in a shared group library
    # where collaborators would see them. Set ZOTERO_GROUP to target a
    # specific group instead.
    import os
    group_override = os.environ.get("ZOTERO_GROUP", "").strip()

    if group_override:
        zot = zotero_io.ZoteroClient.from_config(group_id=group_override)
    else:
        from core.config_loader import get
        user_id = get("zotero", "user_id", env="ZOTERO_USER_ID")
        if not user_id:
            pytest.skip(
                "No zotero user_id in config.toml — run the /setup wizard "
                "(or set ZOTERO_GROUP to target a group library instead)."
            )
        zot = zotero_io.ZoteroClient.for_user_library(user_id)

    # The Connector saves into whichever library Zotero Desktop has
    # selected in its left pane — refuse to run unless that matches
    # the client's target (mirrors the enrich_pdfs.py pre-flight).
    selected = zot.selected_local_library()
    if selected is None:
        pytest.skip(
            "Could not determine the library selected in Zotero Desktop "
            "(getSelectedCollection unavailable). Select the target "
            "library in Zotero Desktop's left pane and re-run."
        )
    cloud_gid = selected.get("groupID") or selected.get("groupId")
    if group_override:
        if cloud_gid is not None:
            matched = str(cloud_gid) == str(zot.group_id)
        else:
            matched = (
                (selected.get("libraryName") or "") == (zot.group_name() or "")
            )
        target_desc = f"group {zot.group_id}"
    else:
        # My Library is local libraryID 1 and never carries a group ID.
        matched = cloud_gid is None and selected.get("libraryID") == 1
        target_desc = "'My Library'"
    if not matched:
        pytest.skip(
            f"Zotero Desktop has {selected.get('libraryName')!r} selected "
            f"but the test targets {target_desc}. Click it in Zotero "
            f"Desktop's left pane and re-run."
        )

    # Safety guard: _poll_for_new_item matches ANY item with this DOI,
    # so a pre-existing one would be falsely merged and then deleted
    # by our cleanup. Refuse to run rather than risk a real item.
    needle = _OA_TEST_DOI.lower()
    for it in zot.journal_articles():
        if (it.get("data", {}).get("DOI") or "").strip().lower() == needle:
            pytest.skip(
                f"An item with test DOI {_OA_TEST_DOI} already exists in "
                f"the library ({it.get('key')}) — refusing to touch it. "
                f"Remove it (or change _OA_TEST_DOI) to run this test."
            )

    # Stub item — plays the role of the SLR item that's missing its PDF.
    resp = zot.cloud.create_items([{
        "itemType": "journalArticle",
        "title": "[connector-live-test stub — safe to delete]",
        "DOI": _OA_TEST_DOI,
    }])
    success = resp.get("success") or resp.get("successful") or {}
    stub_key = ""
    if isinstance(success, dict) and success:
        first = next(iter(success.values()))
        stub_key = (
            first.get("key") or first.get("data", {}).get("key", "")
            if isinstance(first, dict) else str(first)
        )
    assert stub_key, f"could not create stub item: {resp!r}"

    cache_dir = REPO_ROOT / ".pytest-playwright-cache"
    cache_dir.mkdir(exist_ok=True)

    async def run():
        async with async_playwright() as p:
            ctx = await launch_context(p, cache_dir, extensions=[ext])
            try:
                worker = await wait_for_service_worker(ctx, timeout_s=20)
                if worker is None:
                    return None
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                handler = ZoteroConnectorHandler(extension_path=ext)
                return await handler.download_and_attach(
                    page, ctx, worker,
                    {
                        "item_key": stub_key,
                        "doi": _OA_TEST_DOI,
                        "title": "connector live test (PLOS ONE, OA)",
                        "resolver_target_url": _OA_TEST_URL,
                    },
                    zot,
                    counter=Counter(), total=1, t_start=time.monotonic(),
                )
            finally:
                await ctx.close()

    loop = asyncio.new_event_loop()
    try:
        ok = loop.run_until_complete(run())
    finally:
        loop.close()
        deleted = zot.delete_item(stub_key)
        print(f"\n  cleanup: stub {stub_key} "
              f"{'deleted' if deleted else 'NOT deleted — remove manually'}",
              flush=True)

    assert ok is not None, (
        "Connector service worker did not start — see "
        "test_connector_loads_in_playwright_chromium."
    )
    assert ok, (
        "download_and_attach() reported failure — the step-by-step "
        "diagnostics above show where the chain broke (translator "
        "detection, save, local poll, cloud sync, or merge)."
    )
