#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pybliometrics>=3.6",
#     "requests>=2.31",
# ]
# ///
"""Single-database wrapper around `searchers.wos` for piloting.

Thin shim that delegates to `search.py --databases wos`. Useful when
iterating on `QUERY_DEFS` on the WoS side (wildcard stemming,
ISSN filters) without paying Scopus quota.

Requires `WOS_API_KEY_EXTENDED` — the Starter API does not support
`IS=` filters and is not suitable for formal scoped searches.

Usage:
    uv run search_wos.py --config ./search_config.py
"""

from __future__ import annotations

import sys

from search import main as _search_main

if __name__ == "__main__":
    sys.argv = [sys.argv[0], *sys.argv[1:], "--databases", "wos"]
    sys.exit(_search_main())
