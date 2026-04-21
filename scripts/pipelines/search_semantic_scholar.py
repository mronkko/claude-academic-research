#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pybliometrics>=3.6",
#     "requests>=2.31",
# ]
# ///
"""Single-database wrapper around `searchers.semantic_scholar` for piloting.

Thin shim that delegates to `search.py --databases semantic_scholar`.
Semantic Scholar does not filter by ISSN at the API level; the source
post-filters client-side against `config.JOURNALS`, so results can be
noisier than Scopus/WoS. Best used as a complementary signal on
`BLOCK_A_TERMS` / `BLOCK_B_TERMS` piloting.

An API key (`SEMANTIC_SCHOLAR_API_KEY`) is not required but strongly
recommended — the unauthenticated tier shares a 1 rps rate limit
across all callers globally.

Usage:
    uv run search_semantic_scholar.py --config ./search_config.py
"""

from __future__ import annotations

import sys

from search import main as _search_main

if __name__ == "__main__":
    sys.argv = [sys.argv[0], *sys.argv[1:], "--databases", "semantic_scholar"]
    sys.exit(_search_main())
