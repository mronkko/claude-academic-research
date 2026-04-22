"""Pytest fixtures and path setup for the claude-academic-research test suite."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_ROOT = REPO_ROOT / "scripts"
PIPELINES_ROOT = SCRIPTS_ROOT / "pipelines"

# Make `publishers`, `sources`, `core` importable without the sys.path
# tricks the pipeline scripts do at runtime. Also add `scripts/pipelines`
# so test modules can `import zotero_io` / `import http_client` directly,
# mirroring the runtime path layout.
for _p in (SCRIPTS_ROOT, PIPELINES_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
