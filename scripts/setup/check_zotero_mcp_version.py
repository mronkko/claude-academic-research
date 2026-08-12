#!/usr/bin/env python3
"""Pre-flight version report for the installed zotero-mcp-server package.

Prints 'zotero-mcp-server: <version>' (or 'zotero-mcp-server: not
installed'), plus a WARNING line if the installed version is below this
plugin's floor. The wizard installs zotero-mcp-server unpinned from PyPI
(`uv tool install "zotero-mcp-server[scite,semantic]>=0.9"`), so a stale
install from before the tool-surface rename (upstream commit 2823c5a, which
merged fifteen tools into six and gated several groups behind
ZOTERO_MCP_TOOLSETS) would silently miss the tool names this plugin's
skills document — this is the check that surfaces that mismatch before a
skill hits a "tool not found" error mid-task.

Kept as a script (not an inline `python -c`) so the wizard's existing
`Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/**)` allow rule covers it — no
per-session permission prompt at skill load time.
"""
from __future__ import annotations

# Keep in sync with the `zotero-mcp-server>=0.9,<0.10` pin in pyproject.toml
# and the wizard's install_cmd floor (scripts/setup/wizard.py).
FLOOR = (0, 9)


def _parse_major_minor(version: str) -> tuple[int, int]:
    parts = version.split(".")[:2]
    nums = []
    for part in parts:
        digits = ""
        for ch in part:
            if not ch.isdigit():
                break
            digits += ch
        nums.append(int(digits) if digits else 0)
    while len(nums) < 2:
        nums.append(0)
    return (nums[0], nums[1])


def main() -> int:
    try:
        from zotero_mcp import __version__
    except ImportError:
        print("zotero-mcp-server: not installed")
        return 0

    print(f"zotero-mcp-server: {__version__}")
    if _parse_major_minor(__version__) < FLOOR:
        floor_str = ".".join(str(n) for n in FLOOR)
        print(
            f"WARNING: zotero-mcp-server {__version__} is below this "
            f"plugin's floor ({floor_str}). Tools were renamed and gated "
            "behind ZOTERO_MCP_TOOLSETS in 0.9 — an older install will "
            "silently miss tool names the skills document. Upgrade: "
            'uv tool install "zotero-mcp-server[scite,semantic]>=0.9" '
            "--force (or the PyPI alt: pip install "
            '"zotero-mcp-server[scite,semantic]>=0.9" --upgrade).'
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
