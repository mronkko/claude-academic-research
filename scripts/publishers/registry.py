"""Publisher registry for browser-based PDF retrieval.

Each publisher is a dict with:

- ``match``       — list of DOI prefixes routed to this publisher.
- ``url``         — download URL template; ``{doi}`` is substituted.
- ``name``        — display name.
- ``concurrency`` — max parallel page contexts.
- ``delay_s``     — delay between requests (rate-limit courtesy).
- ``use_page_nav``— True if ctx.request fails CF and real page navigation
                    is required (e.g. Taylor & Francis, Wiley fallback).
- ``flow``        — optional; for publishers with a multi-step
                    click-through (e.g. APA PsycNET = ``"psycnet"``).

To add a new publisher, add one entry below. If the publisher has an
unusual click-through flow, also add a ``download_via_<flow>`` handler
in ``fetch_pdfs_browser.py``.

Runtime override: ``fetch_pdfs_browser.py --publishers-json <path>`` loads
from a JSON file instead of this registry — useful for testing or
keeping institution-specific entries out of the public repo.
"""

from __future__ import annotations

DEFAULT_PUBLISHERS: dict[str, dict] = {
    "emerald": {
        "match": ["10.1108/"],
        "url": "https://www.emerald.com/insight/content/doi/{doi}/full/pdf?download=true",
        "name": "Emerald",
        "concurrency": 2,
        "delay_s": 0.0,
        "use_page_nav": False,
    },
    "sage": {
        "match": ["10.1177/"],
        "url": "https://journals.sagepub.com/doi/pdf/{doi}?download=true",
        "name": "Sage",
        # Sage returns HTTP 429 / session-block after ~30 sessions/minute
        "concurrency": 1,
        "delay_s": 2.5,
        "use_page_nav": False,
    },
    "tandf": {
        "match": ["10.1080/"],
        "url": "https://www.tandfonline.com/doi/pdf/{doi}?download=true",
        "name": "Taylor & Francis",
        # T&F's CF rejects ctx.request even with valid cookies — real page
        # nav is required
        "concurrency": 1,
        "delay_s": 1.0,
        "use_page_nav": True,
    },
    "wiley": {
        # Prefer fetch_pdfs_wiley_tdm.py (official Wiley TDM API) over the
        # browser flow. This entry is a fallback for Wiley DOIs not covered
        # by the institution's TDM agreement.
        "match": ["10.1002/", "10.1111/", "10.1046/"],
        "url": "https://onlinelibrary.wiley.com/doi/pdf/{doi}?download=true",
        "name": "Wiley (fallback)",
        "concurrency": 1,
        "delay_s": 1.0,
        "use_page_nav": True,
    },
    "aom": {
        "match": ["10.5465/"],
        "url": "https://journals.aom.org/doi/pdf/{doi}?download=true",
        "name": "Academy of Management",
        "concurrency": 1,
        "delay_s": 1.0,
        "use_page_nav": True,
    },
    "informs": {
        "match": ["10.1287/"],
        "url": "https://pubsonline.informs.org/doi/pdf/{doi}?download=true",
        "name": "INFORMS",
        "concurrency": 1,
        "delay_s": 1.0,
        "use_page_nav": True,
    },
    "apa": {
        # APA PsycNET: DOI → landing page → "Get Access" → "CHECK ACCESS"
        # → "DOWNLOAD PDF". Uses a dedicated click-through flow.
        "match": ["10.1037/"],
        "url": "https://doi.org/{doi}",
        "name": "APA PsycNET",
        "flow": "psycnet",
        "concurrency": 1,
        "delay_s": 1.0,
    },
    "oup": {
        "match": ["10.1093/"],
        "url": "https://academic.oup.com/article-pdf/doi/{doi}",
        "name": "Oxford University Press",
        "concurrency": 1,
        "delay_s": 1.0,
        "use_page_nav": True,
    },
    "aaa": {
        "match": ["10.2308/"],
        "url": "https://publications.aaahq.org/accounting-review/article-pdf/doi/{doi}",
        "name": "AAA (Accounting Review)",
        "concurrency": 1,
        "delay_s": 1.0,
        "use_page_nav": True,
    },
}
