"""Guard: the zotero-mcp-server version floor has one definition.

The floor used to appear as six independent literals — the `pyproject.toml`
dev pin, `check_zotero_mcp_version.FLOOR`, and four install-command strings
in `wizard.py`. `scripts/setup/zotero_mcp_floor.py` is now the source for
everything user-facing; `pyproject.toml` keeps the declarative pin because
`test_zotero_mcp_sync.py`'s E3 regex-matches the literal there. These tests
assert the two agree and that no install string has drifted back to a
hard-coded version.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SETUP_DIR = REPO / "scripts" / "setup"
WIZARD = SETUP_DIR / "wizard.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def floor():
    return _load("zotero_mcp_floor", SETUP_DIR / "zotero_mcp_floor.py")


def test_floor_matches_the_pyproject_pin(floor) -> None:
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert f'"{floor.PIN}"' in pyproject, (
        f"zotero_mcp_floor.PIN is {floor.PIN!r} but pyproject.toml does not "
        f"contain that literal. Bump FLOOR and the pyproject pin together."
    )


def test_derived_strings_are_built_from_the_floor(floor) -> None:
    assert floor.FLOOR_STR == "0.9" or floor.FLOOR_STR.startswith(
        f"{floor.FLOOR[0]}."
    )
    assert floor.REQUIREMENT.endswith(f">={floor.FLOOR_STR}")
    assert floor.REQUIREMENT in floor.UV_INSTALL_CMD
    assert floor.REQUIREMENT in floor.PIP_INSTALL_CMD


@pytest.mark.parametrize(
    ("version", "below"),
    [
        ("0.8.9", True),
        ("0.6.2", True),
        ("0.9.0", False),
        ("0.9.1", False),
        ("0.10.0", False),
        ("1.0.0", False),
        # Suffixed / non-numeric components must not crash or mis-sort.
        ("0.9.0rc1", False),
        ("0.8rc1", True),
        ("1.0.0.dev3", False),
    ],
)
def test_is_below_floor(floor, version: str, below: bool) -> None:
    assert floor.is_below_floor(version) is below


def test_wizard_has_no_hardcoded_zotero_mcp_version() -> None:
    """Every user-facing install string must come from the shared module."""
    text = WIZARD.read_text(encoding="utf-8")
    offenders = [
        f"wizard.py:{lineno}: {line.strip()}"
        for lineno, line in enumerate(text.splitlines(), start=1)
        if re.search(r"zotero-mcp-server\[[^\]]*\]>=", line)
    ]
    assert not offenders, (
        "wizard.py hard-codes a zotero-mcp-server requirement string; import "
        "UV_INSTALL_CMD / PIP_INSTALL_CMD from zotero_mcp_floor instead:\n  "
        + "\n  ".join(offenders)
    )


def test_check_script_has_no_hardcoded_floor() -> None:
    text = (SETUP_DIR / "check_zotero_mcp_version.py").read_text(encoding="utf-8")
    assert "FLOOR = (" not in text, (
        "check_zotero_mcp_version.py must import FLOOR from zotero_mcp_floor, "
        "not redefine it."
    )
    assert re.search(r"from zotero_mcp_floor import", text)
