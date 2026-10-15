#!/usr/bin/env python3
"""Pre-flight probe for academic-research skills.

Prints 'configured' if ~/.config/academic-research/config.toml exists,
'NOT CONFIGURED' otherwise. Skills call this before running so they can
hand off to the setup wizard on first use.

Kept as a shipped script rather than an inline `python -c`: one
auditable command that behaves the same on Windows, macOS and Linux.
"""
from pathlib import Path

config = Path.home() / ".config" / "academic-research" / "config.toml"
print("configured" if config.is_file() else "NOT CONFIGURED")
