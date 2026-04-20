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
