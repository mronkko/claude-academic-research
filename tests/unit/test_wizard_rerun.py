"""End-to-end wizard runs against a scratch HOME.

These drive `wizard.main()` non-interactively with every network and
subprocess probe stubbed, so they check what the wizard *writes* — the
part a re-run can damage — rather than what it asks.

Every test clears the API-key variables from the environment first. The
wizard's `--non-interactive` mode reads keys from the environment and
writes them to config.toml, so a developer's real keys would otherwise
end up in the scratch file (and make assertions environment-dependent).
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tomllib
from pathlib import Path

import pytest

WIZARD = Path(__file__).resolve().parents[2] / "scripts" / "setup" / "wizard.py"

_SECRETISH = ("_API_KEY", "_TOKEN", "_BASE_URL", "_APP_PASSWORD")


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for name in list(os.environ):
        if name.endswith(_SECRETISH) or name in (
            "ACADEMIC_RESEARCH_PROVIDER", "LOCALAPPDATA", "CROSSREF_MAILTO",
            "UNPAYWALL_EMAIL",
        ):
            monkeypatch.delenv(name, raising=False)
    return home


def _load(home: Path):
    """Load the wizard fresh so its module-level paths bind to `home`."""
    spec = importlib.util.spec_from_file_location("wizard_rerun_under_test", WIZARD)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve annotations through it
    spec.loader.exec_module(mod)
    assert mod.CONFIG_PATH.is_relative_to(home)
    return mod


def _run(mod, monkeypatch) -> int:
    monkeypatch.setattr(sys, "argv", ["wizard.py", "--non-interactive", "--skip-verify"])
    monkeypatch.setattr(mod, "_check_zotero_local", lambda *a, **k: ("not_running", "stub"))
    monkeypatch.setattr(mod, "_check_zotero_cli", lambda: ("ok", "stub"))
    monkeypatch.setattr(mod, "_check_mcp_servers", lambda: {})
    monkeypatch.setattr(mod, "_agy_available", lambda: False)
    monkeypatch.setattr(mod, "_maybe_add_claude_to_gitignore", lambda *a, **k: None)
    return mod.main()


def _write_cfg(mod, text: str) -> None:
    mod.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    mod.CONFIG_PATH.write_text(text, encoding="utf-8")


def _read_cfg(mod) -> dict:
    return tomllib.loads(mod.CONFIG_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# P20 — the wizard writes no Claude Code permission rules
# ---------------------------------------------------------------------------


def test_wizard_never_touches_claude_settings(home, monkeypatch) -> None:
    """The wizard writes no permission rules, allow or deny, anywhere.

    `~/.claude/settings.json` is the user's file. This is also what the
    cluster `confirm` level rests on: with no allow rule for ssh / scp /
    sbatch, Claude Code's permission prompt *is* the approval.
    """
    claude = home / ".claude"
    claude.mkdir()
    settings = claude / "settings.json"
    original = b'{"permissions": {"allow": ["Bash(ls)"]}}\n'
    settings.write_bytes(original)
    before = sorted(p.name for p in claude.iterdir())

    monkeypatch.setenv("ZOTERO_API_KEY", "dummy-zotero-key")
    mod = _load(home)
    _run(mod, monkeypatch)

    assert settings.read_bytes() == original
    assert sorted(p.name for p in claude.iterdir()) == before, (
        "the wizard created a file under ~/.claude (a backup?)"
    )


def test_wizard_does_not_create_claude_settings(home, monkeypatch) -> None:
    (home / ".claude").mkdir()
    monkeypatch.setenv("ZOTERO_API_KEY", "dummy-zotero-key")
    mod = _load(home)
    _run(mod, monkeypatch)
    assert not (home / ".claude" / "settings.json").exists()
    assert not hasattr(mod, "_patch_settings")


# ---------------------------------------------------------------------------
# G2 — a re-run preserves everything it does not explicitly change
# ---------------------------------------------------------------------------

_FULL_CONFIG = """\
[llm]
provider = "gateway"
max_retries = 3

[zotero]
api_key = "zkey"
user_id = "12345"
username = "someone"
accessible_group_ids = "111,222"
group_id = "999"

[gateway]
base_url = "https://llm.example.edu"
api_key_env = "UNI_LLM_TOKEN"
base_url_env = "UNI_LLM_URL"
test_model = "some-model-7b"

[cluster]
automation = "confirm"
host = "login.cluster.example.edu"

[library]
openurl_base = "https://resolver.example.edu/openurl"
no_access = ["wiley"]

[elsevier]
render_xml_to_pdf = false

[custom]
note = "hand-added"
limit = 7

[custom.nested]
flag = true
"""


def test_rerun_preserves_values_it_did_not_ask_about(home, monkeypatch) -> None:
    mod = _load(home)
    _write_cfg(mod, _FULL_CONFIG)
    before = tomllib.loads(_FULL_CONFIG)

    _run(mod, monkeypatch)
    after = _read_cfg(mod)

    for section, items in before.items():
        for key, val in items.items():
            assert after.get(section, {}).get(key) == val, (
                f"[{section}] {key} changed or vanished on a re-run: "
                f"{val!r} -> {after.get(section, {}).get(key)!r}"
            )


def test_rerun_keeps_types(home, monkeypatch) -> None:
    """Numbers and booleans used to be written back as strings."""
    mod = _load(home)
    _write_cfg(mod, _FULL_CONFIG)
    _run(mod, monkeypatch)
    after = _read_cfg(mod)
    assert after["llm"]["max_retries"] == 3
    assert after["elsevier"]["render_xml_to_pdf"] is False
    assert after["custom"]["nested"]["flag"] is True


def test_rerun_drops_zotero_identity_when_the_key_changes(home, monkeypatch) -> None:
    """Kept for the same key; dropped for a different, unverified one,
    which would otherwise be filed under the old account's user ID."""
    mod = _load(home)
    _write_cfg(mod, _FULL_CONFIG)
    monkeypatch.setenv("ZOTERO_API_KEY", "a-different-key")
    _run(mod, monkeypatch)
    zotero = _read_cfg(mod)["zotero"]
    assert zotero["api_key"] == "a-different-key"
    for k in ("user_id", "username", "accessible_group_ids"):
        assert k not in zotero
    assert zotero["group_id"] == "999", "group_id is the user's choice, not derived"


def test_merge_config_applies_explicit_removals(home) -> None:
    mod = _load(home)
    existing = {"cluster": {"automation": "auto", "host": "h"}, "library": {"no_access": ["x"]}}
    merged = mod._merge_config(
        existing, {"llm": {"provider": "openai"}},
        {("cluster", "automation"), ("library", "no_access")},
    )
    assert merged == {"cluster": {"host": "h"}, "llm": {"provider": "openai"}}
    assert existing["cluster"]["automation"] == "auto", "input must not be mutated"


def test_gateway_key_in_declared_env_var_counts_as_configured(home, monkeypatch) -> None:
    mod = _load(home)
    monkeypatch.setenv("UNI_LLM_TOKEN", "secret")
    ok, hint = mod._llm_credential_present(
        "gateway", {}, {"gateway": {"base_url": "https://x", "api_key_env": "UNI_LLM_TOKEN"}},
    )
    assert ok, hint


def test_gateway_hint_names_the_declared_env_var(home, monkeypatch) -> None:
    mod = _load(home)
    ok, hint = mod._llm_credential_present(
        "gateway", {}, {"gateway": {"base_url": "https://x", "api_key_env": "UNI_LLM_TOKEN"}},
    )
    assert not ok
    assert "UNI_LLM_TOKEN" in hint


# ---------------------------------------------------------------------------
# G3 — an unparseable config.toml is never overwritten
# ---------------------------------------------------------------------------


def test_unparseable_config_is_backed_up_and_left_alone(home, monkeypatch, capsys) -> None:
    mod = _load(home)
    broken = '[zotero]\napi_key = "unterminated\n'
    _write_cfg(mod, broken)
    monkeypatch.setenv("ZOTERO_API_KEY", "dummy-zotero-key")

    with pytest.raises(SystemExit) as exc:
        _run(mod, monkeypatch)

    assert exc.value.code == mod.EXIT_CONFIG_UNREADABLE
    assert mod.CONFIG_PATH.read_text(encoding="utf-8") == broken
    backups = list(mod.CONFIG_PATH.parent.glob("config.toml.unreadable-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == broken
    assert "cannot parse" in capsys.readouterr().err


def test_written_config_is_private(home, monkeypatch) -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX modes only")
    monkeypatch.setenv("ZOTERO_API_KEY", "dummy-zotero-key")
    mod = _load(home)
    _run(mod, monkeypatch)
    assert mod.CONFIG_PATH.stat().st_mode & 0o777 == 0o600
    assert not list(mod.CONFIG_PATH.parent.glob(".config.toml.*.tmp"))


# ---------------------------------------------------------------------------
# G7 — values with control characters survive the round trip
# ---------------------------------------------------------------------------


def test_value_with_newline_and_control_chars_round_trips(home, monkeypatch) -> None:
    mod = _load(home)
    nasty = 'line one\nline two\t"quoted" \\ \x01 end'
    mod._write_config({"custom": {"v": nasty}})
    assert _read_cfg(mod)["custom"]["v"] == nasty
