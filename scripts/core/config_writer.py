"""Minimal config.toml writer for runtime mutations.

Used by:
  - The "Always skip" failure-prompt path in enrich_pdfs.py (appends a
    publisher name to `[library] no_access`).
  - The setup wizard's `no_access` editor (removes entries).
  - `scripts/setup/set_llm_provider.py` (sets `[llm] provider`).

Design notes:
  - One serialiser for the wizard and the runtime helpers (`_dump`).
    It renders every value shape `tomllib` returns, so keys and
    sections this module does not know about survive a rewrite.
  - Does NOT preserve comments or key ordering from an existing
    file — parses with `tomllib` and re-serialises. The file this
    touches is short and owned by the plugin.
  - Preserves file mode 0600 (same as the wizard).
  - Calls `config_loader.load_config.cache_clear()` after every
    write so a running process sees its own mutations on subsequent
    `get()` calls.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import sys
import tempfile
from pathlib import Path

from core.config_loader import CONFIG_PATH, load_config

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


def _escape(s: str) -> str:
    """Escape `s` for a TOML basic string.

    Backslash and quote, plus every control character: a newline or a
    tab pasted into a key prompt would otherwise end the string early or
    produce a file `tomllib` refuses, and the next read would treat the
    whole config as unparseable.
    """
    out = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return "".join(out)


def _key(k: str) -> str:
    return k if _BARE_KEY.fullmatch(k) else f'"{_escape(k)}"'


def _render(val: object) -> str:
    """Render one TOML value, keeping its type.

    Booleans and numbers used to go through `str()` and come back as
    strings, so a stored `false` read back as the non-empty, truthy
    string "False", and `max_retries = 3` became `"3"`. Everything
    `tomllib` can return is rendered as its own type here, so a
    read-modify-write cycle preserves what it did not touch.
    """
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return repr(val)
    if isinstance(val, (_dt.datetime, _dt.date, _dt.time)):
        return val.isoformat()
    if isinstance(val, list):
        return "[" + ", ".join(_render(v) for v in val) + "]"
    if isinstance(val, dict):
        inner = ", ".join(f"{_key(str(k))} = {_render(v)}" for k, v in val.items())
        return "{" + inner + "}"
    return f'"{_escape(str(val))}"'


def _dump_table(path: list[str], table: dict, lines: list[str]) -> None:
    scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in table.items() if isinstance(v, dict)}
    if path and (scalars or not tables):
        lines.append("[" + ".".join(_key(p) for p in path) + "]")
    for key, val in scalars.items():
        lines.append(f"{_key(str(key))} = {_render(val)}")
    if path and (scalars or not tables):
        lines.append("")
    for key, sub in tables.items():
        _dump_table([*path, str(key)], sub, lines)


def _dump(values: dict) -> str:
    """Serialise a parsed config back to TOML.

    Handles every shape `tomllib` produces — nested tables become
    `[a.b]` headers — because the wizard and these helpers rewrite the
    whole file, and a section or key they do not know about is still the
    user's and must survive.
    """
    lines = [
        "# academic-research plugin configuration.",
        "# Mode 0600. Never commit to git. If leaked, rotate every key below.",
        "",
    ]
    top_scalars = {k: v for k, v in values.items() if not isinstance(v, dict)}
    for key, val in top_scalars.items():
        lines.append(f"{_key(str(key))} = {_render(val)}")
    if top_scalars:
        lines.append("")
    _dump_table([], {k: v for k, v in values.items() if isinstance(v, dict)}, lines)
    return "\n".join(lines)


def _read() -> dict:
    """Read config.toml fresh (bypassing the lru_cache in config_loader)."""
    load_config.cache_clear()
    return dict(load_config())


def write_private(path: Path, text: str) -> None:
    """Replace `path` with `text`, mode 0600 from the first byte.

    Written to a temporary file in the same directory and moved into
    place, so the file never exists with the umask's default mode (it
    holds API keys) and a crash mid-write leaves the old file intact
    rather than a truncated one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # POSIX permission bits don't apply on Windows (os.chmod only toggles
    # the read-only flag there). The config file is under the user's home
    # directory, which NTFS protects per-user by default, so skipping is
    # safe on Windows.
    if sys.platform != "win32":
        os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    if sys.platform != "win32":
        os.chmod(path, 0o600)


def _write(values: dict) -> None:
    write_private(CONFIG_PATH, _dump(values))
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

    Strings only, deliberately: every value the plugin sets this way is
    one, and a non-string here is far more likely a caller's mistake
    than a schema change.
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
