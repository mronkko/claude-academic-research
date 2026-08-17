"""Shared fixtures and helpers for the live test suite.

The live tests (opt-in via `pytest -m live` or `pytest -m live_browser`)
probe real external services. This module provides:

- `KNOWN_DOIS` — the test corpus, one stable DOI per endpoint.
- `require_config()` — skip-if-missing helper for API keys.
- `http_get()` — plain urllib GET that returns (status, body, headers).
- `classify_non_pdf_body()` — match the reference script's failure
  taxonomy (CF / paywall / no-subscription / HTML wrapper / other).

The shared Playwright session for `@live_browser` tests lives in
`test_browser_publishers.py` (module-scoped `browser_session` fixture
driving the production `fetchers.browser` handlers).
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request
from pathlib import Path

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
    "semantic_scholar_pdf": "10.1371/journal.pone.0012345",  # PLOS ONE — S2 resolves an openAccessPdf
    "core": "10.1371/journal.pone.0012345",              # PLOS ONE — harvested into repositories
    "preprint": "10.1103/PhysRevLett.116.061102",        # LIGO GW150914, PRL 2016 — arXiv:1602.03837 is its preprint
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

    # Browser-based publishers (CF-gated; require institutional access).
    # All DOIs verified registered (doi.org → 302) on 2026-06-10;
    # `test_known_dois_resolve` re-checks them on every `-m live` run.
    # Resolution only proves the DOI exists — whether the PDF downloads
    # still depends on your institution's subscriptions.
    "sage":     "10.1177/10422587241306872",          # ETP 2025
    # Journal of Business Ethics 2018. Chosen because the Alma resolver
    # reports 15 licensed routes for it (EBSCOhost, JSTOR, ProQuest and
    # FinELib SpringerLink), so a failure here is the Imperva JS
    # challenge rather than an entitlement gap — which is exactly what
    # the browser handler exists to clear.
    "springer": "10.1007/s10551-018-4026-8",
    "emerald":  "10.1108/ijebr-05-2024-0509",         # IJEBR 2024
    "tandf":    "10.1080/08985626.2024.2444907",      # Entrepreneurship & Regional Dev 2024
    "wiley":    "10.1002/smj.70090",                    # SMJ — Wiley browser fallback (ETP moved to Sage in 2022)
    "aom":      "10.5465/amj.2021.0676",               # AMJ 2023
    "informs":  "10.1287/orsc.2017.1182",              # Org Science 2018
    "apa":      "10.1037/apl0001090",                  # JAP 2023
    "oup":      "10.1093/jleo/ewaa004",                # J of Law, Econ & Org 2020
    "aaa":      "10.2308/tar-2023-0399",               # Accounting Review 2024
}


# ---------------------------------------------------------------------------
# APA PsycNET regression corpus.
#
# Both failed 2-of-2 in a live 0.11.0 run with `Download button not
# found` while the operator downloaded each of them by hand from the
# same Chromium profile — i.e. reachable, licensed, and still broken.
# The generic one-DOI-per-handler smoke test could not have caught that,
# so these two are asserted separately by
# `test_apa_regression_dois_download`.
# ---------------------------------------------------------------------------

APA_REGRESSION_DOIS: dict[str, str] = {
    # J. Applied Psychology 2015, record 2015-01016-001
    "10.1037/apl0000007": "Sinking slowly: Diversity in propensity to trust",
    # J. Applied Psychology 2012, record 2011-19052-001
    "10.1037/a0025231": "Bridging team faultlines by combining task role assignment",
}


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def require_config(section: str, key: str, env: str = "") -> str:
    """Fetch a config value; skip the test cleanly if it is unset.

    `env` is optional because some settings have no conventional
    environment variable — the institutional gateway's, for instance,
    live only in `config.toml`, since any name the plugin invented would
    collide with whatever the user already exports. Omitting it reads
    the file alone and phrases the skip accordingly.
    """
    from core.config_loader import get
    val = get(section, key, env=env or None)
    if not val:
        where = (
            f"{env} (or config [{section}].{key})" if env
            else f"config [{section}].{key}"
        )
        pytest.skip(f"{where} not set; skipping live test.")
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
