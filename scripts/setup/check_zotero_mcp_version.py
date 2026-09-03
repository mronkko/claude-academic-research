#!/usr/bin/env python3
"""Pre-flight version report for the installed zotero-mcp-server package.

Prints 'zotero-mcp-server: <version>' (or 'zotero-mcp-server: not
installed'), plus a WARNING line if the installed version is below this
plugin's floor. The wizard installs zotero-mcp-server unpinned from PyPI,
so a stale install from before the tool-surface rename (upstream commit
2823c5a, which merged fifteen tools into six and gated several groups
behind ZOTERO_MCP_TOOLSETS) would silently miss the tool names this
plugin's skills document — this is the check that surfaces that mismatch
before a skill hits a "tool not found" error mid-task.

The floor itself and the install commands live in `zotero_mcp_floor.py`,
shared with `wizard.py`, so a bump is a one-file edit.

Kept as a script (not an inline `python -c`) so the wizard's existing
`Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/**)` allow rule covers it — no
per-session permission prompt at skill load time.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Running this file as a script already puts its directory on sys.path[0],
# but not when it is imported by path from a test. Be explicit.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from zotero_mcp_floor import (  # noqa: E402
    FLOOR_STR,
    PIP_INSTALL_CMD,
    UV_INSTALL_CMD,
    is_below_floor,
)


def main() -> int:
    try:
        from zotero_mcp import __version__
    except ImportError:
        print("zotero-mcp-server: not installed")
        return 0

    print(f"zotero-mcp-server: {__version__}")
    if is_below_floor(__version__):
        print(
            f"WARNING: zotero-mcp-server {__version__} is below this "
            f"plugin's floor ({FLOOR_STR}). Tools were renamed and gated "
            "behind ZOTERO_MCP_TOOLSETS in 0.9 — an older install will "
            "silently miss tool names the skills document. Upgrade: "
            f"{UV_INSTALL_CMD} --force (or the PyPI alt: "
            f"{PIP_INSTALL_CMD} --upgrade)."
        )
    return 0


if __name__ == "__main__":
    # Windows takes stdout's encoding from the locale when output is
    # redirected — normally cp1252, which cannot encode the arrows and em
    # dashes printed below. Inline rather than via scripts/core/console.py
    # because this script must stand on its own.
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
