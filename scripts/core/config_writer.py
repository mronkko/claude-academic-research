"""Minimal config.toml writer for runtime mutations.

Used by:
  - The "Always skip" failure-prompt path in enrich_pdfs.py (appends a
    publisher name to `[library] no_access`).
  - The setup wizard's `no_access` editor (removes entries).
  - `scripts/setup/set_llm_provider.py` (sets `[llm] provider`).

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


def append_to_list(
    section: str, key: str, value: str, *, promote_scalar: bool = False,
) -> None:
    """Append `value` to the list at `[section] key`, creating the
    section/key if missing.

    Idempotent: appending a value already in the list is a no-op.
    Raises ValueError if the existing key is present but is not a list
    — refuses to overwrite a scalar.

    `promote_scalar=True` rewrites an existing scalar as a one-element
    list and appends to that instead of raising. Off by default, because
    for most keys a scalar *is* the schema and widening it silently
    would bury a typo. It exists for the keys that genuinely accept
    either shape: `[library] openurl_base` takes one endpoint or
    several, and a researcher who gains a second affiliation has to be
    able to get from the first shape to the second. Hand-editing is not
    the answer — a `permissions.deny` rule blocks the Read tool on
    config.toml, so an agent cannot see the file to edit it safely.

    The incumbent keeps position 0. For `openurl_base` that is load
    bearing: the first entry is the primary, which retains the existing
    cache keys and breaks ranking ties.
    """
    values = _read()
    sect = values.setdefault(section, {})
    existing = sect.get(key)
    if existing is None:
        sect[key] = [value]
    elif isinstance(existing, list):
        if value not in existing:
            existing.append(value)
    elif promote_scalar:
        incumbent = str(existing)
        sect[key] = [incumbent] if incumbent == value else [incumbent, value]
    else:
        raise ValueError(
            f"[{section}] {key} is not a list "
            f"({type(existing).__name__}); refusing to overwrite.",
        )
    _write(values)


def set_value(section: str, key: str, value: str) -> None:
    """Set the scalar at `[section] key`, creating the section if missing.

    Overwrites an existing scalar — this is the "switch me to OpenAI"
    path, where replacing the old answer is the whole point. Raises
    ValueError if the key currently holds a list, since flattening one
    silently would lose data.

    Scalars only, and deliberately: `_dump` emits exactly one
    `[section]` header per top-level key, so a dict value here would
    serialise as `key = {...}` and fail to round-trip. Everything the
    plugin stores is flat.
    """
    if not isinstance(value, str):
        raise TypeError(
            f"[{section}] {key} must be a string; got {type(value).__name__}. "
            f"The config schema is flat scalars and lists of strings.",
        )
    values = _read()
    sect = values.setdefault(section, {})
    existing = sect.get(key)
    if isinstance(existing, list):
        raise ValueError(
            f"[{section}] {key} is a list; refusing to replace it with a "
            f"scalar. Use remove_from_list to clear it first.",
        )
    sect[key] = value
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
