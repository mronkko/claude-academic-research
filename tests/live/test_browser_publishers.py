"""Live tests for Cloudflare-gated publishers via the fetchers.browser handlers.

Opt in with `pytest -m live_browser`. Opens a persistent Chromium
session (shared across all tests in this file) and exercises each
publisher handler's real `setup()` + `download()` flow — the same code
path `enrich_pdfs.py --sources browser` drives in production. User
clicks through Cloudflare challenges and institutional SSO as prompted
— once per publisher domain over the whole run.

Parametrized directly from `fetchers.browser.all_handlers()`, so a new
handler added there automatically gets a test here as long as
`KNOWN_DOIS` also contains a DOI for it.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from tests.live.conftest import KNOWN_DOIS

pytestmark = pytest.mark.live_browser

REPO_ROOT = Path(__file__).resolve().parents[2]


def _handlers() -> list:
    """Enumerate handler instances, for parametrize.

    `fetchers.browser` is importable without playwright installed (the
    playwright imports are TYPE_CHECKING-guarded), so collection works
    everywhere; actually running a test requires playwright and is
    gated by the fixture's importorskip.
    """
    from fetchers.browser import all_handlers
    return all_handlers()


def _no_access_publishers() -> set[str]:
    """Publishers persisted to `[library] no_access` in config.toml.

    Same parsing as `enrich_pdfs.py`: the user answered [A]lways-skip
    at a setup prompt, declaring their institution has no access. The
    production pipeline short-circuits these straight to the Connector
    fallback, so a failed download here is expected, not a bug.
    """
    from core.config_loader import load_config
    raw = load_config().get("library", {}).get("no_access", [])
    if isinstance(raw, str):
        return {s.strip() for s in raw.split(",") if s.strip()}
    if isinstance(raw, list):
        return {str(s).strip() for s in raw if s}
    return set()


@pytest.fixture(scope="module")
def browser_session():
    """Module-scoped persistent Chromium driven on a private event loop.

    Uses the production `launch_context()` (persistent profile under
    `<cache_dir>/.chrome-profile`, built-in PDF viewer disabled), so CF
    cookies and institutional SSO survive between tests and runs.
    """
    pytest.importorskip(
        "playwright.async_api",
        reason="live_browser tests require `playwright` — install with "
               "`uv pip install playwright && playwright install chromium`",
    )
    from fetchers.browser import launch_context
    from playwright.async_api import async_playwright

    cache_dir = REPO_ROOT / ".pytest-playwright-cache"
    cache_dir.mkdir(exist_ok=True)

    print()
    print("=" * 72)
    print("  live_browser test session starting")
    print("=" * 72)
    print()
    print("  A Chromium window will open on your desktop. For each publisher")
    print("  domain the tests cover, you may need to:")
    print()
    print("    1. Solve a Cloudflare challenge (click the checkbox).")
    print("    2. Sign in via your institution's SSO.")
    print()
    print("  The session is shared across all live_browser tests, so you only")
    print("  see each challenge once per publisher domain for the whole run.")
    print()
    print("  Leave the terminal and the browser window open until done.")
    print("=" * 72, flush=True)
    print()

    loop = asyncio.new_event_loop()
    pw = loop.run_until_complete(async_playwright().start())
    ctx = loop.run_until_complete(launch_context(pw, cache_dir))
    yield loop, ctx, cache_dir
    loop.run_until_complete(ctx.close())
    loop.run_until_complete(pw.stop())
    loop.close()


@pytest.mark.parametrize("handler", _handlers(), ids=lambda h: h.name)
def test_browser_publisher_downloads_pdf(handler, browser_session) -> None:
    """Every registered handler downloads a `%PDF-` payload for its known DOI."""
    if handler.name not in KNOWN_DOIS:
        pytest.skip(
            f"No test DOI for publisher {handler.name!r} in KNOWN_DOIS. "
            f"Add one to tests/live/conftest.py."
        )
    if handler.name in _no_access_publishers():
        pytest.skip(
            f"[{handler.name}] in [library] no_access (config.toml) — "
            f"your institution has no access; the pipeline routes these "
            f"items to the Connector fallback. Remove the entry from "
            f"config.toml to test this publisher again."
        )
    doi = KNOWN_DOIS[handler.name]
    loop, ctx, cache_dir = browser_session

    from fetchers.browser import Counter, cache_path_for, is_cached
    from fetchers.browser.base import normalise_setup_result

    # Force a real download — a cached PDF from a previous run would
    # let download() short-circuit without exercising the flow.
    cache_path_for(cache_dir, doi).unlink(missing_ok=True)

    async def run():
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        outcome = normalise_setup_result(await handler.setup(page, doi))
        if outcome != "proceed":
            return outcome, None
        result = await handler.download(
            page, ctx, {"doi": doi, "title": f"live test {handler.name}"},
            cache_dir, counter=Counter(), total=1, t_start=time.monotonic(),
        )
        return outcome, result

    outcome, result = loop.run_until_complete(run())
    if outcome == "always_skip":
        # Honour the prompt's promise: persist to [library] no_access,
        # exactly as the enrich_pdfs.py driver does, so future test
        # runs (and pipeline runs) skip this publisher up front.
        from core.config_writer import append_to_list
        try:
            append_to_list("library", "no_access", handler.name)
            persisted = "persisted to [library] no_access"
        except Exception as e:
            persisted = f"could not persist to config.toml: {e}"
        pytest.skip(
            f"[{handler.name}] user chose always-skip at setup; "
            f"{persisted}."
        )
    if outcome != "proceed":
        pytest.skip(f"[{handler.name}] user chose {outcome!r} at setup.")
    assert result is not None, (
        f"[{handler.name}] download() returned None for DOI {doi} — the "
        f"handler's ERROR line above has the specific reason. If your "
        f"institution has no access to this publisher, answer "
        f"[A]lways-skip at its setup prompt instead of [Y]es: that "
        f"persists it to [library] no_access and this test will skip it."
    )
    path, _source_url = result
    assert is_cached(path), (
        f"[{handler.name}] {path} is missing or not a %PDF- payload. "
        f"Likely an HTML wrapper or access-denied page."
    )
