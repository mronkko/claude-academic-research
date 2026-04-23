#!/usr/bin/env python3
"""Check that a skill's required template files exist in the project.

Usage:
    python3 check_project_scaffold.py FILE [FILE ...]

Each FILE is a project-relative path. Prints 'ok' if every path is a
regular file, otherwise 'missing: <comma-separated>'. Skills use the
output to decide whether to copy in templates.

Kept as a script (not an inline `python -c`) so the wizard's existing
`Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/**)` allow rule covers it.
"""
import sys
from pathlib import Path

missing = [f for f in sys.argv[1:] if not Path(f).is_file()]
print("ok" if not missing else "missing: " + ", ".join(missing))
