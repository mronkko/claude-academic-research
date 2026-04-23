#!/usr/bin/env python3
"""Pre-flight probe for academic-research skills.

Prints 'configured' if ~/.config/academic-research/config.toml exists,
'NOT CONFIGURED' otherwise. Skills call this before running so they can
hand off to the setup wizard on first use.

Kept as a script (not an inline `python -c`) so the wizard's existing
`Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/**)` allow rule covers it —
no per-session permission prompt at skill load time.
"""
from pathlib import Path

config = Path.home() / ".config" / "academic-research" / "config.toml"
print("configured" if config.is_file() else "NOT CONFIGURED")
