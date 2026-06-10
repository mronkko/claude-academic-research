"""Live guard: every KNOWN_DOIS entry is a registered DOI.

The browser-publisher and endpoint tests can only fail meaningfully if
their test DOIs exist in the first place — a dead placeholder makes
every downstream failure look like an access problem. This test asks
the doi.org handle API (no Cloudflare, no auth) whether each DOI is
registered, so a stale entry is caught on every `pytest -m live` run
instead of mid-browser-session.
"""

from __future__ import annotations

import json

import pytest

from tests.live.conftest import KNOWN_DOIS, http_get

pytestmark = pytest.mark.live

# KNOWN_DOIS also carries one paper *title* (wos_title_fallback_title)
# for the WoS title-search fallback — only handle-check actual DOIs.
_UNIQUE_DOIS = sorted({v for v in KNOWN_DOIS.values() if v.startswith("10.")})


@pytest.mark.parametrize("doi", _UNIQUE_DOIS)
def test_known_dois_resolve(doi: str) -> None:
    keys = sorted(k for k, v in KNOWN_DOIS.items() if v == doi)
    status, body, _headers = http_get(f"https://doi.org/api/handles/{doi}")
    assert status == 200, (
        f"DOI {doi} (KNOWN_DOIS keys: {keys}) is not registered at "
        f"doi.org (handle API returned {status}). Replace it in "
        f"tests/live/conftest.py with a registered DOI."
    )
    payload = json.loads(body.decode("utf-8"))
    assert payload.get("responseCode") == 1, (
        f"DOI {doi} (KNOWN_DOIS keys: {keys}) handle lookup returned "
        f"responseCode {payload.get('responseCode')} — expected 1 "
        f"(registered)."
    )
