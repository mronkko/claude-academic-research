"""Minimal config.toml writer for runtime mutations.

Used by:
  - The "Always skip" failure-prompt path in enrich_pdfs.py (appends a
    publisher name to `[library] no_access`).
  - The setup wizard's `no_access` editor (removes entries).

Design notes:
  - Re-uses the wizard's manual TOML format. Quoted strings and flat
    lists-of-strings are the only two value shapes; that's all the
    plugin currently writes.
  - Does NOT preserve comments or key ordering from an existing
    file — parses with `tomllib` and re-serialises. The file this
    touches is short and owned by the plugin.
  - Preserves file mode 0600 (same as the wizard).
  - Calls `config_loader.load_config.cache_clear()` after every
    write so a running process sees its own mutations on subsequent
    `get()` calls.
"""

from __future__ import annotations

import os
import sys

from core.config_loader import CONFIG_PATH, load_config


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _render(val: object) -> str:
    if isinstance(val, list):
        inner = ", ".join(f'"{_escape(str(v))}"' for v in val)
        return f"[{inner}]"
    return f'"{_escape(str(val))}"'


def _dump(values: dict) -> str:
    lines = [
        "# academic-research plugin configuration.",
        "# Mode 0600. Never commit to git. If leaked, rotate every key below.",
        "",
    ]
    for section, items in values.items():
        lines.append(f"[{section}]")
        for key, val in items.items():
            lines.append(f"{key} = {_render(val)}")
        lines.append("")
    return "\n".join(lines)


def _read() -> dict:
    """Read config.toml fresh (bypassing the lru_cache in config_loader)."""
    load_config.cache_clear()
    return dict(load_config())


def _write(values: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # POSIX permission bits don't apply on Windows (os.chmod only toggles
    # the read-only flag there). The config file is under the user's home
    # directory, which NTFS protects per-user by default, so skipping is
    # safe on Windows.
    if sys.platform != "win32":
        os.chmod(CONFIG_PATH.parent, 0o700)
    CONFIG_PATH.write_text(_dump(values), encoding="utf-8")
    if sys.platform != "win32":
        os.chmod(CONFIG_PATH, 0o600)
    load_config.cache_clear()


def append_to_list(section: str, key: str, value: str) -> None:
    """Append `value` to the list at `[section] key`, creating the
    section/key if missing.

    Idempotent: appending a value already in the list is a no-op.
    Raises ValueError if the existing key is present but is not a list
    — refuses to overwrite a scalar.
    """
    values = _read()
    sect = values.setdefault(section, {})
    existing = sect.get(key)
    if existing is None:
        sect[key] = [value]
    elif isinstance(existing, list):
        if value not in existing:
            existing.append(value)
    else:
        raise ValueError(
            f"[{section}] {key} is not a list "
            f"({type(existing).__name__}); refusing to overwrite.",
        )
    _write(values)


def remove_from_list(section: str, key: str, values_to_remove: list[str]) -> None:
    """Remove each of `values_to_remove` from `[section] key`.

    Removes the key (and empty section) entirely when the list becomes
    empty — cleaner than leaving `no_access = []` in the file.
    """
    values = _read()
    sect = values.get(section)
    if not sect or key not in sect:
        return
    existing = sect[key]
    if not isinstance(existing, list):
        return
    remaining = [v for v in existing if v not in values_to_remove]
    if remaining:
        sect[key] = remaining
    else:
        sect.pop(key, None)
        if not sect:
            values.pop(section, None)
    _write(values)
