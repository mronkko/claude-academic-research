"""Guard: the MCP server's tools stay in step with the scripts they run.

`mcp/tool_registry.py` maps each tool parameter to a command-line flag of
an existing script. Nothing ties the two together at runtime — the server
deliberately never imports pipeline code — so a renamed or removed flag
would surface only as an argparse error in a user's session. This test
reads each target script's argparse calls from its source (AST, no
import, no execution) and fails when:

- a tool's target script does not exist;
- a parameter, map choice or fixed token names a flag the script does not
  define, or a positional it does not take;
- a parameter's enum offers a value the script's `choices=` refuses;
- a description does not start with its effect class;
- the server or registry imports anything outside the standard library.

Flags a script gets from a shared helper (`zotero_io.add_library_args`,
`tag_prefix.add_argument`) are followed into that helper's module.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MCP_DIR = REPO / "mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import tool_registry as reg  # noqa: E402


def _add_argument_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"):
            yield node


def _flags_in(tree: ast.AST) -> tuple[set[str], set[str], dict[str, set]]:
    """(option strings, positional names, flag -> literal choices)."""
    options: set[str] = set()
    positionals: set[str] = set()
    choices: dict[str, set] = {}
    for call in _add_argument_calls(tree):
        names = [a.value for a in call.args
                 if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        if not names:
            continue
        if names[0].startswith("-"):
            options.update(names)
        else:
            positionals.add(names[0])
        for kw in call.keywords:
            if kw.arg == "choices":
                try:
                    allowed = set(ast.literal_eval(kw.value))
                except ValueError:
                    continue
                for n in names:
                    choices[n] = allowed
    return options, positionals, choices


def _helper_trees(tree: ast.AST, script_dir: Path) -> list[ast.AST]:
    """Bodies of `module.function(...)` helpers that live beside the script
    and themselves call add_argument."""
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)):
            continue
        module = script_dir / f"{node.func.value.id}.py"
        if not module.is_file():
            continue
        mod_tree = ast.parse(module.read_text(encoding="utf-8"))
        for fn in ast.walk(mod_tree):
            if isinstance(fn, ast.FunctionDef) and fn.name == node.func.attr:
                found.append(fn)
    return found


def script_cli(script: Path) -> tuple[set[str], set[str], dict[str, set]]:
    tree = ast.parse(script.read_text(encoding="utf-8"))
    options, positionals, choices = _flags_in(tree)
    for helper in _helper_trees(tree, script.parent):
        o, p, c = _flags_in(helper)
        options |= o
        positionals |= p
        choices.update(c)
    return options, positionals, choices


TOOL_IDS = [t.name for t in reg.TOOLS]
SCRIPT_TOOLS = [t for t in reg.TOOLS if not t.internal]
SCRIPT_TOOL_IDS = [t.name for t in SCRIPT_TOOLS]
JOB_TOOL_IDS = [t.name for t in reg.JOB_TOOLS]


@pytest.mark.parametrize("tool", reg.TOOLS, ids=TOOL_IDS)
def test_target_scripts_exist(tool: reg.Tool) -> None:
    for script in tool.scripts():
        assert (REPO / script).is_file(), f"{tool.name}: {script} does not exist"


@pytest.mark.parametrize("tool", SCRIPT_TOOLS, ids=SCRIPT_TOOL_IDS)
def test_every_parameter_maps_to_a_real_flag(tool: reg.Tool) -> None:
    options, positionals, choices = script_cli(REPO / tool.script)
    for token in tool.fixed:
        if token.startswith("-"):
            assert token in options, f"{tool.name}: fixed {token} not in {tool.script}"
    for p in tool.params:
        if p.kind == "positional":
            assert p.flag in positionals, (
                f"{tool.name}.{p.name}: {tool.script} takes no positional {p.flag!r}"
            )
        elif p.kind == "map":
            for value, tokens in p.choices.items():
                for t in tokens:
                    if t.startswith("-"):
                        assert t in options, (
                            f"{tool.name}.{p.name}={value}: {t} not in {tool.script}"
                        )
        else:
            assert p.flag in options, (
                f"{tool.name}.{p.name}: {p.flag} is not a flag of {tool.script}"
            )
            if p.enum and p.flag in choices:
                extra = set(p.enum) - choices[p.flag]
                assert not extra, f"{tool.name}.{p.name}: {tool.script} refuses {extra}"


@pytest.mark.parametrize("tool", reg.JOB_TOOLS, ids=JOB_TOOL_IDS)
def test_job_scripts_take_the_flags_the_server_appends(tool: reg.Tool) -> None:
    """The server adds `--progress-json` to every job's argv, and
    `--control-file` to those whose script can ask a question; a script
    without them would die on argparse the moment the job started."""
    options, _, _ = script_cli(REPO / tool.script)
    assert "--progress-json" in options, f"{tool.script} lacks --progress-json"
    if tool.control:
        assert "--control-file" in options, f"{tool.script} lacks --control-file"


def test_control_flag_matches_the_script() -> None:
    """A script with --control-file but a tool without `control=True`
    would ask its question on a TTY nobody is at."""
    for tool in reg.JOB_TOOLS:
        assert tool.job and not tool.internal
        options, _, _ = script_cli(REPO / tool.script)
        assert tool.control == ("--control-file" in options), tool.name
    assert all(t.internal and not t.job for t in reg.JOB_CONTROL_TOOLS)


@pytest.mark.parametrize("tool", reg.JOB_TOOLS, ids=JOB_TOOL_IDS)
def test_job_artifacts_are_project_relative(tool: reg.Tool) -> None:
    for rel in tool.artifacts:
        assert not Path(rel).is_absolute() and ".." not in Path(rel).parts


def test_internal_tools_declare_no_flags() -> None:
    for tool in reg.JOB_CONTROL_TOOLS:
        assert tool.scripts() == ()
        assert all(p.flag == "" for p in tool.params), tool.name


def test_scanner_follows_shared_helpers() -> None:
    """The scan must see `--group` etc. through `zotero_io.add_library_args`,
    or every library parameter above would pass or fail for the wrong reason."""
    options, _, _ = script_cli(REPO / "scripts" / "pipelines" / "export_coded_includes.py")
    assert {"--group", "--user", "--remote", "--tag-prefix", "--collection"} <= options


@pytest.mark.parametrize("tool", reg.TOOLS, ids=TOOL_IDS)
def test_description_starts_with_effect_class(tool: reg.Tool) -> None:
    assert tool.effect in reg.EFFECT_CLASSES
    assert tool.full_description().startswith(f"[{tool.effect}] ")
    if tool.effect == reg.READ_ONLY:
        assert tool.read_only and not tool.destructive


def test_server_is_stdlib_only() -> None:
    local = {p.stem for p in MCP_DIR.glob("*.py")}
    for path in MCP_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots = [node.module.split(".")[0]]
            else:
                continue
            for root in roots:
                assert root in sys.stdlib_module_names or root in local, (
                    f"{path.name} imports {root!r}: the server must stay stdlib-only "
                    "and must not import pipeline code"
                )


def test_claude_code_registration_points_at_the_server() -> None:
    manifest = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    entry = manifest["mcpServers"]["academic-research"]
    assert entry["command"] == "uv"
    assert entry["args"][-1] == "${CLAUDE_PLUGIN_ROOT}/mcp/server.py"
    assert "--no-project" in entry["args"]
    assert (REPO / "mcp" / "server.py").is_file()
