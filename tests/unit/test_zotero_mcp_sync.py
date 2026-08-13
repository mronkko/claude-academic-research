"""Guard against zotero-mcp's MCP tool surface drifting out from under this
plugin's skills, wizard permission rules, and CLI references.

zotero-mcp 0.9.0 merged/renamed fifteen tools into six and gated several
groups behind ``ZOTERO_MCP_TOOLSETS`` (upstream commit ``2823c5a``, "cut the
MCP tool surface from 62 tools/~23k tokens to 37/~13.8k"). Nothing in this
plugin's test suite would have caught that rename landing: the skills and
``wizard.py`` kept naming tools that no longer resolved, and FastMCP silently
ignores unknown names in ``enable``/``disable`` rather than raising. This
module is the guard that would have caught it, mirroring the pattern
zotero-mcp uses on itself in its own ``tests/test_toolsets.py`` +
``validate_toolsets()``.

Authoritative source is the *installed* ``zotero-mcp-server`` package
(pinned ``>=0.9,<0.10`` in ``pyproject.toml``'s dev group) — specifically its
live FastMCP tool registry, ``toolsets`` module, and CLI parser. This
deliberately does NOT parse ``@mcp.tool`` decorators out of source: that
would only reproduce the class of drift this guard exists to catch.
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
WIZARD = REPO / "scripts" / "setup" / "wizard.py"

TOOL_REF_RE = re.compile(r"mcp__zotero__([a-zA-Z_]+)")

# (root, glob) pairs to scan for `mcp__zotero__<name>` references. Mirrors
# E1's brief: skills/**/*.md, scripts/**/*.py, templates/**, excluding logs/
# (logs/ is gitignored session transcripts, not under any of these roots).
SCAN_ROOTS: tuple[tuple[Path, str], ...] = (
    (REPO / "skills", "**/*.md"),
    (REPO / "scripts", "**/*.py"),
    (REPO / "templates", "**/*"),
)


def _load_wizard():
    spec = importlib.util.spec_from_file_location("wizard", WIZARD)
    assert spec is not None and spec.loader is not None, f"cannot load {WIZARD}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["wizard"] = mod
    spec.loader.exec_module(mod)
    return mod


def _referenced_tool_names() -> dict[str, list[str]]:
    """Every ``mcp__zotero__<name>`` referenced under the scan roots, mapped
    to the ``file:line`` locations referencing it (for actionable failures)."""
    hits: dict[str, list[str]] = {}
    for root, pattern in SCAN_ROOTS:
        for path in sorted(root.glob(pattern)):
            if not path.is_file() or "logs" in path.parts:
                continue
            # `templates/**/*` is an unfiltered glob, so anything non-text
            # that lands under a scan root would otherwise blow up the whole
            # module with a UnicodeDecodeError instead of reporting on tool
            # names. Bytecode caches are the realistic case: importing a
            # template in another test drops one into `templates/__pycache__/`.
            if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                for m in TOOL_REF_RE.finditer(line):
                    hits.setdefault(m.group(1), []).append(
                        f"{path.relative_to(REPO)}:{lineno}"
                    )
    return hits


def _live_tool_names(*, all_toolsets: bool = True) -> set[str]:
    """Tool names FastMCP actually registers, per ``zotero_mcp.server.mcp``."""
    from zotero_mcp.server import mcp
    from zotero_mcp.toolsets import optional_tool_names

    if all_toolsets:
        mcp.enable(names=optional_tool_names())
    return {t.name for t in asyncio.run(mcp.list_tools())}


@pytest.fixture(autouse=True)
def _restore_zotero_toolsets():
    """Tests in this module flip ``zotero_mcp.server.mcp``'s enabled-tool
    state (a process-global FastMCP instance). Reset to "everything on"
    after each test so state doesn't leak between them — same convention
    zotero-mcp's own ``tests/test_toolsets.py`` uses in its ``finally``
    blocks."""
    yield
    try:
        from zotero_mcp.server import mcp
        from zotero_mcp.toolsets import apply_toolsets
    except ImportError:
        return
    apply_toolsets(mcp, raw="all", transport="streamable-http")


# ---------------------------------------------------------------------------
# E1 — referenced tools exist in the live registry
# ---------------------------------------------------------------------------


def test_e1_referenced_tools_exist_in_registry() -> None:
    referenced = _referenced_tool_names()
    live = _live_tool_names(all_toolsets=True)

    missing = {name: locs for name, locs in referenced.items() if name not in live}
    assert not missing, (
        f"skills/scripts/templates reference zotero MCP tool name(s) that "
        f"don't exist in the installed zotero-mcp-server registry "
        f"({len(live)} tools known): "
        + "; ".join(f"{name} ({', '.join(locs)})" for name, locs in sorted(missing.items()))
    )


# ---------------------------------------------------------------------------
# E2 — referenced tools are reachable under the wizard's configured profile
# ---------------------------------------------------------------------------


def _zotero_mcp_server_spec(wizard):
    for spec in wizard.EXPECTED_MCP:
        if spec.name == "zotero":
            return spec
    raise AssertionError("no 'zotero' entry in wizard.EXPECTED_MCP")


def _zotero_toolsets_env_value(spec) -> str | None:
    """Pull the ``ZOTERO_MCP_TOOLSETS`` value out of ``add_args``' ``-e
    NAME=value`` pairs — the shape ``claude mcp add -e ...`` expects."""
    args = list(spec.add_args)
    for i, arg in enumerate(args):
        if arg in ("-e", "--env") and i + 1 < len(args):
            key, _, value = args[i + 1].partition("=")
            if key == "ZOTERO_MCP_TOOLSETS":
                return value
    return None


def test_e2_configured_toolsets_are_real_groups() -> None:
    """Catches typos FastMCP would otherwise ignore silently: every group
    named in the wizard's ZOTERO_MCP_TOOLSETS spec must be a real key of
    toolsets.TOOLSETS."""
    from zotero_mcp.toolsets import TOOLSETS

    wizard = _load_wizard()
    spec = _zotero_mcp_server_spec(wizard)
    raw = _zotero_toolsets_env_value(spec)
    assert raw, (
        "wizard.py's zotero McpServerSpec.add_args does not set "
        "ZOTERO_MCP_TOOLSETS, so only toolsets.DEFAULT_ON ships — "
        "scite/duplicates (and any other opt-in group the skills document) "
        "stay unreachable."
    )
    named = {tok.lstrip("-") for tok in raw.replace(",", " ").split()}
    unknown = named - set(TOOLSETS) - {"all", "none"}
    assert not unknown, (
        f"wizard.py's ZOTERO_MCP_TOOLSETS names unknown toolset(s) {sorted(unknown)}; "
        f"valid names: {sorted(TOOLSETS)}"
    )


def test_e2_referenced_tools_are_enabled_under_wizard_profile() -> None:
    """Existing-but-disabled is invisible to E1: a tool can be a real,
    registered name and still never appear in a session because its
    toolset isn't in ZOTERO_MCP_TOOLSETS. Apply the wizard's exact spec
    (stdio transport — that's what `claude mcp add -- zotero-mcp` runs)
    and check every referenced tool actually shows up."""
    from zotero_mcp.server import mcp
    from zotero_mcp.toolsets import apply_toolsets

    wizard = _load_wizard()
    spec = _zotero_mcp_server_spec(wizard)
    raw = _zotero_toolsets_env_value(spec)

    apply_toolsets(mcp, raw=raw, transport="stdio")
    reachable = {t.name for t in asyncio.run(mcp.list_tools())}

    referenced = _referenced_tool_names()
    unreachable = {name: locs for name, locs in referenced.items() if name not in reachable}
    assert not unreachable, (
        "Tool(s) referenced in skills/scripts/templates exist in "
        "zotero-mcp-server but are not enabled under the wizard's "
        f"ZOTERO_MCP_TOOLSETS={raw!r} profile (stdio transport): "
        + "; ".join(f"{name} ({', '.join(locs)})" for name, locs in sorted(unreachable.items()))
    )


# ---------------------------------------------------------------------------
# E3 — version pinning is the tripwire, not a straitjacket
# ---------------------------------------------------------------------------


def test_e3_dev_dependency_is_pinned_below_next_major() -> None:
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'"zotero-mcp-server>=0\.9,<0\.10"', pyproject), (
        "pyproject.toml's [dependency-groups] dev must pin "
        "zotero-mcp-server>=0.9,<0.10. Unpinned, an unrelated upstream "
        "release would redden this test module on every PR instead of "
        "surfacing as its own named signal on the weekly drift workflow."
    )


def test_e3_weekly_drift_workflow_exists() -> None:
    workflow = REPO / ".github" / "workflows" / "zotero_mcp_drift.yml"
    assert workflow.exists(), (
        "Missing the scheduled weekly workflow that installs "
        "zotero-mcp-server unpinned and runs only this module (E3) — "
        "without it, drift has no lead time before it reddens someone's "
        "unrelated PR."
    )
    text = workflow.read_text(encoding="utf-8")
    assert "schedule" in text, f"{workflow} has no schedule trigger"
    assert "test_zotero_mcp_sync" in text, (
        f"{workflow} does not appear to run this module"
    )
    assert re.search(r"pip install[^\n]*zotero-mcp-server(?!>=|<|==)", text), (
        f"{workflow} should install zotero-mcp-server unpinned, not with "
        "the dev-group version floor"
    )


# ---------------------------------------------------------------------------
# E4 — wizard permission allowlist coverage
# ---------------------------------------------------------------------------


def test_e4_wizard_allowlist_tools_exist() -> None:
    wizard = _load_wizard()
    categories, _deny = wizard._permission_categories()
    live = _live_tool_names(all_toolsets=True)

    missing = []
    for cat in categories:
        for pattern, purpose in cat.rules:
            if not pattern.startswith("mcp__zotero__"):
                continue
            name = pattern[len("mcp__zotero__"):]
            if name.endswith("*"):
                continue  # wildcard rule (e.g. a future mcp__zotero__*), not a tool name
            if name not in live:
                missing.append(f"{pattern!r} in category {cat.name!r} ({purpose})")
    assert not missing, (
        "wizard.py's permission allow list references zotero MCP tool(s) "
        f"that no longer exist: {missing}"
    )


# ---------------------------------------------------------------------------
# E5 — zotero-cli subcommand references
# ---------------------------------------------------------------------------

# Matches the subcommand token immediately after `zotero-cli` inside an
# inline-code span, e.g. "zotero-cli edit <key> ..." -> "edit".
_CLI_SPAN_RE = re.compile(r"^zotero-cli\s+([a-zA-Z][a-zA-Z-]*)")
# Matches the subcommand token in a wizard.py Bash() permission rule, e.g.
# "Bash(zotero-cli duplicates find:*)" -> "duplicates".
_CLI_BASH_RULE_RE = re.compile(r"Bash\(zotero-cli\s+([a-zA-Z][a-zA-Z-]*)")


def _referenced_cli_subcommands() -> set[str]:
    names: set[str] = set()
    for path in sorted((REPO / "skills").glob("**/*.md")):
        # Markdown word-wraps prose across lines; collapse whitespace runs
        # so an inline-code span split across a line break still matches.
        text = re.sub(r"\s+", " ", path.read_text(encoding="utf-8"))
        for span in re.findall(r"`([^`]+)`", text):
            m = _CLI_SPAN_RE.match(span)
            if m:
                names.add(m.group(1))
    names.update(_CLI_BASH_RULE_RE.findall(WIZARD.read_text(encoding="utf-8")))
    return names


def test_e5_cli_subcommands_exist() -> None:
    from zotero_mcp.cli_standalone import build_parser

    parser = build_parser()
    command_action = next(
        a for a in parser._subparsers._group_actions if a.dest == "command"
    )
    known = set(command_action.choices)

    referenced = _referenced_cli_subcommands()
    missing = referenced - known
    assert not missing, (
        f"zotero-cli subcommand(s) referenced in skills/ or wizard.py no "
        f"longer exist in zotero_mcp.cli_standalone.build_parser(): "
        f"{sorted(missing)}. Known subcommands: {sorted(known)}"
    )
