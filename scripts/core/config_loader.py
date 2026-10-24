"""Canonical loader for ~/.config/academic-research/config.toml.

All pipeline scripts that need API keys or other plugin-level
configuration should call through this module rather than reading
the file directly. This keeps the read path consistent and keeps the
file contents entirely inside the pipeline-script process — never in
Claude's tool layer.

Precedence for any config value:
    1. Environment variable (explicit override wins)
    2. config.toml [section][key]
    3. default argument

Usage:
    from core.config_loader import get

    zotero_key = get("zotero", "local_api_key", env="ZOTERO_LOCAL_API_KEY")
    group_id   = get("zotero", "group_id", env="ZOTERO_GROUP")
    mailto     = get("crossref", "mailto", env="CROSSREF_MAILTO",
                     default="anonymous@example.com")
"""

from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "academic-research" / "config.toml"


@lru_cache(maxsize=1)
def load_config() -> dict:
    """Load config.toml into a nested dict. Cached. Empty dict if missing."""
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open("rb") as f:
        return tomllib.load(f)


def get(section: str, key: str, env: str | None = None, default: str = "") -> str:
    """Fetch a config value. Environment variable (if given) takes precedence."""
    if env:
        val = os.environ.get(env, "").strip()
        if val:
            return val
    val = load_config().get(section, {}).get(key, default)
    # Stripped like the environment value above. A key pasted into the
    # file with a trailing newline or space is otherwise sent as-is, and
    # `http.client` rejects it with an error that quotes the key.
    return val.strip() if isinstance(val, str) else val


def value_source(section: str, key: str, env: str | None = None) -> str | None:
    """Which source `get` would take this value from: "env", "config", or
    None when neither has it.

    For error messages, not for reading: an API key the server rejects is
    only actionable if the user knows *which* copy was sent, and the
    environment variable silently overriding a good key in config.toml
    (a stale export in a shell profile) is a failure that otherwise looks
    like a bad key in the config. Returns a label, never the value.
    """
    if env and os.environ.get(env, "").strip():
        return "env"
    val = load_config().get(section, {}).get(key, "")
    if isinstance(val, str):
        val = val.strip()
    return "config" if val else None


def require(section: str, key: str, env: str | None = None) -> str:
    """Like `get`, but raise RuntimeError if the value is empty.

    The error message points at the wizard via `Path(__file__)` rather
    than a `~/.claude/plugins/cache/.../*/` glob — the glob breaks when
    two plugin versions are cached side-by-side after an update
    (P12 in BACKLOG.md). `__file__` always resolves to the version of
    the script currently executing, so the path is single-valued by
    construction.
    """
    val = get(section, key, env=env)
    if not val:
        sources = f"{env}" if env else ""
        sources += " or " if env else ""
        sources += f"config.toml [{section}].{key}"
        wizard_path = (
            Path(__file__).resolve().parent.parent / "setup" / "wizard.py"
        )
        raise RuntimeError(
            f"Required configuration missing: {sources}. "
            f"Run setup (or re-run the wizard at {wizard_path}) "
            f"to provide this value."
        )
    return val
