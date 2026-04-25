#!/usr/bin/env python3
"""Cross-platform `mkdir -p` for skills.

Usage:
    python3 ensure_dir.py DIR [DIR ...]

Creates each directory (and any missing parents). Idempotent — silent
on already-existing paths. Skills use this to create project-local
working directories (`.claude/fact-check/`, `critic-reviews/`, etc.)
before writing reports.

Kept as a script (not an inline `python -c`) so the wizard's existing
`Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/**)` allow rule covers it.
Windows built-in `mkdir` has no `-p` flag, which is why this exists.
"""
import sys
from pathlib import Path

for d in sys.argv[1:]:
    Path(d).mkdir(parents=True, exist_ok=True)
