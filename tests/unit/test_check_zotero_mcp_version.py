"""check_zotero_mcp_version.py reports the server the MCP config launches.

It used to `import zotero_mcp` in whatever interpreter ran it, so from the
plugin's dev `.venv` it reported 0.9.1 while the server Claude Code launched
(`~/.claude.json` -> `~/.local/bin/zotero-mcp` -> a uv tool venv) was 0.14.1.
These tests build fake config files and fake tool venvs under the per-test
HOME that `tests/conftest.py` already provides, and check that the script
follows the configured command to its install — and never echoes the `env`
or `args` of an entry, which can carry API keys.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "setup" / "check_zotero_mcp_version.py"

SECRET = "sk-THIS-MUST-NEVER-BE-PRINTED"


@pytest.fixture
def mod():
    spec = importlib.util.spec_from_file_location("check_zotero_mcp_version", SCRIPT)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    sys.modules["check_zotero_mcp_version"] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "project"
    p.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(p))
    # Nothing on PATH unless a test puts it there.
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    return p


def _tool_venv(root: Path, version: str) -> Path:
    """A uv-tool-shaped venv holding zotero-mcp-server `version`; returns
    its `bin/zotero-mcp` entry point (never executed)."""
    bindir = root / ("Scripts" if sys.platform == "win32" else "bin")
    bindir.mkdir(parents=True)
    python = bindir / "python3"
    python.write_text("", encoding="utf-8")
    exe = bindir / "zotero-mcp"
    exe.write_text(f"#!{python}\nfrom zotero_mcp.cli import main\nmain()\n",
                   encoding="utf-8")
    dist = (root / "lib" / "python3.12" / "site-packages"
            / f"zotero_mcp_server-{version}.dist-info")
    dist.mkdir(parents=True)
    (dist / "METADATA").write_text(
        f"Metadata-Version: 2.5\nName: zotero-mcp-server\nVersion: {version}\n\nBody\n",
        encoding="utf-8")
    return exe


def _entry(command: str, args: list[str] | None = None) -> dict:
    return {"command": command, "args": args or ["serve", "--api-key", SECRET],
            "env": {"ZOTERO_API_KEY": SECRET}}


def _write_claude_json(data: dict) -> None:
    (Path.home() / ".claude.json").write_text(json.dumps(data), encoding="utf-8")


def _out(mod, project: Path) -> str:
    return "\n".join(mod.report(project))


def test_user_scope_entry_resolves_to_its_tool_venv(mod, project, tmp_path) -> None:
    exe = _tool_venv(tmp_path / "tools" / "zotero-mcp-server", "0.14.1")
    _write_claude_json({"mcpServers": {"zotero": _entry(str(exe))}})
    out = _out(mod, project)
    assert out.startswith("zotero-mcp-server: 0.14.1 — Claude Code (user scope")
    assert "package metadata in" in out
    assert "WARNING" not in out


def test_symlinked_launcher_is_followed_to_the_venv(mod, project, tmp_path) -> None:
    exe = _tool_venv(tmp_path / "tools" / "zotero-mcp-server", "0.14.1")
    link = Path.home() / ".local" / "bin" / "zotero-mcp"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(exe)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    _write_claude_json({"mcpServers": {"zotero": _entry("~/.local/bin/zotero-mcp")}})
    assert "zotero-mcp-server: 0.14.1 — Claude Code" in _out(mod, project)


def test_shebang_interpreter_venv_is_used_for_a_copied_launcher(
        mod, project, tmp_path) -> None:
    """Windows-style uv installs copy the launcher out of the venv; the
    shebang still names the venv's interpreter."""
    exe = _tool_venv(tmp_path / "tools" / "zotero-mcp-server", "0.12.4")
    copied = Path.home() / ".local" / "bin" / "zotero-mcp"
    copied.parent.mkdir(parents=True)
    copied.write_text(exe.read_text(encoding="utf-8"), encoding="utf-8")
    _write_claude_json({"mcpServers": {"zotero": _entry(str(copied))}})
    assert "zotero-mcp-server: 0.12.4 — Claude Code" in _out(mod, project)


def test_local_scope_beats_project_and_user_scope(mod, project, tmp_path) -> None:
    local = _tool_venv(tmp_path / "local", "0.13.0")
    proj = _tool_venv(tmp_path / "proj", "0.12.0")
    user = _tool_venv(tmp_path / "user", "0.14.1")
    (project / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"zotero": _entry(str(proj))}}), encoding="utf-8")
    _write_claude_json({
        "mcpServers": {"zotero": _entry(str(user))},
        "projects": {str(project): {"mcpServers": {"zotero": _entry(str(local))}}},
    })
    first = mod.report(project)[0]
    assert first.startswith("zotero-mcp-server: 0.13.0 — Claude Code (local scope")


def test_project_mcp_json_beats_user_scope(mod, project, tmp_path) -> None:
    proj = _tool_venv(tmp_path / "proj", "0.12.0")
    user = _tool_venv(tmp_path / "user", "0.14.1")
    (project / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"zotero": _entry(str(proj))}}), encoding="utf-8")
    _write_claude_json({"mcpServers": {"zotero": _entry(str(user))}})
    assert mod.report(project)[0].startswith(
        "zotero-mcp-server: 0.12.0 — Claude Code (project scope")


def test_entry_found_by_command_when_key_is_not_zotero(mod, project, tmp_path) -> None:
    exe = _tool_venv(tmp_path / "t", "0.14.1")
    _write_claude_json({"mcpServers": {"other": {"command": "node"},
                                       "zotero-mcp": _entry(str(exe))}})
    assert "mcpServers.zotero-mcp" in _out(mod, project)


def test_below_floor_warns_and_names_the_host(mod, project, tmp_path) -> None:
    exe = _tool_venv(tmp_path / "t", "0.9.1")
    _write_claude_json({"mcpServers": {"zotero": _entry(str(exe))}})
    out = _out(mod, project)
    assert "zotero-mcp-server: 0.9.1" in out
    assert "WARNING: the zotero-mcp-server Claude Code launches is 0.9.1" in out
    assert "uv tool install" in out


def test_antigravity_is_reported_separately(mod, project, tmp_path) -> None:
    cc = _tool_venv(tmp_path / "cc", "0.14.1")
    agy = _tool_venv(tmp_path / "agy", "0.10.0")
    _write_claude_json({"mcpServers": {"zotero": _entry(str(cc))}})
    cfg = Path.home() / ".gemini" / "config" / "mcp_config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {"zotero": _entry(str(agy))}}),
                   encoding="utf-8")
    lines = mod.report(project)
    assert lines[0].startswith("zotero-mcp-server: 0.14.1 — Claude Code")
    assert lines[1].startswith("zotero-mcp-server: 0.10.0 — Antigravity")
    assert any(line.startswith("WARNING: the zotero-mcp-server Antigravity")
               for line in lines)


def test_secrets_in_env_and_args_are_never_printed(mod, project, tmp_path, capsys) -> None:
    exe = _tool_venv(tmp_path / "t", "0.14.1")
    _write_claude_json({"mcpServers": {"zotero": _entry(str(exe))}})
    assert mod.main() == 0
    out = capsys.readouterr().out
    assert SECRET not in out
    assert "--api-key" not in out


def test_uvx_launch_is_reported_as_unresolved(mod, project) -> None:
    _write_claude_json({"mcpServers": {"zotero": _entry(
        "uvx", ["--from", "zotero-mcp-server", "zotero-mcp", "serve"])}})
    out = _out(mod, project)
    assert out.startswith("zotero-mcp-server: unknown — Claude Code")
    assert "picks the version at launch" in out
    assert "WARNING" not in out


def test_missing_command_is_reported_as_unresolved(mod, project, tmp_path) -> None:
    _write_claude_json({"mcpServers": {"zotero": _entry(str(tmp_path / "gone"))}})
    out = _out(mod, project)
    assert out.startswith("zotero-mcp-server: unknown — Claude Code")
    assert "not found" in out


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell launcher")
def test_falls_back_to_the_version_subcommand(mod, project, tmp_path) -> None:
    exe = tmp_path / "elsewhere" / "zotero-mcp"
    exe.parent.mkdir()
    exe.write_text('#!/bin/sh\n[ "$1" = version ] && echo "Zotero MCP v0.12.2"\n',
                   encoding="utf-8")
    os.chmod(exe, 0o755)
    _write_claude_json({"mcpServers": {"zotero": _entry(str(exe))}})
    out = _out(mod, project)
    assert "zotero-mcp-server: 0.12.2 — Claude Code" in out
    assert "`zotero-mcp version`" in out


def test_no_config_and_nothing_on_path(mod, project) -> None:
    assert mod.report(project) == [
        f"zotero-mcp-server: not installed — no Zotero MCP server registered "
        f"(checked ~/.claude.json, {project / '.mcp.json'}, "
        f"~/.gemini/config/mcp_config.json)"
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="PATH lookup needs a PATHEXT suffix")
def test_unregistered_but_on_path(mod, project, tmp_path, monkeypatch) -> None:
    exe = _tool_venv(tmp_path / "t", "0.14.1")
    os.chmod(exe, 0o755)
    monkeypatch.setenv("PATH", str(exe.parent))
    out = _out(mod, project)
    assert out.startswith("zotero-mcp-server: 0.14.1 — no Zotero MCP server registered")


def test_malformed_config_is_ignored(mod, project) -> None:
    (Path.home() / ".claude.json").write_text("{not json", encoding="utf-8")
    assert "not installed" in _out(mod, project)
