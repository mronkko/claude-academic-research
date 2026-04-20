"""Live tests for Cloudflare-gated publishers via Playwright.

Opt in with `pytest -m live_browser`. Opens a persistent Chromium
session (shared across all tests in this file) and exercises each
publisher's download flow from the pipeline script. User clicks
through Cloudflare challenges and institutional SSO as prompted —
once per publisher domain over the whole run.

Parametrized directly from `publishers.registry.DEFAULT_PUBLISHERS`,
so a new publisher added there automatically gets a test here as long
as `KNOWN_DOIS` also contains a DOI for it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from tests.live.conftest import KNOWN_DOIS

pytestmark = pytest.mark.live_browser

REPO_ROOT = Path(__file__).resolve().parents[2]
FETCHER_PATH = REPO_ROOT / "scripts" / "pipelines" / "fetch_pdfs_browser.py"


def _fetcher():
    """Load fetch_pdfs_browser.py by path; its sibling imports need sys.path."""
    scripts_dir = str(REPO_ROOT / "scripts")
    pipelines_dir = str(REPO_ROOT / "scripts" / "pipelines")
    for p in (scripts_dir, pipelines_dir):
        if p not in sys.path:
            sys.path.insert(0, p)
    spec = importlib.util.spec_from_file_location(
        "fetch_pdfs_browser", FETCHER_PATH,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fetch_pdfs_browser"] = mod
    spec.loader.exec_module(mod)
    return mod


def _publisher_keys() -> list[str]:
    """Enumerate registry keys, for parametrize."""
    scripts_dir = str(REPO_ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    from publishers.registry import DEFAULT_PUBLISHERS
    return sorted(DEFAULT_PUBLISHERS.keys())


@pytest.mark.parametrize("publisher_key", _publisher_keys())
def test_browser_publisher_downloads_pdf(publisher_key: str, browser_context, tmp_path) -> None:
    """Every registry publisher downloads a `%PDF-` payload for its known DOI."""
    pytest.importorskip(
        "playwright.sync_api",
        reason="live_browser tests need playwright installed",
    )
    if publisher_key not in KNOWN_DOIS:
        pytest.skip(
            f"No test DOI for publisher {publisher_key!r} in KNOWN_DOIS. "
            f"Add one to tests/live/conftest.py."
        )

    doi = KNOWN_DOIS[publisher_key]

    fetcher = _fetcher()
    publishers = fetcher.DEFAULT_PUBLISHERS
    info = publishers[publisher_key]

    # Use the session-scoped Chromium context; grab a page from it.
    page = browser_context.pages[0] if browser_context.pages else browser_context.new_page()

    url = info["url"].format(doi=doi)
    out_path = tmp_path / (doi.replace("/", "_") + ".pdf")

    print(f"\n  [{publisher_key}] navigating to {url}", flush=True)

    # The pipeline's async handlers expect asyncio + Playwright's async API.
    # The browser_context fixture uses sync Playwright (simpler for tests).
    # For the live test, we reproduce the essential behaviour in sync form:
    # navigate, wait for download, verify magic bytes.
    try:
        with page.expect_download(timeout=60000) as dl_info:
            try:
                page.goto(url, wait_until="commit", timeout=30000)
            except Exception:
                pass  # download event interrupts navigation
        dl = dl_info.value
        dl.save_as(str(out_path))
    except Exception as e:
        pytest.fail(
            f"[{publisher_key}] no download event from {url}: {e}. "
            f"Likely CF challenge unsolved or institutional access missing."
        )

    assert out_path.exists(), f"[{publisher_key}] download path missing"
    with open(out_path, "rb") as f:
        head = f.read(5)
    assert head == b"%PDF-", (
        f"[{publisher_key}] did not return a PDF for DOI {doi} "
        f"(got {head!r}). Likely an HTML wrapper or access-denied page."
    )
