"""Smoke tests for the setup wizard.

The wizard is mostly interactive, but we can check the static pieces:
permission-pattern generation, the key schema, verify-function behaviour,
and module import.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

WIZARD = Path(__file__).resolve().parents[2] / "scripts" / "setup" / "wizard.py"


def _load():
    import sys
    spec = importlib.util.spec_from_file_location("wizard", WIZARD)
    assert spec is not None and spec.loader is not None, f"cannot load {WIZARD}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["wizard"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Static schema tests
# ---------------------------------------------------------------------------


def test_wizard_imports() -> None:
    _load()


def test_key_schema_has_required_core_keys() -> None:
    mod = _load()
    required_env = {k.env_var for k in mod.KEYS if k.required}
    assert "ZOTERO_API_KEY" in required_env
    assert "ANTHROPIC_API_KEY" in required_env
    # ZOTERO_GROUP deliberately NOT a key in the global config — group IDs
    # are per-project.
    assert "ZOTERO_GROUP" not in {k.env_var for k in mod.KEYS}


def test_every_key_has_full_documentation() -> None:
    mod = _load()
    for k in mod.KEYS:
        assert k.what and len(k.what) > 40, f"{k.env_var} missing what"
        assert k.used_by and len(k.used_by) > 10, f"{k.env_var} missing used_by"
        assert k.impact and len(k.impact) > 20, f"{k.env_var} missing impact"
        assert k.where and len(k.where) > 10, f"{k.env_var} missing where"


def test_no_bare_acronyms_in_user_facing_text() -> None:
    """User-facing prose should spell out acronyms the first time they appear."""
    mod = _load()
    for k in mod.KEYS:
        text = f"{k.label} {k.what} {k.used_by} {k.impact} {k.where}"
        # TDM = Text and Data Mining. Must be spelled out in at least the
        # first occurrence per key.
        if "TDM" in text:
            assert "Text and Data Mining" in text or "text and data mining" in text, (
                f"{k.env_var}: uses 'TDM' without spelling it out at least once"
            )
        # "S2" was a prior alias for Semantic Scholar — banned entirely.
        assert " S2 " not in f" {text} ", f"{k.env_var}: uses 'S2' acronym"


def test_every_key_has_a_verify_callable() -> None:
    mod = _load()
    for k in mod.KEYS:
        assert callable(k.verify), f"{k.env_var} has no verify callable"


def test_permission_patterns_cover_plugin_scripts() -> None:
    mod = _load()
    allow, deny = mod._permission_patterns()
    assert any("uv run" in p and "scripts/**" in p for p in allow)
    assert any("python3" in p and "scripts/**" in p for p in allow)
    assert any("playwright install chromium" in p for p in allow)
    assert any("config.toml" in p for p in deny)


def test_permission_patterns_deny_covers_read_and_shell() -> None:
    mod = _load()
    _, deny = mod._permission_patterns()
    assert any(p.startswith("Read(") for p in deny), "deny list missing Read()"
    assert any("cat " in p for p in deny), "deny list missing `cat` Bash"


def test_config_path_is_under_home() -> None:
    mod = _load()
    assert str(mod.CONFIG_PATH).endswith(".config/academic-research/config.toml")


# ---------------------------------------------------------------------------
# Verify-function tests (mocked HTTP)
# ---------------------------------------------------------------------------


def test_verify_zotero_success(monkeypatch) -> None:
    mod = _load()
    monkeypatch.setattr(mod, "_http_json", lambda *a, **kw: (200, {
        "userID": 475425,
        "username": "mronkko",
        "access": {"groups": {"40758": {}, "52014": {}}},
    }, ""))
    ok, msg, extras = mod._verify_zotero("fake-key")
    assert ok
    assert "userID=475425" in msg
    assert extras["user_id"] == "475425"
    assert "40758" in extras["accessible_group_ids"]


def test_verify_zotero_rejected(monkeypatch) -> None:
    mod = _load()
    monkeypatch.setattr(mod, "_http_json", lambda *a, **kw: (403, None, "403 Forbidden"))
    ok, msg, _ = mod._verify_zotero("bad-key")
    assert not ok
    assert "rejected" in msg.lower() or "403" in msg


def test_verify_zotero_network_failure_permits_save(monkeypatch) -> None:
    """Offline or transient failure should return ok=False but not error out —
    the wizard saves the key anyway so the user isn't blocked."""
    mod = _load()
    monkeypatch.setattr(mod, "_http_json", lambda *a, **kw: (0, None, "Connection refused"))
    ok, msg, _ = mod._verify_zotero("any-key")
    assert not ok
    assert "saved anyway" in msg or "reach" in msg.lower()


def test_verify_anthropic_success(monkeypatch) -> None:
    mod = _load()
    monkeypatch.setattr(mod, "_http_json", lambda *a, **kw: (200, {"data": []}, ""))
    ok, _msg, _ = mod._verify_anthropic("sk-ant-...")
    assert ok


def test_verify_anthropic_401(monkeypatch) -> None:
    mod = _load()
    monkeypatch.setattr(mod, "_http_json", lambda *a, **kw: (401, None, "401 Unauthorized"))
    ok, msg, _ = mod._verify_anthropic("bad-key")
    assert not ok
    assert "401" in msg or "reject" in msg.lower()


def test_verify_crossref_mailto_valid() -> None:
    mod = _load()
    ok, _, _ = mod._verify_crossref_mailto("user@example.com")
    assert ok


def test_verify_crossref_mailto_invalid() -> None:
    mod = _load()
    ok, _, _ = mod._verify_crossref_mailto("not an email")
    assert not ok


# ---------------------------------------------------------------------------
# _prompt_key env/config precedence (non-interactive path)
# ---------------------------------------------------------------------------


def test_non_interactive_env_wins(monkeypatch) -> None:
    mod = _load()
    spec = next(k for k in mod.KEYS if k.env_var == "ZOTERO_API_KEY")
    monkeypatch.setenv("ZOTERO_API_KEY", "env-value")
    value, _extras = mod._prompt_key(spec, "config-value",
                                     interactive=False, verify=False)
    assert value == "env-value"


def test_non_interactive_falls_back_to_config(monkeypatch) -> None:
    mod = _load()
    spec = next(k for k in mod.KEYS if k.env_var == "ZOTERO_API_KEY")
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    value, _extras = mod._prompt_key(spec, "config-value",
                                     interactive=False, verify=False)
    assert value == "config-value"


def test_env_var_names_match_user_convention() -> None:
    """The wizard's env var names must match what the user sets in their shell
    profile. If a name changes here, projects' existing shells break."""
    mod = _load()
    env_names = {k.env_var for k in mod.KEYS}
    expected = {
        "ZOTERO_API_KEY", "ANTHROPIC_API_KEY",
        "WOS_API_KEY_EXTENDED", "WOS_API_KEY",
        "ELSEVIER_API_KEY", "SCOPUS_API_KEY",
        "SEMANTIC_SCHOLAR_API_KEY", "CROSSREF_MAILTO",
        "WILEY_TDM_TOKEN", "OPENALEX_API_KEY",
    }
    assert env_names == expected, f"env var schema drift: {env_names ^ expected}"


def test_non_interactive_with_verify_collects_extras(monkeypatch) -> None:
    mod = _load()
    spec = next(k for k in mod.KEYS if k.env_var == "ZOTERO_API_KEY")
    monkeypatch.setenv("ZOTERO_API_KEY", "valid-key")
    monkeypatch.setattr(mod, "_http_json", lambda *a, **kw: (200, {
        "userID": 42, "username": "u", "access": {"groups": {"1": {}}},
    }, ""))
    value, extras = mod._prompt_key(spec, None, interactive=False, verify=True)
    assert value == "valid-key"
    assert extras.get("user_id") == "42"


# ---------------------------------------------------------------------------
# MCP server schema and parser tests
# ---------------------------------------------------------------------------


_MCP_LIST_SAMPLE = """Checking MCP server health…

claude.ai Google Calendar: https://calendarmcp.googleapis.com/mcp/v1 - ! Needs authentication
zotero: zotero-mcp  - ✓ Connected
semantic-scholar: npx -y aira-semanticscholar - ✓ Connected
openalex: npx -y openalex-research-mcp - ✗ Failed
scopus: scopus-mcp  - ! Needs authentication
paper-search: uvx --from paper-search-mcp python -m paper_search_mcp.server - ✓ Connected
"""


def test_check_mcp_servers_parses_connected_status() -> None:
    mod = _load()
    parsed = mod._parse_mcp_list(_MCP_LIST_SAMPLE)
    assert parsed["zotero"] == mod.MCP_STATUS_CONNECTED
    assert parsed["semantic-scholar"] == mod.MCP_STATUS_CONNECTED
    assert parsed["openalex"] == mod.MCP_STATUS_FAILED
    assert parsed["scopus"] == mod.MCP_STATUS_NEEDS_AUTH
    assert parsed["paper-search"] == mod.MCP_STATUS_CONNECTED


def test_check_mcp_servers_ignores_claude_ai_builtin_servers() -> None:
    mod = _load()
    parsed = mod._parse_mcp_list(_MCP_LIST_SAMPLE)
    # "claude.ai Google Calendar" has whitespace in the name and must be
    # skipped — otherwise it would shadow legitimate entries or crash
    # callers that index by EXPECTED_MCP names.
    assert "claude.ai" not in parsed
    assert "Google" not in parsed


def test_expected_mcp_contains_five_servers_in_correct_tiers() -> None:
    mod = _load()
    by_name = {s.name: s for s in mod.EXPECTED_MCP}
    assert set(by_name) == {
        "zotero", "scopus", "semantic-scholar", "openalex", "paper-search",
    }
    assert by_name["zotero"].tier == mod.MCP_TIER_REQUIRED
    assert by_name["scopus"].tier == mod.MCP_TIER_SEARCH_DB
    assert by_name["semantic-scholar"].tier == mod.MCP_TIER_SEARCH_DB
    assert by_name["openalex"].tier == mod.MCP_TIER_SEARCH_DB
    assert by_name["paper-search"].tier == mod.MCP_TIER_OPTIONAL


def test_every_mcp_spec_has_homepage_and_install_guidance() -> None:
    """Analogue of test_every_key_has_full_documentation: every entry must
    give the user actionable install info, not just a name."""
    mod = _load()
    for spec in mod.EXPECTED_MCP:
        assert spec.homepage.startswith("https://"), f"{spec.name}: bad homepage"
        assert spec.purpose and len(spec.purpose) > 20, f"{spec.name}: missing purpose"
        # Either an explicit install_cmd, or an install_note that explains
        # the auto-install path (npx/uvx).
        has_cmd = bool(spec.install_cmd)
        auto_note = "npx" in spec.install_note.lower() or "uvx" in spec.install_note.lower()
        assert has_cmd or auto_note, (
            f"{spec.name}: must have install_cmd or auto-install note"
        )


def test_mcp_summary_warns_when_no_search_database_connected() -> None:
    mod = _load()
    current = {"zotero": mod.MCP_STATUS_CONNECTED}  # all three search-dbs missing
    zotero_missing, all_search_missing = mod._print_mcp_summary(current)
    assert not zotero_missing
    assert all_search_missing


def test_mcp_summary_does_not_warn_when_one_search_database_connected() -> None:
    mod = _load()
    current = {
        "zotero": mod.MCP_STATUS_CONNECTED,
        "semantic-scholar": mod.MCP_STATUS_CONNECTED,
    }
    zotero_missing, all_search_missing = mod._print_mcp_summary(current)
    assert not zotero_missing
    assert not all_search_missing


def test_mcp_summary_flags_zotero_missing() -> None:
    mod = _load()
    current = {
        "scopus": mod.MCP_STATUS_CONNECTED,
        "semantic-scholar": mod.MCP_STATUS_CONNECTED,
        "openalex": mod.MCP_STATUS_CONNECTED,
        "paper-search": mod.MCP_STATUS_CONNECTED,
    }
    zotero_missing, all_search_missing = mod._print_mcp_summary(current)
    assert zotero_missing
    assert not all_search_missing


def test_offer_register_mcp_runs_claude_mcp_add(monkeypatch) -> None:
    """Simulate user typing 'y' at the prompt; assert subprocess gets the
    full `claude mcp add ...` argv from EXPECTED_MCP."""
    mod = _load()
    captured: list[list[str]] = []

    def fake_run(args, **_kw):
        captured.append(list(args))
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(mod.shutil, "which", lambda _x: "/usr/local/bin/claude")
    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: "y")

    current: dict[str, str] = {}  # nothing registered
    registered, updated = mod._offer_register_mcp(
        mod.EXPECTED_MCP, current, interactive=True,
    )
    assert registered == len(mod.EXPECTED_MCP)
    # Each spec produced one `claude mcp add ...` call.
    add_calls = [c for c in captured if c[:3] == ["claude", "mcp", "add"]]
    assert len(add_calls) == len(mod.EXPECTED_MCP)
    zotero_call = next(c for c in add_calls if "zotero" in c)
    assert zotero_call == ["claude", "mcp", "add", "-s", "user", "zotero",
                           "--", "zotero-mcp"]
    assert all(updated[s.name] == mod.MCP_STATUS_CONNECTED for s in mod.EXPECTED_MCP)


def test_offer_register_mcp_skips_when_already_connected(monkeypatch) -> None:
    mod = _load()
    called = False

    def fake_run(*_a, **_kw):
        nonlocal called
        called = True
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(mod.shutil, "which", lambda _x: "/usr/local/bin/claude")
    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: "y")

    current = {s.name: mod.MCP_STATUS_CONNECTED for s in mod.EXPECTED_MCP}
    registered, _ = mod._offer_register_mcp(
        mod.EXPECTED_MCP, current, interactive=True,
    )
    assert registered == 0
    assert not called


def test_offer_register_mcp_respects_non_interactive(monkeypatch) -> None:
    mod = _load()
    called = False

    def fake_run(*_a, **_kw):
        nonlocal called
        called = True
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(mod.shutil, "which", lambda _x: "/usr/local/bin/claude")
    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)

    registered, updated = mod._offer_register_mcp(
        mod.EXPECTED_MCP, {}, interactive=False,
    )
    assert registered == 0
    assert not called
    # Map is returned unchanged.
    assert updated == {}


def test_offer_register_mcp_prints_install_hint_on_missing_binary(
    monkeypatch, capsys
) -> None:
    mod = _load()

    def fake_run(_args, **_kw):
        # Simulate `claude mcp add -- zotero-mcp` failing because the
        # binary isn't on PATH.
        class R:
            returncode = 1
            stdout = ""
            stderr = "zotero-mcp: command not found"
        return R()

    monkeypatch.setattr(mod.shutil, "which", lambda _x: "/usr/local/bin/claude")
    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: "y")

    zotero = next(s for s in mod.EXPECTED_MCP if s.name == "zotero")
    registered, _ = mod._offer_register_mcp((zotero,), {}, interactive=True)
    out = capsys.readouterr().out
    assert registered == 0
    assert "uv tool install zotero-mcp-server" in out
    assert "isn't on your PATH" in out


def test_offer_register_mcp_no_claude_cli_is_no_op(monkeypatch) -> None:
    """Fail-open: if `claude` is not on PATH, the function returns
    (0, current) without any subprocess calls or prompts."""
    mod = _load()
    monkeypatch.setattr(mod.shutil, "which", lambda _x: None)

    def boom(*_a, **_kw):
        raise AssertionError("subprocess.run must not be called when claude CLI is missing")

    import subprocess
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr("builtins.input",
                        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("no prompt")))

    registered, updated = mod._offer_register_mcp(
        mod.EXPECTED_MCP, {}, interactive=True,
    )
    assert registered == 0
    assert updated == {}


def test_format_register_command_is_copy_pasteable() -> None:
    mod = _load()
    zotero = next(s for s in mod.EXPECTED_MCP if s.name == "zotero")
    cmd = mod._format_register_command(zotero)
    assert cmd == "claude mcp add -s user zotero -- zotero-mcp"


# ---------------------------------------------------------------------------
# Zotero local API probe
# ---------------------------------------------------------------------------


def test_zotero_local_probe_ok(monkeypatch) -> None:
    """HTTP 200 from localhost:23119/api/ → ok status."""
    mod = _load()
    monkeypatch.setattr(mod, "_http_json", lambda *a, **kw: (200, {}, ""))
    status, msg = mod._check_zotero_local()
    assert status == mod.ZOTERO_LOCAL_STATUS_OK
    assert "localhost:23119" in msg


def test_zotero_local_probe_not_running(monkeypatch) -> None:
    """Connection refused → not_running status."""
    mod = _load()
    monkeypatch.setattr(
        mod, "_http_json",
        lambda *a, **kw: (0, None, "Connection refused"),
    )
    status, msg = mod._check_zotero_local()
    assert status == mod.ZOTERO_LOCAL_STATUS_NOT_RUNNING
    assert "refused" in msg.lower()


def test_zotero_local_probe_http_error(monkeypatch) -> None:
    """Non-200 HTTP (e.g. some proxy is serving a page at :23119) → not_running."""
    mod = _load()
    monkeypatch.setattr(mod, "_http_json", lambda *a, **kw: (404, None, "404 Not Found"))
    status, _ = mod._check_zotero_local()
    assert status == mod.ZOTERO_LOCAL_STATUS_NOT_RUNNING


def test_zotero_local_help_mentions_settings_path(capsys) -> None:
    """The help text must tell the user exactly where to flip the toggle."""
    mod = _load()
    mod._print_zotero_local_help()
    out = capsys.readouterr().out
    assert "Settings" in out or "Preferences" in out
    assert "Advanced" in out
    assert "Allow other applications" in out
    assert "zotero.org" in out   # link to download Zotero


# ---------------------------------------------------------------------------
# Better BibTeX probe
# ---------------------------------------------------------------------------


def test_zotero_bbt_probe_ok_on_200(monkeypatch) -> None:
    """HTTP 200 from /better-bibtex/json-rpc → ok status."""
    mod = _load()
    monkeypatch.setattr(mod, "_http_json", lambda *a, **kw: (200, {}, ""))
    status, msg = mod._check_zotero_bbt()
    assert status == mod.ZOTERO_BBT_STATUS_OK
    assert "Better BibTeX" in msg


def test_zotero_bbt_probe_ok_on_method_not_allowed(monkeypatch) -> None:
    """405/400 on GET just means the endpoint exists but expects POST —
    that's still a BBT-is-installed signal."""
    mod = _load()
    monkeypatch.setattr(mod, "_http_json", lambda *a, **kw: (405, None, "405 Method Not Allowed"))
    status, _ = mod._check_zotero_bbt()
    assert status == mod.ZOTERO_BBT_STATUS_OK


def test_zotero_bbt_probe_missing_on_404(monkeypatch) -> None:
    """Zotero running but BBT not installed → 404 on /better-bibtex/*."""
    mod = _load()
    monkeypatch.setattr(mod, "_http_json", lambda *a, **kw: (404, None, "404 Not Found"))
    status, msg = mod._check_zotero_bbt()
    assert status == mod.ZOTERO_BBT_STATUS_MISSING
    assert "Better BibTeX" in msg


def test_zotero_bbt_probe_unreachable_on_connection_failure(monkeypatch) -> None:
    """Status 0 from _http_json = Zotero not reachable at all."""
    mod = _load()
    monkeypatch.setattr(
        mod, "_http_json",
        lambda *a, **kw: (0, None, "Connection refused"),
    )
    status, msg = mod._check_zotero_bbt()
    assert status == mod.ZOTERO_BBT_STATUS_UNREACHABLE
    assert "refused" in msg.lower()


def test_zotero_bbt_help_mentions_xpi_install_path(capsys) -> None:
    """The help text must tell the user where to get the .xpi and how to
    install it in Zotero."""
    mod = _load()
    mod._print_zotero_bbt_help()
    out = capsys.readouterr().out
    assert ".xpi" in out
    assert "Tools" in out and "Add-ons" in out   # install path in Zotero
    assert "retorquere/zotero-better-bibtex" in out  # release URL
    assert "grounded-citations" in out           # *why* it matters
