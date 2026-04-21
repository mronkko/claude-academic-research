#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "requests>=2.31",
# ]
# ///
"""Single-database wrapper around `searchers.openalex` for piloting.

Thin shim that delegates to `search.py --databases openalex`. Useful
when iterating on the block-term list without paying for
Scopus/WoS API round-trips each run.

Usage:
    uv run search_openalex.py --config ./search_config.py
"""

from __future__ import annotations

import sys

from search import main as _search_main

if __name__ == "__main__":
    # Rewrite argv so the orchestrator runs OpenAlex only, preserving
    # every other flag (--config, --output-dir, --metadata-dir).
    sys.argv = [sys.argv[0], *sys.argv[1:], "--databases", "openalex"]
    sys.exit(_search_main())
