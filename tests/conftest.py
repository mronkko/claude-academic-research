"""Pytest fixtures and path setup for the claude-academic-research test suite."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_ROOT = REPO_ROOT / "scripts"

# Make `publishers`, `sources`, `core` importable without the sys.path
# tricks the pipeline scripts do at runtime.
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))
