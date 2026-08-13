"""Single source of truth for the `zotero-mcp-server` version floor.

The floor used to be written out in six places — `pyproject.toml`'s dev
pin, `check_zotero_mcp_version.FLOOR`, and four install-command strings in
`wizard.py`. Nothing kept them in agreement, so a floor bump could easily
leave the wizard telling users to install a version the plugin no longer
accepts (or the reverse).

`pyproject.toml` remains the *declarative* pin — `tests/unit/
test_zotero_mcp_sync.py`'s E3 regex-matches the literal there, and
`tests/unit/test_zotero_mcp_floor.py` asserts this module agrees with it.
Everything user-facing derives from the constants below.

Stdlib-only and import-free on purpose: `check_zotero_mcp_version.py` runs
as a bare `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/setup/...` subprocess under
the wizard's Bash allow rule, and must not pull in a dependency or pay an
import cost to answer a version question.

Bumping the floor: edit `FLOOR` and the `pyproject.toml` pin together, then
run `pytest tests/unit/test_zotero_mcp_floor.py`.
"""

from __future__ import annotations

# (major, minor). 0.9 is where upstream merged fifteen MCP tools into six
# and gated several groups behind ZOTERO_MCP_TOOLSETS (commit 2823c5a); an
# older install silently misses tool names this plugin's skills document.
FLOOR = (0, 9)

# The next major/minor, i.e. the exclusive upper bound of the pin.
CEILING = (FLOOR[0], FLOOR[1] + 1)

FLOOR_STR = ".".join(str(n) for n in FLOOR)
CEILING_STR = ".".join(str(n) for n in CEILING)

PACKAGE = "zotero-mcp-server"

# Extras the plugin depends on: `scite` powers the retraction-check step in
# systematic-review / zotero-operations; `semantic` enables semantic search.
EXTRAS = "scite,semantic"

#: The pyproject.toml dev-group pin, e.g. "zotero-mcp-server>=0.9,<0.10".
PIN = f"{PACKAGE}>={FLOOR_STR},<{CEILING_STR}"

#: What users install, extras included, e.g.
#: 'zotero-mcp-server[scite,semantic]>=0.9'.
REQUIREMENT = f"{PACKAGE}[{EXTRAS}]>={FLOOR_STR}"

UV_INSTALL_CMD = f'uv tool install "{REQUIREMENT}"'
PIP_INSTALL_CMD = f'pip install "{REQUIREMENT}"'


def is_below_floor(version: str) -> bool:
    """True when `version`'s (major, minor) sorts below `FLOOR`.

    Tolerates suffixed parts ("0.9.1rc2", "1.0.0.dev3") by reading the
    leading digits of each of the first two components and treating a
    non-numeric component as 0.
    """
    nums: list[int] = []
    for part in version.split(".")[:2]:
        digits = ""
        for ch in part:
            if not ch.isdigit():
                break
            digits += ch
        nums.append(int(digits) if digits else 0)
    while len(nums) < 2:
        nums.append(0)
    return (nums[0], nums[1]) < FLOOR
