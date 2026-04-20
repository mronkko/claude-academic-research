"""Smoke tests for core.config_loader."""

from __future__ import annotations

from pathlib import Path

from core import config_loader


def _set_config(tmp_path: Path, contents: str, monkeypatch) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(config_loader, "CONFIG_PATH", path)
    config_loader.load_config.cache_clear()
    return path


def test_missing_config_returns_empty(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config_loader, "CONFIG_PATH", tmp_path / "does-not-exist.toml")
    config_loader.load_config.cache_clear()
    assert config_loader.load_config() == {}


def test_get_reads_config(tmp_path, monkeypatch) -> None:
    _set_config(tmp_path, '[zotero]\napi_key = "from-file"\n', monkeypatch)
    assert config_loader.get("zotero", "api_key") == "from-file"


def test_env_overrides_config(tmp_path, monkeypatch) -> None:
    _set_config(tmp_path, '[zotero]\napi_key = "from-file"\n', monkeypatch)
    monkeypatch.setenv("ZOTERO_API_KEY", "from-env")
    assert config_loader.get("zotero", "api_key", env="ZOTERO_API_KEY") == "from-env"


def test_default_used_when_both_missing(tmp_path, monkeypatch) -> None:
    _set_config(tmp_path, "", monkeypatch)
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    assert config_loader.get("zotero", "api_key",
                             env="ZOTERO_API_KEY", default="fallback") == "fallback"


def test_require_raises_when_missing(tmp_path, monkeypatch) -> None:
    _set_config(tmp_path, "", monkeypatch)
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    import pytest
    with pytest.raises(RuntimeError, match="Required configuration missing"):
        config_loader.require("zotero", "api_key", env="ZOTERO_API_KEY")


def test_require_returns_value_when_present(tmp_path, monkeypatch) -> None:
    _set_config(tmp_path, '[zotero]\napi_key = "present"\n', monkeypatch)
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    assert config_loader.require("zotero", "api_key", env="ZOTERO_API_KEY") == "present"
