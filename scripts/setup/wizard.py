#!/usr/bin/env python3
"""Interactive setup wizard for the academic-research plugin.

Runs in the user's terminal. Prompts for API keys with hidden input
(getpass), writes ~/.config/academic-research/config.toml mode 0600,
patches ~/.claude/settings.json with the permission rules the plugin
needs, and reports status.

API keys entered here NEVER pass through Claude's context — the wizard
is a normal process reading the terminal directly. Claude only sees
the final summary line.

Usage:
    python3 wizard.py               # interactive; re-run to update keys
    python3 wizard.py --non-interactive  # read from env vars (for CI /
                                         # reproducible fresh-machine setup)
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "academic-research"
CONFIG_PATH = CONFIG_DIR / "config.toml"
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

PLUGIN_ROOT_ENV = "${CLAUDE_PLUGIN_ROOT}"


@dataclass(frozen=True)
class KeySpec:
    env_var: str
    toml_section: str
    toml_key: str
    label: str
    required: bool
    hidden: bool
    explanation: str


KEYS: tuple[KeySpec, ...] = (
    # Required for core functionality
    KeySpec(
        "ZOTERO_API_KEY", "zotero", "api_key", "Zotero API key",
        required=True, hidden=True,
        explanation="Generate at https://www.zotero.org/settings/keys with write access.",
    ),
    KeySpec(
        "ZOTERO_GROUP", "zotero", "group_id", "Zotero group library ID",
        required=True, hidden=False,
        explanation="Numeric ID from the group URL (https://www.zotero.org/groups/<this>). "
                    "Use your user-library ID if you have no group.",
    ),
    KeySpec(
        "ANTHROPIC_API_KEY", "anthropic", "api_key", "Anthropic API key",
        required=True, hidden=True,
        explanation="Generate at https://console.anthropic.com/settings/keys. "
                    "Required for LLM screening and coding.",
    ),
    # Required for systematic reviews
    KeySpec(
        "WOS_API_KEY_EXTENDED", "wos", "expanded_key", "Web of Science Expanded API key",
        required=False, hidden=True,
        explanation="Institutional access to the Expanded WoS API. Needed for scripted SR search.",
    ),
    KeySpec(
        "ELSEVIER_API_KEY", "elsevier", "api_key", "Elsevier / ScienceDirect API key",
        required=False, hidden=True,
        explanation="Institutional access. Needed for Elsevier PDF and abstract retrieval.",
    ),
    KeySpec(
        "S2_API_KEY", "semantic_scholar", "api_key", "Semantic Scholar API key",
        required=False, hidden=True,
        explanation="Higher rate limits. Request at "
                    "https://www.semanticscholar.org/product/api#api-key-form",
    ),
    KeySpec(
        "CROSSREF_MAILTO", "crossref", "mailto", "Crossref polite-pool email",
        required=False, hidden=False,
        explanation="Any valid email. Used as Crossref's polite-pool identifier.",
    ),
    # Optional
    KeySpec(
        "WILEY_TDM_TOKEN", "wiley", "tdm_token", "Wiley TDM token",
        required=False, hidden=True,
        explanation="UUID from your institution's Wiley TDM agreement (librarian has it). "
                    "Only needed to pull Wiley PDFs at scale.",
    ),
    KeySpec(
        "OPENALEX_API_KEY", "openalex", "api_key", "OpenAlex Content API key (paid)",
        required=False, hidden=True,
        explanation="Paid tier, $0.01/PDF. Skip unless you need high-volume PDF retrieval.",
    ),
)


def _print_header() -> None:
    print()
    print("=" * 64)
    print("  academic-research plugin — setup wizard")
    print("=" * 64)
    print()
    print("  This will:")
    print("    1. Collect API keys (hidden input)")
    print(f"    2. Write {CONFIG_PATH} (mode 0600)")
    print(f"    3. Patch {SETTINGS_PATH} with permission rules")
    print()
    print("  Your keys stay on this machine. They do not pass through")
    print("  Claude's context at any point.")
    print()


def _prompt(spec: KeySpec, existing: str | None, interactive: bool) -> str:
    env_value = os.environ.get(spec.env_var, "").strip()
    if not interactive:
        return env_value or (existing or "")

    # Default precedence: env var, then existing config value, then empty.
    default = env_value or (existing or "")
    source = "environment" if env_value else ("existing config" if existing else "")

    marker = " [REQUIRED]" if spec.required else " [optional, press Enter to skip]"
    if default:
        display = "*" * 8 if spec.hidden else default
        marker += f" (from {source}: {display}; press Enter to keep)"
    print(f"\n  {spec.label}{marker}")
    print(f"    {spec.explanation}")
    try:
        if spec.hidden:
            value = getpass.getpass("    > ").strip()
        else:
            value = input("    > ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  Aborted.")
        sys.exit(1)
    # Empty input keeps the default; any non-empty input replaces it.
    return value or default


def _load_existing_config() -> dict[str, dict[str, str]]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        import tomllib
        with CONFIG_PATH.open("rb") as f:
            return tomllib.load(f)
    except Exception as e:
        print(f"  Warning: could not parse existing {CONFIG_PATH}: {e}", file=sys.stderr)
        return {}


def _collect_keys(interactive: bool) -> dict[str, dict[str, str]]:
    existing = _load_existing_config()
    values: dict[str, dict[str, str]] = {}

    missing_required: list[str] = []
    for spec in KEYS:
        prior = existing.get(spec.toml_section, {}).get(spec.toml_key, "")
        val = _prompt(spec, prior, interactive)
        if spec.required and not val:
            missing_required.append(spec.env_var)
            continue
        if val:
            values.setdefault(spec.toml_section, {})[spec.toml_key] = val

    if missing_required:
        print("\n  Required keys missing: " + ", ".join(missing_required))
        print("  Re-run the wizard and supply these before using the plugin.")
        sys.exit(2)

    return values


def _write_config(values: dict[str, dict[str, str]]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)

    lines = [
        "# academic-research plugin configuration.",
        "# Mode 0600. Never commit to git. If leaked, rotate every key below.",
        "",
    ]
    for section, items in values.items():
        lines.append(f"[{section}]")
        for key, val in items.items():
            escaped = val.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{key} = "{escaped}"')
        lines.append("")

    CONFIG_PATH.write_text("\n".join(lines), encoding="utf-8")
    os.chmod(CONFIG_PATH, 0o600)


def _permission_patterns() -> tuple[list[str], list[str]]:
    home = str(Path.home())
    absolute_home_pattern = f"//{home.lstrip('/')}"

    allow = [
        f"Bash(uv run {PLUGIN_ROOT_ENV}/scripts/**)",
        f"Bash(uv run -s {PLUGIN_ROOT_ENV}/scripts/**)",
        f"Bash(uv run --script {PLUGIN_ROOT_ENV}/scripts/**)",
        f"Bash(python3 {PLUGIN_ROOT_ENV}/scripts/**)",
        f"Bash({PLUGIN_ROOT_ENV}/scripts/**.py:*)",
        "Bash(playwright install chromium)",
        "Bash(playwright install-deps)",
        f"Read({absolute_home_pattern}/.config/academic-research/)",
    ]
    deny = [
        f"Read({absolute_home_pattern}/.config/academic-research/config.toml)",
        f"Bash(cat {home}/.config/academic-research/config.toml)",
        f"Bash(head {home}/.config/academic-research/config.toml*)",
        f"Bash(tail {home}/.config/academic-research/config.toml*)",
        f"Bash(grep*{home}/.config/academic-research/config.toml*)",
    ]
    return allow, deny


def _patch_settings() -> tuple[int, int]:
    """Returns (allow_added, deny_added) counts."""
    allow_new, deny_new = _permission_patterns()

    if SETTINGS_PATH.exists():
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  ERROR: cannot parse {SETTINGS_PATH}: {e}", file=sys.stderr)
            print("  Back up your settings.json, then re-run the wizard.", file=sys.stderr)
            sys.exit(3)
        # Backup before mutating.
        backup = SETTINGS_PATH.with_suffix(".json.bak-wizard")
        shutil.copy2(SETTINGS_PATH, backup)
    else:
        data = {}

    perms = data.setdefault("permissions", {})
    allow_list = perms.setdefault("allow", [])
    deny_list = perms.setdefault("deny", [])

    allow_added = 0
    for p in allow_new:
        if p not in allow_list:
            allow_list.append(p)
            allow_added += 1

    deny_added = 0
    for p in deny_new:
        if p not in deny_list:
            deny_list.append(p)
            deny_added += 1

    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return allow_added, deny_added


def _check_mcp_servers() -> list[str]:
    """Return a list of MCP server names claude knows about."""
    out = shutil.which("claude")
    if not out:
        return []
    import subprocess
    try:
        result = subprocess.run(
            ["claude", "mcp", "list"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return []
        return [
            line.split()[0].rstrip(":")
            for line in result.stdout.splitlines()
            if line.strip() and not line.startswith(" ")
        ]
    except Exception:
        return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--non-interactive", action="store_true",
        help="Read keys from environment variables instead of prompting.",
    )
    args = parser.parse_args()

    if platform.system() == "Windows":
        # getpass on Windows works in cmd/PowerShell; warn about unusual shells.
        pass

    interactive = not args.non_interactive
    if interactive:
        _print_header()
        env_hits = [k.env_var for k in KEYS if os.environ.get(k.env_var, "").strip()]
        if env_hits:
            print(f"  Detected environment variables: {', '.join(env_hits)}")
            print("  These will be offered as defaults below (press Enter to accept).")
            print()

    values = _collect_keys(interactive)
    _write_config(values)
    allow_added, deny_added = _patch_settings()

    mcp_servers = _check_mcp_servers()
    expected_mcp = {"openalex", "semantic-scholar", "zotero", "paper-search"}
    missing_mcp = sorted(expected_mcp - set(mcp_servers))

    print()
    print("  Setup complete.")
    print(f"    Config:   {CONFIG_PATH} (mode 0600)")
    print(f"    Settings: {SETTINGS_PATH} (+{allow_added} allow, +{deny_added} deny)")
    if missing_mcp:
        print(f"    MCP servers still to register: {', '.join(missing_mcp)}")
        print("    Register each via `claude mcp add <name> <command>`.")
    else:
        print("    MCP servers: all expected servers appear to be registered.")
    print()
    print("  Return to your Claude Code session and tell Claude setup is done.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
