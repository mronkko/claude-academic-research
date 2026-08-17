"""Smoke tests for the setup wizard.

The wizard is mostly interactive, but we can check the static pieces:
permission-pattern generation, the key schema, verify-function behaviour,
and module import.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

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
    # ANTHROPIC_API_KEY and GEMINI_API_KEY are now optional individually
    # but at least one must be provided.
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


def test_permission_categories_have_purpose_and_per_rule_explanations() -> None:
    """Every category has a purpose + skip_impact, and every rule has a
    one-line explanation. The wizard prints these in interactive mode
    so the user can audit each allow rule."""
    mod = _load()
    cats, _ = mod._permission_categories()
    assert len(cats) >= 4, "expected at least 4 permission categories"
    for cat in cats:
        assert cat.name, "category missing name"
        assert cat.purpose, f"{cat.name}: missing purpose"
        assert cat.skip_impact, f"{cat.name}: missing skip_impact"
        assert cat.rules, f"{cat.name}: no rules"
        for rule, purpose in cat.rules:
            assert rule, f"{cat.name}: empty rule string"
            assert purpose, f"{cat.name}: rule '{rule}' missing per-rule purpose"


def test_permission_categories_match_flat_list() -> None:
    """The legacy `_permission_patterns()` flat list must equal the
    concatenation of category rule lists — no rule lost or duplicated
    by the categorisation refactor."""
    mod = _load()
    flat_allow, _ = mod._permission_patterns()
    cats, _ = mod._permission_categories()
    cat_rules = [rule for cat in cats for rule, _ in cat.rules]
    assert flat_allow == cat_rules


def test_config_path_is_under_home() -> None:
    mod = _load()
    # Compare by Path parts rather than trailing-string match so the test
    # passes on Windows (where str(Path) uses backslashes).
    assert mod.CONFIG_PATH.parts[-3:] == (
        ".config", "academic-research", "config.toml",
    )


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


def test_verify_gemini_success(monkeypatch) -> None:
    mod = _load()
    monkeypatch.setattr(mod, "_http_json", lambda *a, **kw: (200, {"models": []}, ""))
    ok, _msg, _ = mod._verify_gemini("valid-gemini-key")
    assert ok


def test_verify_gemini_rejected(monkeypatch) -> None:
    mod = _load()
    monkeypatch.setattr(mod, "_http_json", lambda *a, **kw: (403, None, "403 Forbidden"))
    ok, msg, _ = mod._verify_gemini("bad-key")
    assert not ok
    assert "rejected" in msg.lower()



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
    # Config-only specs contribute no name; `test_which_specs_are_config_only`
    # below pins exactly which those are, so dropping them here cannot
    # quietly become a way to skip this guard.
    env_names = {k.env_var for k in mod.KEYS if k.env_var}
    expected = {
        "ZOTERO_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
        "WOS_API_KEY_EXTENDED", "WOS_API_KEY",
        "ELSEVIER_API_KEY", "SCOPUS_API_KEY",
        "SEMANTIC_SCHOLAR_API_KEY", "CROSSREF_MAILTO",
        "WILEY_TDM_TOKEN", "OPENALEX_API_KEY", "LIBRARY_OPENURL_BASE",
        # CORE (core.ac.uk) — repository-aggregated OA full text, last
        # in the PDF cascade.
        "CORE_API_KEY",
        # Not a credential: points the screening pipelines at an
        # Anthropic-compatible endpoint (local models — issue #1).
        "ANTHROPIC_BASE_URL",
        # The other providers in `core.providers`. Each is only
        # prompted for when it is the selected provider, but the name
        # must stay stable regardless — users set these in their shell.
        "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENROUTER_API_KEY",
        # The institutional gateway contributes nothing here on purpose:
        # both its settings are config-only (`env_var == ""`), because
        # any name this plugin invented would collide with whatever the
        # user already calls theirs. `test_config_only_specs_are_gateway`
        # below is what keeps that exemption honest.
        # Local providers: a URL is the whole configuration, there is
        # no key at all.
        "OLLAMA_BASE_URL", "LMSTUDIO_BASE_URL",
    }
    assert env_names == expected, f"env var schema drift: {env_names ^ expected}"


def test_which_specs_are_config_only() -> None:
    """Exactly the gateway's two settings may omit an environment variable.

    Omitting one is a real decision, not a default: it means the setting
    can only be reached through `config.toml`, and it exempts the spec
    from `test_env_var_names_match_user_convention`. The gateway earns
    that because any name the plugin invented would collide with the one
    its user already exports. Nothing else has that excuse — a new spec
    that forgets its `env_var` should fail here rather than silently
    become unreachable from a shell.
    """
    mod = _load()
    config_only = {
        (k.toml_section, k.toml_key) for k in mod.KEYS if not k.env_var
    }
    assert config_only == {("gateway", "api_key"), ("gateway", "base_url")}


def test_llm_key_specs_name_a_real_provider() -> None:
    """`llm_provider` on a KeySpec gates whether that key is asked for.

    A typo there would silently drop the question for a provider the
    user just selected, so the value must resolve in `core.providers`.
    """
    mod = _load()
    from core import providers

    unknown = {
        spec.env_var: spec.llm_provider
        for spec in mod.KEYS
        if spec.llm_provider and providers.get(spec.llm_provider) is None
    }
    assert not unknown, f"KeySpecs naming an unknown provider: {unknown}"


def test_every_provider_credential_has_a_key_spec() -> None:
    """Every provider in the registry can be configured through /setup.

    Without this, adding a provider to `core.providers` gives the user a
    menu entry and no way to supply its credential — the exact
    invisibility this change set out to remove.
    """
    mod = _load()
    from core import providers

    # Keyed on (section, key), not on the env var: a bring-your-own
    # provider declares no variable, and matching on `""` would make
    # this guard pass vacuously for exactly the provider that needs it
    # most — the one whose settings have no conventional name.
    asked = {(spec.toml_section, spec.toml_key) for spec in mod.KEYS}
    missing = []
    for spec in providers.PROVIDERS:
        section = providers.config_section(spec)
        if spec.local:
            # No key; the base URL is the whole configuration.
            wanted = [(section, "base_url")]
        elif spec.byo_endpoint:
            # Both halves, and neither has a default to fall back on.
            wanted = [(section, "api_key"), (section, "base_url")]
        else:
            wanted = [(section, "api_key")]
        for want in wanted:
            if want not in asked:
                missing.append(
                    f"{spec.name} (expected a KeySpec for "
                    f"[{want[0]}].{want[1]})"
                )
    assert not missing, (
        f"Providers with no KeySpec to configure them: {missing}. "
        f"Add one to wizard.py:KEYS (plus a live auth test)."
    )



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
    assert zotero_call == [
        "claude", "mcp", "add", "-s", "user", "zotero",
        "-e", "ZOTERO_MCP_TOOLSETS=libraries,search-admin,pdf-geometry,duplicates,scite",
        "--", "zotero-mcp",
    ]
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
    assert "zotero-mcp-server[scite,semantic]" in out
    assert "isn't on your PATH" in out


def test_zotero_install_includes_scite_semantic_extras() -> None:
    """R9: the wizard must install the [scite,semantic] extras so the
    Scite retraction-check (R5) and semantic search are available — base
    `zotero-mcp-server` ships neither."""
    mod = _load()
    zotero = next(s for s in mod.EXPECTED_MCP if s.name == "zotero")
    assert "zotero-mcp-server[scite,semantic]" in zotero.install_cmd
    # The note must explain what the extras buy, not just name them.
    assert "scite" in zotero.install_note.lower()
    assert "retraction" in zotero.install_note.lower()
    assert "semantic" in zotero.install_note.lower()
    # PyPI alternative must also carry the extras.
    assert "zotero-mcp-server[scite,semantic]" in zotero.install_note


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
    assert cmd == (
        "claude mcp add -s user zotero "
        "-e ZOTERO_MCP_TOOLSETS=libraries,search-admin,pdf-geometry,duplicates,scite "
        "-- zotero-mcp"
    )


# ---------------------------------------------------------------------------
# Antigravity (agy) MCP config support
# ---------------------------------------------------------------------------


def test_mcp_spec_to_agy_entry_simple_command() -> None:
    mod = _load()
    zotero = next(s for s in mod.EXPECTED_MCP if s.name == "zotero")
    assert mod._mcp_spec_to_agy_entry(zotero) == {
        "command": "zotero-mcp", "args": [],
        "env": {
            "ZOTERO_MCP_TOOLSETS": "libraries,search-admin,pdf-geometry,duplicates,scite",
        },
    }


def test_mcp_spec_to_agy_entry_multi_arg_command() -> None:
    mod = _load()
    semantic_scholar = next(
        s for s in mod.EXPECTED_MCP if s.name == "semantic-scholar"
    )
    assert mod._mcp_spec_to_agy_entry(semantic_scholar) == {
        "command": "npx", "args": ["-y", "aira-semanticscholar"],
    }


def test_mcp_spec_to_agy_entry_paper_search_long_args() -> None:
    mod = _load()
    paper_search = next(s for s in mod.EXPECTED_MCP if s.name == "paper-search")
    assert mod._mcp_spec_to_agy_entry(paper_search) == {
        "command": "uvx",
        "args": ["--from", "paper-search-mcp", "python", "-m",
                 "paper_search_mcp.server"],
    }


def test_mcp_spec_to_agy_entry_extracts_env_vars() -> None:
    mod = _load()
    spec = mod.McpServerSpec(
        name="example",
        purpose="Example server with an env var.",
        add_args=("-s", "user", "example", "-e", "FOO=bar", "--", "example-mcp"),
        homepage="https://example.invalid",
        install_cmd="",
        install_note="",
        tier=mod.MCP_TIER_OPTIONAL,
    )
    assert mod._mcp_spec_to_agy_entry(spec) == {
        "command": "example-mcp", "args": [], "env": {"FOO": "bar"},
    }


def test_check_agy_mcp_servers_missing_file_returns_empty(monkeypatch, tmp_path) -> None:
    mod = _load()
    monkeypatch.setattr(mod, "AGY_MCP_CONFIG_PATH", tmp_path / "mcp_config.json")
    assert mod._check_agy_mcp_servers() == {}


def test_check_agy_mcp_servers_reads_configured_entries(monkeypatch, tmp_path) -> None:
    mod = _load()
    config_path = tmp_path / "mcp_config.json"
    config_path.write_text(json.dumps({
        "mcpServers": {
            "zotero": {"command": "zotero-mcp", "args": []},
            "scopus": {"command": "scopus-mcp", "args": [], "disabled": True},
        }
    }), encoding="utf-8")
    monkeypatch.setattr(mod, "AGY_MCP_CONFIG_PATH", config_path)

    parsed = mod._check_agy_mcp_servers()
    assert parsed["zotero"] == mod.MCP_STATUS_CONNECTED
    assert parsed["scopus"] == mod.MCP_STATUS_MISSING


def test_check_agy_mcp_servers_malformed_json_fails_open(monkeypatch, tmp_path) -> None:
    mod = _load()
    config_path = tmp_path / "mcp_config.json"
    config_path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(mod, "AGY_MCP_CONFIG_PATH", config_path)
    assert mod._check_agy_mcp_servers() == {}


def test_load_agy_mcp_config_missing_file_returns_empty_dict(monkeypatch, tmp_path) -> None:
    mod = _load()
    monkeypatch.setattr(mod, "AGY_MCP_CONFIG_PATH", tmp_path / "mcp_config.json")
    assert mod._load_agy_mcp_config() == {}


def test_load_agy_mcp_config_backs_up_existing_file(monkeypatch, tmp_path) -> None:
    mod = _load()
    config_path = tmp_path / "mcp_config.json"
    config_path.write_text(json.dumps({"mcpServers": {"zotero": {"command": "zotero-mcp"}}}),
                            encoding="utf-8")
    monkeypatch.setattr(mod, "AGY_MCP_CONFIG_PATH", config_path)

    data = mod._load_agy_mcp_config()
    assert data == {"mcpServers": {"zotero": {"command": "zotero-mcp"}}}
    backup = config_path.with_suffix(".json.bak-wizard")
    assert backup.exists()


def test_load_agy_mcp_config_malformed_json_exits(monkeypatch, tmp_path) -> None:
    mod = _load()
    config_path = tmp_path / "mcp_config.json"
    config_path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(mod, "AGY_MCP_CONFIG_PATH", config_path)

    with pytest.raises(SystemExit) as exc_info:
        mod._load_agy_mcp_config()
    assert exc_info.value.code == 3


def test_write_agy_mcp_config_creates_parent_dirs(monkeypatch, tmp_path) -> None:
    mod = _load()
    config_path = tmp_path / "nested" / "config" / "mcp_config.json"
    monkeypatch.setattr(mod, "AGY_MCP_CONFIG_PATH", config_path)

    mod._write_agy_mcp_config({"mcpServers": {"zotero": {"command": "zotero-mcp", "args": []}}})

    written = json.loads(config_path.read_text(encoding="utf-8"))
    assert written == {"mcpServers": {"zotero": {"command": "zotero-mcp", "args": []}}}


def test_offer_register_agy_mcp_writes_config(monkeypatch, tmp_path) -> None:
    mod = _load()
    config_path = tmp_path / "mcp_config.json"
    monkeypatch.setattr(mod, "AGY_MCP_CONFIG_PATH", config_path)
    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: "y")

    registered, updated = mod._offer_register_agy_mcp(
        mod.EXPECTED_MCP, {}, interactive=True,
    )

    assert registered == len(mod.EXPECTED_MCP)
    assert all(updated[s.name] == mod.MCP_STATUS_CONNECTED for s in mod.EXPECTED_MCP)

    written = json.loads(config_path.read_text(encoding="utf-8"))
    assert written["mcpServers"]["zotero"] == {
        "command": "zotero-mcp", "args": [],
        "env": {
            "ZOTERO_MCP_TOOLSETS": "libraries,search-admin,pdf-geometry,duplicates,scite",
        },
    }
    assert written["mcpServers"]["semantic-scholar"] == {
        "command": "npx", "args": ["-y", "aira-semanticscholar"],
    }


def test_offer_register_agy_mcp_skips_when_already_connected(monkeypatch, tmp_path) -> None:
    mod = _load()
    config_path = tmp_path / "mcp_config.json"
    monkeypatch.setattr(mod, "AGY_MCP_CONFIG_PATH", config_path)

    def boom(*_a, **_kw):
        raise AssertionError("must not prompt when already connected")
    monkeypatch.setattr("builtins.input", boom)

    current = {s.name: mod.MCP_STATUS_CONNECTED for s in mod.EXPECTED_MCP}
    registered, updated = mod._offer_register_agy_mcp(
        mod.EXPECTED_MCP, current, interactive=True,
    )

    assert registered == 0
    assert updated == current
    assert not config_path.exists()


def test_offer_register_agy_mcp_respects_non_interactive(monkeypatch, tmp_path) -> None:
    mod = _load()
    config_path = tmp_path / "mcp_config.json"
    monkeypatch.setattr(mod, "AGY_MCP_CONFIG_PATH", config_path)

    def boom(*_a, **_kw):
        raise AssertionError("must not prompt in non-interactive mode")
    monkeypatch.setattr("builtins.input", boom)

    registered, updated = mod._offer_register_agy_mcp(
        mod.EXPECTED_MCP, {}, interactive=False,
    )

    assert registered == 0
    assert updated == {}
    assert not config_path.exists()


def test_offer_register_agy_mcp_preserves_unrelated_entries(monkeypatch, tmp_path) -> None:
    mod = _load()
    config_path = tmp_path / "mcp_config.json"
    config_path.write_text(json.dumps({
        "mcpServers": {"my-other-server": {"command": "my-other-mcp", "args": []}}
    }), encoding="utf-8")
    monkeypatch.setattr(mod, "AGY_MCP_CONFIG_PATH", config_path)
    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: "y")

    zotero = next(s for s in mod.EXPECTED_MCP if s.name == "zotero")
    mod._offer_register_agy_mcp((zotero,), {}, interactive=True)

    written = json.loads(config_path.read_text(encoding="utf-8"))
    assert written["mcpServers"]["my-other-server"] == {
        "command": "my-other-mcp", "args": [],
    }
    assert written["mcpServers"]["zotero"] == {
        "command": "zotero-mcp", "args": [],
        "env": {
            "ZOTERO_MCP_TOOLSETS": "libraries,search-admin,pdf-geometry,duplicates,scite",
        },
    }


def test_offer_register_agy_mcp_skipped_answer_does_not_write(monkeypatch, tmp_path) -> None:
    mod = _load()
    config_path = tmp_path / "mcp_config.json"
    monkeypatch.setattr(mod, "AGY_MCP_CONFIG_PATH", config_path)
    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: "n")

    zotero = next(s for s in mod.EXPECTED_MCP if s.name == "zotero")
    registered, updated = mod._offer_register_agy_mcp((zotero,), {}, interactive=True)

    assert registered == 0
    assert updated == {}
    assert not config_path.exists()


def test_agy_available_true_when_agy_home_exists(monkeypatch, tmp_path) -> None:
    mod = _load()
    agy_home = tmp_path / ".gemini"
    agy_home.mkdir()
    monkeypatch.setattr(mod, "AGY_HOME", agy_home)
    monkeypatch.setattr(mod.shutil, "which", lambda _name: None)

    assert mod._agy_available() is True


def test_agy_available_true_when_agy_binary_on_path(monkeypatch, tmp_path) -> None:
    mod = _load()
    monkeypatch.setattr(mod, "AGY_HOME", tmp_path / "does-not-exist")
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/local/bin/agy" if name == "agy" else None)

    assert mod._agy_available() is True


def test_agy_available_false_when_neither(monkeypatch, tmp_path) -> None:
    mod = _load()
    monkeypatch.setattr(mod, "AGY_HOME", tmp_path / "does-not-exist")
    monkeypatch.setattr(mod.shutil, "which", lambda _name: None)

    assert mod._agy_available() is False


def test_merge_mcp_status_connected_if_either_connected() -> None:
    mod = _load()
    claude_status = {"zotero": mod.MCP_STATUS_MISSING}
    agy_status = {"zotero": mod.MCP_STATUS_CONNECTED}

    merged = mod._merge_mcp_status(claude_status, agy_status)

    assert merged["zotero"] == mod.MCP_STATUS_CONNECTED


def test_merge_mcp_status_prefers_claude_status_when_neither_connected() -> None:
    mod = _load()
    claude_status = {"zotero": mod.MCP_STATUS_NEEDS_AUTH}
    agy_status = {"zotero": mod.MCP_STATUS_MISSING}

    merged = mod._merge_mcp_status(claude_status, agy_status)

    assert merged["zotero"] == mod.MCP_STATUS_NEEDS_AUTH


def test_merge_mcp_status_uses_agy_when_claude_missing_entry() -> None:
    mod = _load()
    claude_status = {}
    agy_status = {"zotero": mod.MCP_STATUS_CONNECTED}

    merged = mod._merge_mcp_status(claude_status, agy_status)

    assert merged["zotero"] == mod.MCP_STATUS_CONNECTED


def test_merge_mcp_status_includes_all_keys_from_both_maps() -> None:
    mod = _load()
    claude_status = {"zotero": mod.MCP_STATUS_CONNECTED}
    agy_status = {"scopus": mod.MCP_STATUS_CONNECTED}

    merged = mod._merge_mcp_status(claude_status, agy_status)

    assert merged == {
        "zotero": mod.MCP_STATUS_CONNECTED,
        "scopus": mod.MCP_STATUS_CONNECTED,
    }


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


# ---------------------------------------------------------------------------
# LLM provider selection
# ---------------------------------------------------------------------------


def test_provider_default_falls_back_to_registry_default(monkeypatch) -> None:
    mod = _load()
    from core import providers

    monkeypatch.delenv(providers.PROVIDER_ENV, raising=False)
    assert mod._provider_default({}) == providers.DEFAULT_PROVIDER


def test_provider_default_reads_existing_config(monkeypatch) -> None:
    mod = _load()
    from core import providers

    monkeypatch.delenv(providers.PROVIDER_ENV, raising=False)
    assert mod._provider_default({"llm": {"provider": "ollama"}}) == "ollama"


def test_provider_default_env_beats_config(monkeypatch) -> None:
    """Same precedence as llm_provider.resolve_provider — env wins."""
    mod = _load()
    from core import providers

    monkeypatch.setenv(providers.PROVIDER_ENV, "openai")
    assert mod._provider_default({"llm": {"provider": "ollama"}}) == "openai"


def test_provider_default_ignores_an_unknown_name(monkeypatch) -> None:
    """A typo in the config must not be offered back as the default."""
    mod = _load()
    from core import providers

    monkeypatch.delenv(providers.PROVIDER_ENV, raising=False)
    assert mod._provider_default(
        {"llm": {"provider": "anthorpic"}},
    ) == providers.DEFAULT_PROVIDER


def test_choose_provider_non_interactive_never_prompts(monkeypatch) -> None:
    mod = _load()
    from core import providers

    def boom(*_a, **_kw):
        raise AssertionError("must not prompt in non-interactive mode")

    monkeypatch.setattr("builtins.input", boom)
    monkeypatch.setenv(providers.PROVIDER_ENV, "lmstudio")
    assert mod._choose_provider(False, {}) == "lmstudio"


def test_choose_provider_accepts_a_number(monkeypatch, capsys) -> None:
    mod = _load()
    from core import providers

    monkeypatch.delenv(providers.PROVIDER_ENV, raising=False)
    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: "3")
    assert mod._choose_provider(True, {}) == providers.PROVIDERS[2].name


def test_choose_provider_accepts_a_name(monkeypatch) -> None:
    mod = _load()
    from core import providers

    monkeypatch.delenv(providers.PROVIDER_ENV, raising=False)
    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: "OpenRouter")
    assert mod._choose_provider(True, {}) == "openrouter"


def test_choose_provider_empty_input_keeps_the_default(monkeypatch) -> None:
    mod = _load()
    from core import providers

    monkeypatch.delenv(providers.PROVIDER_ENV, raising=False)
    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: "")
    assert mod._choose_provider(True, {"llm": {"provider": "google"}}) == "google"


def test_choose_provider_reprompts_on_a_bad_answer(monkeypatch, capsys) -> None:
    mod = _load()
    from core import providers

    monkeypatch.delenv(providers.PROVIDER_ENV, raising=False)
    answers = iter(["chatgtp", "99", "ollama"])
    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: next(answers))
    assert mod._choose_provider(True, {}) == "ollama"
    assert "Not one of" in capsys.readouterr().out


def test_choose_provider_menu_lists_every_registered_provider(
    monkeypatch, capsys,
) -> None:
    """The menu is generated from the registry, so a provider added there
    is offered here without a second edit."""
    mod = _load()
    from core import providers

    monkeypatch.delenv(providers.PROVIDER_ENV, raising=False)
    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: "")
    mod._choose_provider(True, {})
    out = capsys.readouterr().out
    for spec in providers.PROVIDERS:
        assert spec.label in out


# ---------------------------------------------------------------------------
# Provider-conditional key collection
# ---------------------------------------------------------------------------


def _collect(mod, monkeypatch, provider: str, existing: dict) -> dict:
    """Run _collect_keys non-interactively over a fake existing config."""
    monkeypatch.setattr(mod, "_load_existing_config", lambda: existing)
    for spec in mod.KEYS:
        monkeypatch.delenv(spec.env_var, raising=False)
    return mod._collect_keys(False, False, provider)


def test_collect_keys_skips_other_providers_credentials(monkeypatch) -> None:
    """An OpenAI user is not asked for an Anthropic key."""
    mod = _load()
    asked: list[str] = []
    real_prompt = mod._prompt_key

    def spy(spec, existing, interactive, verify):
        asked.append(spec.env_var)
        return real_prompt(spec, existing, interactive, verify)

    monkeypatch.setattr(mod, "_prompt_key", spy)
    _collect(mod, monkeypatch, "openai", {"zotero": {"api_key": "z"}})

    assert "OPENAI_API_KEY" in asked
    assert "ANTHROPIC_API_KEY" not in asked
    assert "GEMINI_API_KEY" not in asked
    # Non-LLM keys are asked for whatever the provider is.
    assert "ZOTERO_API_KEY" in asked
    assert "SCOPUS_API_KEY" in asked


def test_collect_keys_preserves_the_unselected_providers_key(monkeypatch) -> None:
    """Switching provider must not delete the old key from config.toml —
    _write_config rewrites the file from these values alone."""
    mod = _load()
    values = _collect(mod, monkeypatch, "openai", {
        "zotero": {"api_key": "z"},
        "anthropic": {"api_key": "sk-ant-old", "base_url": "http://x:1234"},
        "gemini": {"api_key": "gem-old"},
    })
    assert values["anthropic"]["api_key"] == "sk-ant-old"
    assert values["anthropic"]["base_url"] == "http://x:1234"
    assert values["gemini"]["api_key"] == "gem-old"


def test_collect_keys_warns_when_the_selected_provider_has_no_key(
    monkeypatch, capsys,
) -> None:
    mod = _load()
    _collect(mod, monkeypatch, "openai", {"zotero": {"api_key": "z"}})
    out = capsys.readouterr().out
    assert "OPENAI_API_KEY" in out
    assert "WARNING" in out


def test_collect_keys_is_silent_for_a_local_provider(monkeypatch, capsys) -> None:
    """Local providers have no credential; warning about a missing key
    scolds a user whose setup is complete."""
    mod = _load()
    _collect(mod, monkeypatch, "ollama", {"zotero": {"api_key": "z"}})
    assert "WARNING" not in capsys.readouterr().out


def test_collect_keys_accepts_anthropic_base_url_without_a_key(
    monkeypatch, capsys,
) -> None:
    """Issue #1: a local Anthropic-compatible endpoint ignores the key,
    so a base URL alone is a working configuration."""
    mod = _load()
    _collect(mod, monkeypatch, "anthropic", {
        "zotero": {"api_key": "z"},
        "anthropic": {"base_url": "http://localhost:1234"},
    })
    assert "WARNING" not in capsys.readouterr().out


def test_collect_keys_no_warning_when_the_key_is_present(monkeypatch, capsys) -> None:
    mod = _load()
    _collect(mod, monkeypatch, "google", {
        "zotero": {"api_key": "z"},
        "gemini": {"api_key": "gem"},
    })
    assert "WARNING" not in capsys.readouterr().out


def test_llm_credential_hint_points_at_the_key_when_only_a_url_is_set() -> None:
    """An OpenAI-compatible gateway still needs *a* key value: the SDK
    requires one even when the endpoint ignores it."""
    mod = _load()
    ok, hint = mod._llm_credential_present(
        "openai", {}, {"openai": {"base_url": "http://localhost:8000"}},
    )
    assert not ok
    assert "OPENAI_API_KEY" in hint
    assert "OPENAI_BASE_URL" in hint
