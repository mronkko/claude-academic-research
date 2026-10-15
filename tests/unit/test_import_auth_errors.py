"""import_to_zotero stops on a rejected Zotero API key, and says which key.

A dry run that hit HTTP 403 "Invalid key" used to print a WARNING and carry
on with "NOT CHECKED" counts, which reads as "offline, try the real run"
when the real run will fail the same way. The user's actual cause was a
stale `ZOTERO_API_KEY` exported in `~/.zshenv`, silently overriding a good
key in config.toml — invisible unless the error says which copy was sent.
A rejected key (401/403) is now fatal in both modes and names its source;
the dry run also stopped sending a placeholder key, which Zotero refuses
with the same 403.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path
from unittest.mock import MagicMock

import import_to_zotero as imp
import pytest
import zotero_io
from core import config_loader
from pyzotero import errors as pyz_errors

INVALID_KEY = "Code: 403\nURL: https://api.zotero.org/groups/1/items\nResponse: Invalid key"


def _zot(exc: BaseException | None = None, items: list | None = None) -> MagicMock:
    zot = MagicMock()
    zot.describe_library.return_value = "group 1"
    if exc is not None:
        zot.cloud_journal_articles.side_effect = exc
    else:
        zot.cloud_journal_articles.return_value = items or []
    return zot


def _write_config(text: str) -> None:
    config_loader.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    config_loader.CONFIG_PATH.write_text(text, encoding="utf-8")
    config_loader.load_config.cache_clear()


# ---------------------------------------------------------------------------
# config_loader.value_source
# ---------------------------------------------------------------------------


def test_value_source_env_overrides_config(monkeypatch) -> None:
    _write_config('[zotero]\napi_key = "from-file"\n')
    monkeypatch.setenv("ZOTERO_API_KEY", "from-env")
    assert config_loader.value_source("zotero", "api_key", env="ZOTERO_API_KEY") == "env"


def test_value_source_config_when_env_unset() -> None:
    _write_config('[zotero]\napi_key = "from-file"\n')
    assert config_loader.value_source("zotero", "api_key", env="ZOTERO_API_KEY") == "config"


def test_value_source_none_when_missing_or_blank(monkeypatch) -> None:
    assert config_loader.value_source("zotero", "api_key", env="ZOTERO_API_KEY") is None
    _write_config('[zotero]\napi_key = "   "\n')
    monkeypatch.setenv("ZOTERO_API_KEY", "  ")
    assert config_loader.value_source("zotero", "api_key", env="ZOTERO_API_KEY") is None


# ---------------------------------------------------------------------------
# zotero_io.auth_failure_status
# ---------------------------------------------------------------------------


def test_auth_failure_status_recognises_401_and_403_only() -> None:
    assert zotero_io.auth_failure_status(pyz_errors.UserNotAuthorisedError(INVALID_KEY)) == 403
    assert zotero_io.auth_failure_status(
        pyz_errors.UserNotAuthorisedError("Code: 401\nResponse: no key")) == 401
    assert zotero_io.auth_failure_status(RuntimeError("403 Forbidden")) is None
    assert zotero_io.auth_failure_status(
        pyz_errors.HTTPError("Code: 503\nResponse: busy")) is None


# ---------------------------------------------------------------------------
# _fetch_existing_items
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dry_run", [True, False])
def test_rejected_key_is_fatal_in_both_modes(dry_run: bool) -> None:
    zot = _zot(pyz_errors.UserNotAuthorisedError(INVALID_KEY))
    with pytest.raises(SystemExit) as info:
        imp._fetch_existing_items(zot, dry_run, key_source="config")
    message = str(info.value.code)
    assert message.startswith("ERROR: Zotero rejected the API key for group 1")
    assert "HTTP 403: Response: Invalid key" in message
    assert imp._existing_lookup_failed is False


def test_env_sourced_key_says_the_env_var_overrides_config() -> None:
    zot = _zot(pyz_errors.UserNotAuthorisedError(INVALID_KEY))
    with pytest.raises(SystemExit) as info:
        imp._fetch_existing_items(zot, True, key_source="env")
    message = str(info.value.code)
    assert "ZOTERO_API_KEY environment variable" in message
    assert "overrides [zotero].api_key in ~/.config/academic-research/config.toml" in message
    assert "shell profile" in message


def test_config_sourced_key_says_the_env_var_is_not_set() -> None:
    zot = _zot(pyz_errors.UserNotAuthorisedError("Code: 401\nResponse: no"))
    with pytest.raises(SystemExit) as info:
        imp._fetch_existing_items(zot, False, key_source="config")
    message = str(info.value.code)
    assert "HTTP 401" in message
    assert "ZOTERO_API_KEY is not set" in message
    assert "no access to this library" not in message  # 403-only advice


def test_other_failures_still_degrade_in_a_dry_run(capsys) -> None:
    zot = _zot(pyz_errors.HTTPError("Code: 503\nResponse: busy"))
    assert imp._fetch_existing_items(zot, True, key_source="config") == ({}, {})
    assert imp._existing_lookup_failed is True
    imp._existing_lookup_failed = False


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


@pytest.fixture
def run_main(tmp_path, monkeypatch):
    def _run(zot, *extra) -> tuple[object, dict]:
        csv_path = tmp_path / "in.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["doi", "title"])
            w.writeheader()
            w.writerow({"doi": "10.1016/j.example.2020.01.001", "title": "A"})
        seen: dict = {}

        def from_args(cls, args, **kw):
            seen.update(kw)
            return zot

        monkeypatch.setattr(imp.zotero_io.ZoteroClient, "from_args",
                            classmethod(from_args))
        monkeypatch.setattr(imp.screening_common, "load_tag_namespace",
                            lambda *a, **k: "rev")
        # Stop the metadata fill from reaching Crossref; these tests are
        # about the library lookup, which happens before it.
        monkeypatch.setattr(
            imp, "build_item",
            lambda row, coll, **kw: ({"itemType": "journalArticle",
                                      "title": row["title"], "DOI": row["doi"]},
                                     imp.BUILD_SOURCE))
        monkeypatch.setattr(sys, "argv", ["import_to_zotero.py", "--group", "1",
                                          "--input", str(csv_path), *extra])
        try:
            return imp.main(), seen
        except SystemExit as exc:
            return exc, seen
    return _run


def test_dry_run_sends_the_real_key_and_exits_on_rejection(run_main, monkeypatch) -> None:
    monkeypatch.setenv("ZOTERO_API_KEY", "stale-env-key")
    result, seen = run_main(_zot(pyz_errors.UserNotAuthorisedError(INVALID_KEY)),
                            "--dry-run")
    assert seen["api_key"] == "stale-env-key"
    assert isinstance(result, SystemExit)
    assert "ZOTERO_API_KEY environment variable" in str(result.code)
    assert "stale-env-key" not in str(result.code)


def test_real_run_exits_cleanly_on_rejection(run_main, monkeypatch) -> None:
    _write_config('[zotero]\napi_key = "file-key"\n')
    result, seen = run_main(_zot(pyz_errors.UserNotAuthorisedError(INVALID_KEY)))
    assert seen["api_key"] == "file-key"
    assert isinstance(result, SystemExit)
    assert "ZOTERO_API_KEY is not set" in str(result.code)
    assert "file-key" not in str(result.code)


def test_dry_run_without_a_key_skips_the_lookup(run_main, capsys) -> None:
    zot = _zot(AssertionError("must not read without a key"))
    result, _seen = run_main(zot, "--dry-run")
    try:
        out = capsys.readouterr().out
        assert "no Zotero API key configured" in out
        assert "NOT CHECKED" in out
        zot.cloud_journal_articles.assert_not_called()
        assert not isinstance(result, SystemExit) or result.code in (0, None)
    finally:
        imp._existing_lookup_failed = False


def test_module_reads_config_path_at_call_time() -> None:
    """The message names the configured path, which conftest repoints —
    a copy bound at import would show the developer's real path."""
    assert not hasattr(imp, "CONFIG_PATH")
    assert Path(config_loader.CONFIG_PATH).name == "config.toml"
