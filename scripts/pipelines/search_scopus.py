#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pybliometrics>=3.6",
#     "requests>=2.31",
# ]
# ///
"""Single-database wrapper around `searchers.scopus` for piloting.

Thin shim that delegates to `search.py --databases scopus`. Useful
when tuning `QUERY_DEFS` on the Scopus side alone (avoids paying
WoS quota on every iteration).

Usage:
    uv run search_scopus.py --config ./search_config.py
"""

from __future__ import annotations

import sys

from search import main as _search_main

if __name__ == "__main__":
    sys.argv = [sys.argv[0], *sys.argv[1:], "--databases", "scopus"]
    sys.exit(_search_main())
