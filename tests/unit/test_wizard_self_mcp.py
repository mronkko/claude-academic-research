"""The wizard registers the plugin's own MCP server for Antigravity and Codex.

Claude Code finds `mcp/server.py` through `mcpServers` in
`.claude-plugin/plugin.json`; Antigravity and Codex need it written into
their global config with an absolute path. These tests cover that write,
its idempotence, the refresh of a stale path after the plugin moves, and
that a user's own Codex config survives the edit. Codex support itself
is untested by policy — these check only the file the wizard writes.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
WIZARD = REPO / "scripts" / "setup" / "wizard.py"


@pytest.fixture
def wiz(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("wizard", WIZARD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["wizard"] = mod
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "AGY_MCP_CONFIG_PATH", tmp_path / "gemini" / "mcp_config.json")
    monkeypatch.setattr(mod, "CODEX_HOME", tmp_path / "codex")
    monkeypatch.setattr(mod, "CODEX_CONFIG_PATH", tmp_path / "codex" / "config.toml")
    return mod


def _yes(monkeypatch, answer: str = "") -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt="": answer)


def test_entry_matches_the_claude_code_registration(wiz) -> None:
    """One launch command for every host, only the root spelled differently."""
    manifest = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    claude = manifest["mcpServers"][wiz.SELF_MCP_NAME]
    entry = wiz._self_mcp_entry()
    assert entry["command"] == claude["command"]
    expected = [a.replace("${CLAUDE_PLUGIN_ROOT}", str(REPO)) for a in claude["args"]]
    assert [a.replace("\\", "/") for a in entry["args"]] == [
        a.replace("\\", "/") for a in expected
    ]
    assert Path(entry["args"][-1]).is_file()


def test_agy_registration_writes_and_is_idempotent(wiz, monkeypatch) -> None:
    path = wiz.AGY_MCP_CONFIG_PATH
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"mcpServers": {"zotero": {"command": "zotero-mcp"}}}),
                    encoding="utf-8")
    _yes(monkeypatch)
    assert wiz._register_self_agy(interactive=True) == "registered"
    servers = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]
    assert servers["academic-research"] == wiz._self_mcp_entry()
    assert servers["zotero"] == {"command": "zotero-mcp"}, "other entries kept"
    assert wiz._register_self_agy(interactive=True) == "current"


def test_agy_registration_refreshes_a_stale_path(wiz, monkeypatch) -> None:
    path = wiz.AGY_MCP_CONFIG_PATH
    path.parent.mkdir(parents=True)
    stale = {"command": "uv", "args": ["run", "/old/plugin/mcp/server.py"]}
    path.write_text(json.dumps({"mcpServers": {"academic-research": stale}}), encoding="utf-8")
    _yes(monkeypatch)
    assert wiz._register_self_agy(interactive=True) == "registered"
    assert json.loads(path.read_text(encoding="utf-8"))["mcpServers"][
        "academic-research"] == wiz._self_mcp_entry()


def test_agy_registration_respects_no_and_non_interactive(wiz, monkeypatch) -> None:
    assert wiz._register_self_agy(interactive=False) == "skipped"
    _yes(monkeypatch, "n")
    assert wiz._register_self_agy(interactive=True) == "skipped"
    assert not wiz.AGY_MCP_CONFIG_PATH.exists()


def test_codex_absent_means_nothing_written(wiz, monkeypatch) -> None:
    _yes(monkeypatch)
    assert wiz._register_self_codex(interactive=True) == "absent"
    assert not wiz.CODEX_HOME.exists()


def test_codex_registration_appends_and_keeps_user_content(wiz, monkeypatch) -> None:
    wiz.CODEX_HOME.mkdir()
    original = (
        '# my codex settings\nmodel = "o4"\n\n'
        '[mcp_servers.other]\ncommand = "other-mcp"\n'
    )
    wiz.CODEX_CONFIG_PATH.write_text(original, encoding="utf-8")
    _yes(monkeypatch)
    assert wiz._register_self_codex(interactive=True) == "registered"
    text = wiz.CODEX_CONFIG_PATH.read_text(encoding="utf-8")
    assert text.startswith(original.rstrip("\n")), "user's lines and comments kept"
    parsed = tomllib.loads(text)
    assert parsed["mcp_servers"]["academic-research"] == wiz._self_mcp_entry()
    assert parsed["mcp_servers"]["other"] == {"command": "other-mcp"}
    assert parsed["model"] == "o4"
    assert wiz._register_self_codex(interactive=True) == "current"
    assert wiz.CODEX_CONFIG_PATH.with_suffix(".toml.bak-wizard").is_file()


def test_codex_registration_replaces_a_stale_table(wiz, monkeypatch) -> None:
    wiz.CODEX_HOME.mkdir()
    wiz.CODEX_CONFIG_PATH.write_text(
        '[mcp_servers.academic-research]\ncommand = "uv"\nargs = ["old"]\n\n'
        '[mcp_servers.academic-research.env]\nX = "1"\n\n'
        '[mcp_servers.academic-research-other]\ncommand = "keep-me"\n',
        encoding="utf-8",
    )
    _yes(monkeypatch)
    assert wiz._register_self_codex(interactive=True) == "registered"
    parsed = tomllib.loads(wiz.CODEX_CONFIG_PATH.read_text(encoding="utf-8"))
    assert parsed["mcp_servers"]["academic-research"] == wiz._self_mcp_entry()
    assert parsed["mcp_servers"]["academic-research-other"] == {"command": "keep-me"}


def test_codex_inline_table_is_left_alone(wiz, monkeypatch, capsys) -> None:
    wiz.CODEX_HOME.mkdir()
    original = 'mcp_servers = { academic-research = { command = "x" } }\n'
    wiz.CODEX_CONFIG_PATH.write_text(original, encoding="utf-8")
    _yes(monkeypatch)
    assert wiz._register_self_codex(interactive=True) == "manual"
    assert wiz.CODEX_CONFIG_PATH.read_text(encoding="utf-8") == original
    assert "[mcp_servers.academic-research]" in capsys.readouterr().out


def test_codex_non_interactive_writes_nothing(wiz) -> None:
    wiz.CODEX_HOME.mkdir()
    assert wiz._register_self_codex(interactive=False) == "skipped"
    assert not wiz.CODEX_CONFIG_PATH.exists()
