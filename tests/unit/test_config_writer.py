"""Tests for scripts/core/config_writer.py.

Exercises `append_to_list` + `remove_from_list` against a
tmp-path-redirected config file. The real
`~/.config/academic-research/config.toml` is NEVER touched because
every test monkeypatches `config_loader.CONFIG_PATH` and
`config_writer.CONFIG_PATH`.
"""

from __future__ import annotations

import os
import stat
import sys
import tomllib
from pathlib import Path

import pytest


@pytest.fixture
def _redirect_config(tmp_path: Path, monkeypatch):
    """Point both config_loader and config_writer at tmp_path so the
    helpers read/write an ephemeral config.toml."""
    from core import config_loader, config_writer

    cfg_dir = tmp_path / ".config" / "academic-research"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = cfg_dir / "config.toml"

    monkeypatch.setattr(config_loader, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(config_writer, "CONFIG_PATH", cfg_path)
    config_loader.load_config.cache_clear()
    return cfg_path


def _write_initial(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    if sys.platform != "win32":
        # POSIX permission bits are a no-op on Windows (os.chmod only
        # toggles the read-only flag there) — and setting read-only
        # would break the subsequent write() in the test itself.
        os.chmod(path, 0o600)


# ---------------------------------------------------------------------------
# append_to_list
# ---------------------------------------------------------------------------


def test_append_to_list_creates_section_and_key(_redirect_config) -> None:
    """Appending to a fresh config (no [library] section at all)
    creates both the section and the list."""
    from core import config_writer

    # Empty file to start.
    _write_initial(_redirect_config, "")

    config_writer.append_to_list("library", "no_access", "aom")

    parsed = tomllib.loads(_redirect_config.read_text())
    assert parsed["library"]["no_access"] == ["aom"]


def test_append_to_list_round_trips_existing_entries(_redirect_config) -> None:
    """Pre-existing entries in the list are preserved; the new one is
    added at the end."""
    from core import config_writer

    _write_initial(
        _redirect_config,
        "[library]\nno_access = [\"aom\"]\n",
    )

    config_writer.append_to_list("library", "no_access", "apa")

    parsed = tomllib.loads(_redirect_config.read_text())
    assert parsed["library"]["no_access"] == ["aom", "apa"]


def test_append_to_list_is_idempotent(_redirect_config) -> None:
    """Appending a value that's already present is a no-op."""
    from core import config_writer

    _write_initial(
        _redirect_config,
        "[library]\nno_access = [\"aom\"]\n",
    )

    config_writer.append_to_list("library", "no_access", "aom")
    config_writer.append_to_list("library", "no_access", "aom")

    parsed = tomllib.loads(_redirect_config.read_text())
    assert parsed["library"]["no_access"] == ["aom"]


def test_append_to_list_preserves_other_sections(_redirect_config) -> None:
    """Scalar values in other sections are untouched after a write."""
    from core import config_writer

    _write_initial(
        _redirect_config,
        (
            "[zotero]\n"
            "api_key = \"secret\"\n"
            "user_id = \"5591\"\n"
            "\n"
        ),
    )

    config_writer.append_to_list("library", "no_access", "aom")

    parsed = tomllib.loads(_redirect_config.read_text())
    assert parsed["zotero"]["api_key"] == "secret"
    assert parsed["zotero"]["user_id"] == "5591"
    assert parsed["library"]["no_access"] == ["aom"]


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX permission bits; NTFS uses per-user ACLs instead",
)
def test_append_to_list_preserves_file_mode_0600(_redirect_config) -> None:
    """Every write restores mode 0600. Prevents leaking keys to other
    users after a `chmod 644` fumble."""
    from core import config_writer

    _write_initial(
        _redirect_config,
        "[zotero]\napi_key = \"secret\"\n",
    )
    os.chmod(_redirect_config, 0o644)       # simulate a mode fumble

    config_writer.append_to_list("library", "no_access", "aom")

    mode = stat.S_IMODE(_redirect_config.stat().st_mode)
    assert mode == 0o600


def test_append_to_list_rejects_overwriting_a_scalar(_redirect_config) -> None:
    """If the key already exists but is not a list, refuse — the user
    likely set it to something meaningful by hand."""
    from core import config_writer

    _write_initial(
        _redirect_config,
        "[library]\nno_access = \"aom\"\n",
    )
    with pytest.raises(ValueError):
        config_writer.append_to_list("library", "no_access", "apa")


# ---------------------------------------------------------------------------
# remove_from_list
# ---------------------------------------------------------------------------


def test_remove_from_list_drops_specified_values(_redirect_config) -> None:
    from core import config_writer

    _write_initial(
        _redirect_config,
        "[library]\nno_access = [\"aom\", \"apa\", \"tandf\"]\n",
    )
    config_writer.remove_from_list("library", "no_access", ["apa"])

    parsed = tomllib.loads(_redirect_config.read_text())
    assert parsed["library"]["no_access"] == ["aom", "tandf"]


def test_remove_from_list_drops_empty_section(_redirect_config) -> None:
    """Removing the last entry drops the key entirely — cleaner than
    leaving `no_access = []` in the file."""
    from core import config_writer

    _write_initial(
        _redirect_config,
        "[library]\nno_access = [\"aom\"]\n",
    )
    config_writer.remove_from_list("library", "no_access", ["aom"])

    parsed = tomllib.loads(_redirect_config.read_text())
    assert "library" not in parsed


def test_remove_from_list_on_missing_section_is_noop(_redirect_config) -> None:
    from core import config_writer

    _write_initial(
        _redirect_config,
        "[zotero]\napi_key = \"x\"\n",
    )
    # Should not raise.
    config_writer.remove_from_list("library", "no_access", ["aom"])

    parsed = tomllib.loads(_redirect_config.read_text())
    assert "library" not in parsed
    assert parsed["zotero"]["api_key"] == "x"


# ---------------------------------------------------------------------------
# Escaping
# ---------------------------------------------------------------------------


def test_append_to_list_escapes_quotes_and_backslashes(_redirect_config) -> None:
    """Bizarre values round-trip — the writer's escape path matches
    the wizard's."""
    from core import config_writer

    _write_initial(_redirect_config, "")
    config_writer.append_to_list("library", "no_access", 'weird"name\\back')

    parsed = tomllib.loads(_redirect_config.read_text())
    assert parsed["library"]["no_access"] == ['weird"name\\back']
