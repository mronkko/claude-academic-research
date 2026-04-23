#!/usr/bin/env python3
"""Report which citation databases the user's plugin config unlocks.

Usage:
    python3 check_database_access.py

Reads ~/.config/academic-research/config.toml out-of-process (skills
cannot Read it directly — a permission deny rule blocks the Read
tool), and prints one line per database in the form:

    <database>: available
    <database>: not configured

Only yes/no status is emitted; API keys never appear in stdout.

Skills use this to orient themselves during scope interviews — the
SR skill's Scope lock-in gate reads this before asking the user to
pick a database set, so the question can be specific ("Scopus and
OpenAlex are configured; use both?") rather than blind ("do you
have institutional access?").
"""
import tomllib
from pathlib import Path

CONFIG = Path.home() / ".config" / "academic-research" / "config.toml"


def _load() -> dict:
    if not CONFIG.is_file():
        return {}
    try:
        with CONFIG.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError:
        return {}


def _has(data: dict, section: str, key: str) -> bool:
    val = data.get(section, {}).get(key, "")
    return bool(isinstance(val, str) and val.strip())


def main() -> int:
    data = _load()

    scopus = _has(data, "scopus", "api_key") or _has(data, "elsevier", "api_key")
    wos_expanded = _has(data, "wos", "expanded_key")
    wos_starter = _has(data, "wos", "starter_key")
    ss_paid = _has(data, "semantic_scholar", "api_key")
    oa_paid = _has(data, "openalex", "api_key")

    print(f"scopus: {'available' if scopus else 'not configured'}")
    print(f"wos_expanded: {'available' if wos_expanded else 'not configured'}")
    print(f"wos_starter: {'available' if wos_starter else 'not configured'}")
    print("openalex: available (free tier; no key required)"
          if not oa_paid
          else "openalex: available (paid Content API key configured)")
    print("semantic_scholar: available (free tier; no key required)"
          if not ss_paid
          else "semantic_scholar: available (higher-rate key configured)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
