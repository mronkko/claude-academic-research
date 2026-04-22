"""Shared fixtures and helpers for the live test suite.

The live tests (opt-in via `pytest -m live` or `pytest -m live_browser`)
probe real external services. This module provides:

- `KNOWN_DOIS` — the test corpus, one stable DOI per endpoint.
- `require_config()` — skip-if-missing helper for API keys.
- `http_get()` — plain urllib GET that returns (status, body, headers).
- `classify_non_pdf_body()` — match the reference script's failure
  taxonomy (CF / paywall / no-subscription / HTML wrapper / other).
- `browser_context` — session-scoped Playwright fixture shared by all
  `@live_browser` tests so the user solves CF once per publisher
  domain rather than once per test.
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_ROOT = REPO_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))
if str(SCRIPTS_ROOT / "pipelines") not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT / "pipelines"))


# ---------------------------------------------------------------------------
# Known-stable DOIs.
#
# TODO(user): replace with DOIs you know work at your institution. These
# placeholders are chosen from widely-indexed open-access papers and may or
# may not resolve cleanly at every endpoint. See tests/live/README.md for
# how to swap them in.
# ---------------------------------------------------------------------------

KNOWN_DOIS: dict[str, str] = {
    # Direct-HTTP PDF endpoints
    "crossref_tdm": "10.1016/j.jbusvent.2006.10.003",  # JBV 2007 — Elsevier deposits 2 text-mining links
    "pmc": "10.1371/journal.pone.0012345",              # PLOS ONE (PMC-indexed)
    "elsevier": "10.1016/j.jbusvent.2006.10.003",       # JBV 2007, Elsevier
    "openalex_content": "10.1371/journal.pone.0012345",
    "unpaywall": "10.1371/journal.pone.0012345",
    "openalex_oa": "10.1371/journal.pone.0012345",
    "wiley_tdm": "10.1002/smj.70090",                    # SMJ (user-confirmed in TDM scope). ETP moved to Sage in 2022.

    # Direct-HTTP abstract endpoints.
    # Publishers increasingly have Semantic Scholar elide abstracts per-DOI;
    # only fully-OA papers (PLOS, PMC) reliably return them via the S2 API.
    # Crossref abstracts depend on publisher deposit — Wiley deposits, many
    # Elsevier papers do not. Pick each DOI for the specific provider.
    "crossref_abstract": "10.1002/smj.70090",            # Wiley — deposits JATS abstracts at Crossref
    "semantic_scholar_abstract": "10.1371/journal.pone.0012345",  # PLOS ONE — not elided by publisher
    "scopus_abstract": "10.1016/j.jbusvent.2006.10.003",  # Scopus has via pybliometrics view=FULL
    "sciencedirect_abstract": "10.1016/j.jbusvent.2006.10.003",
    "openalex_grobid": "10.1016/j.jbusvent.2006.10.003",

    # Web of Science abstract endpoints.
    # - wos_abstract: DOI that WoS indexes AND for which the publisher
    #   deposited the abstract content (AMD 2015 Priming Affect — verified
    #   2,124-char abstract in WoS).
    # - wos_title_fallback_doi + _title: DOI where WoS indexes the paper
    #   under a *different* DOI alias. AoM Annals pre-2014 was published
    #   by Routledge/T&F (10.1080/...); the AoM re-issued DOI (10.5465/...)
    #   is what most libraries carry but WoS kept the original prefix.
    #   WosSource must recover this via the title-search fallback.
    "wos_abstract": "10.5465/amd.2015.0052",
    "wos_title_fallback_doi": "10.5465/19416520.2014.875669",
    "wos_title_fallback_title": "Putting Framing in Perspective: A Review of Framing and Frame Analysis",

    # Browser-based publishers (CF-gated; require institutional access)
    "sage":     "10.1177/1042258717725967",           # ETP 2018
    "emerald":  "10.1108/IJEBR-08-2019-0513",         # IJEBR 2020
    "tandf":    "10.1080/08985626.2020.1727096",      # Entrepreneurship & Regional Dev 2020
    "wiley":    "10.1002/smj.70090",                    # SMJ — Wiley browser fallback (ETP moved to Sage in 2022)
    "aom":      "10.5465/amj.2014.0387",               # AMJ 2016
    "informs":  "10.1287/orsc.2017.1182",              # Org Science 2018
    "apa":      "10.1037/0021-9010.93.3.481",          # JAP 2008
    "oup":      "10.1093/jleo/ewaa004",                # J of Law, Econ & Org 2020
    "aaa":      "10.2308/accr-52421",                  # Accounting Review 2019
}


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def require_config(section: str, key: str, env: str) -> str:
    """Fetch a config value; skip the test cleanly if it is unset."""
    from core.config_loader import get
    val = get(section, key, env=env)
    if not val:
        pytest.skip(f"{env} (or config [{section}].{key}) not set; skipping live test.")
    return val


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only)
# ---------------------------------------------------------------------------


def http_get(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: int = 20,
) -> tuple[int, bytes, dict[str, str]]:
    """Plain urllib GET; returns (status, body_bytes, headers). 0 on network error."""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read() if hasattr(e, "read") else b"", dict(e.headers or {})
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, b"", {}


def classify_non_pdf_body(body: bytes) -> str:
    """Reference-script body taxonomy: explain why a non-PDF came back."""
    if body[:5] == b"%PDF-":
        return "is a PDF"
    text = body[:4000].decode("utf-8", errors="replace").lower()
    if "just a moment" in text or "cf-chl" in text or "cloudflare" in text:
        return "Cloudflare challenge page"
    if "access" in text and ("denied" in text or "not available" in text or "subscri" in text):
        return "access denied / no subscription"
    if "purchase" in text or "buy" in text or "rent" in text:
        return "paywall / purchase prompt"
    if text.lstrip().startswith("<"):
        return f"HTML response ({len(body)} bytes)"
    return f"unknown non-PDF body ({len(body)} bytes)"


# ---------------------------------------------------------------------------
# Playwright session — shared across all @live_browser tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def browser_context() -> Any:
    """Session-scoped persistent Chromium. One CF challenge per domain total."""
    pytest.importorskip(
        "playwright.sync_api",
        reason="live_browser tests require `playwright` — install with "
               "`uv pip install playwright && playwright install chromium`",
    )
    import json

    from playwright.sync_api import sync_playwright

    # Prep persistent profile with built-in PDF viewer disabled so PDF URLs
    # fire download events instead of opening inline.
    user_data_dir = REPO_ROOT / ".pytest-playwright-profile"
    user_data_dir.mkdir(exist_ok=True)
    prefs_dir = user_data_dir / "Default"
    prefs_dir.mkdir(exist_ok=True)
    prefs_file = prefs_dir / "Preferences"
    prefs: dict[str, Any] = {}
    if prefs_file.exists():
        try:
            prefs = json.loads(prefs_file.read_text())
        except Exception:
            prefs = {}
    prefs.setdefault("plugins", {})["always_open_pdf_externally"] = True
    prefs_file.write_text(json.dumps(prefs))

    # Loud pre-run banner so the user knows what is about to happen.
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

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=False,
            accept_downloads=True,
            viewport={"width": 1200, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        yield ctx
        ctx.close()
