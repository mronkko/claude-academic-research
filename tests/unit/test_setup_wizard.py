"""Smoke tests for the setup wizard.

The wizard is mostly interactive, but we can check the static pieces:
permission-pattern generation, the key schema, and module import.
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
    # Register before exec so dataclass annotation resolution (which calls
    # sys.modules.get(cls.__module__)) has a namespace to look up.
    sys.modules["wizard"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_wizard_imports() -> None:
    _load()


def test_key_schema_has_required_core_keys() -> None:
    mod = _load()
    required_env = {k.env_var for k in mod.KEYS if k.required}
    assert "ZOTERO_API_KEY" in required_env
    assert "ANTHROPIC_API_KEY" in required_env
    # ZOTERO_GROUP deliberately NOT required — group IDs are per-project,
    # not global config. The wizard discovers user_id from the Zotero API
    # using the collected API key; group is set per-project via env var.
    assert "ZOTERO_GROUP" not in {k.env_var for k in mod.KEYS}


def test_discover_zotero_identity_returns_none_on_network_failure(monkeypatch) -> None:
    import urllib.error
    mod = _load()
    def raise_url_error(*a, **kw):
        raise urllib.error.URLError("simulated offline")
    monkeypatch.setattr(mod.urllib if hasattr(mod, "urllib") else __import__("urllib.request"),
                        "urlopen", raise_url_error, raising=False)
    # Simpler path: monkeypatch the function inside the module's import tree.
    import urllib.request as urlreq
    monkeypatch.setattr(urlreq, "urlopen", raise_url_error)
    result = mod._discover_zotero_identity("fake-key", interactive=False)
    assert result is None


def test_permission_patterns_cover_plugin_scripts() -> None:
    mod = _load()
    allow, deny = mod._permission_patterns()
    assert any("uv run" in p and "scripts/**" in p for p in allow)
    assert any("python3" in p and "scripts/**" in p for p in allow)
    assert any("playwright install chromium" in p for p in allow)
    assert any("config.toml" in p for p in deny)


def test_permission_patterns_deny_covers_read_and_shell() -> None:
    """Config file must be blocked against both Read tool and Bash readers."""
    mod = _load()
    _, deny = mod._permission_patterns()
    assert any(p.startswith("Read(") for p in deny), "deny list missing Read() entry"
    assert any("cat " in p for p in deny), "deny list missing `cat` Bash entry"


def test_config_path_is_under_home() -> None:
    mod = _load()
    assert str(mod.CONFIG_PATH).endswith(".config/academic-research/config.toml")


def test_every_key_has_explanation() -> None:
    mod = _load()
    for k in mod.KEYS:
        assert k.explanation, f"key {k.env_var} has no explanation"
        assert len(k.explanation) >= 20, f"key {k.env_var} explanation too short"


def test_non_interactive_reads_env_vars(monkeypatch) -> None:
    mod = _load()
    spec = next(k for k in mod.KEYS if k.env_var == "ZOTERO_API_KEY")
    monkeypatch.setenv("ZOTERO_API_KEY", "test-from-env")
    assert mod._prompt(spec, None, interactive=False) == "test-from-env"


def test_non_interactive_env_overrides_existing_config(monkeypatch) -> None:
    """If both env and existing config have a value, env wins (explicit >
    implicit)."""
    mod = _load()
    spec = next(k for k in mod.KEYS if k.env_var == "ZOTERO_API_KEY")
    monkeypatch.setenv("ZOTERO_API_KEY", "from-env")
    assert mod._prompt(spec, "from-config", interactive=False) == "from-env"


def test_non_interactive_falls_back_to_existing_when_no_env(monkeypatch) -> None:
    mod = _load()
    spec = next(k for k in mod.KEYS if k.env_var == "ZOTERO_API_KEY")
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    assert mod._prompt(spec, "from-config", interactive=False) == "from-config"
