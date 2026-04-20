#!/usr/bin/env python3
"""Interactive setup wizard for the academic-research plugin.

Runs in the user's terminal. Prompts for API keys with hidden input
(getpass), tests each key against its provider, writes
~/.config/academic-research/config.toml mode 0600, patches
~/.claude/settings.json with the permission rules the plugin needs,
and reports status.

API keys entered here NEVER pass through Claude's context — the wizard
is a normal process reading the terminal directly. Claude only sees
the final summary line.

Usage:
    python3 wizard.py               # interactive; re-run to update keys
    python3 wizard.py --non-interactive  # read from env vars (for CI /
                                         # reproducible fresh-machine setup)
    python3 wizard.py --skip-verify      # skip API verification calls
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "academic-research"
CONFIG_PATH = CONFIG_DIR / "config.toml"
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

PLUGIN_ROOT_ENV = "${CLAUDE_PLUGIN_ROOT}"

# ---------------------------------------------------------------------------
# Per-provider verification helpers.
#
# Each returns (ok: bool, message: str, extras: dict).
# - ok=True means the key is valid.
# - message is a short human-readable result line.
# - extras carries additional data to persist (e.g. Zotero user_id).
# ---------------------------------------------------------------------------


def _http_json(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: int = 10,
) -> tuple[int, dict | None, str]:
    """Plain urllib GET returning (status, json_or_none, error_message)."""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = response.read()
            try:
                return response.status, json.loads(data), ""
            except json.JSONDecodeError:
                return response.status, None, "non-JSON response"
    except urllib.error.HTTPError as e:
        return e.code, None, f"{e.code} {e.reason}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, None, str(e)


def _verify_zotero(key: str) -> tuple[bool, str, dict]:
    status, data, err = _http_json(
        f"https://api.zotero.org/keys/{key}",
        headers={"Zotero-API-Version": "3"},
    )
    if status == 0:
        return False, f"could not reach api.zotero.org ({err}) — saved anyway", {}
    if status == 403 or status == 404:
        return False, "Zotero rejected the key (403/404). Re-check the key.", {}
    if not data:
        return False, f"Zotero returned HTTP {status} with unparseable body", {}
    user_id = str(data.get("userID", ""))
    username = data.get("username", "") or ""
    groups = sorted((data.get("access", {}).get("groups") or {}).keys())
    summary = f"userID={user_id}" + (f" (@{username})" if username else "")
    if groups:
        preview = ", ".join(groups[:5])
        more = f" +{len(groups) - 5} more" if len(groups) > 5 else ""
        summary += f"; groups: {preview}{more}"
    else:
        summary += "; no group libraries accessible"
    return True, summary, {
        "user_id": user_id,
        "username": username,
        "accessible_group_ids": ",".join(groups),
    }


def _verify_anthropic(key: str) -> tuple[bool, str, dict]:
    status, data, err = _http_json(
        "https://api.anthropic.com/v1/models?limit=1",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
    )
    if status == 0:
        return False, f"could not reach api.anthropic.com ({err}) — saved anyway", {}
    if status == 401:
        return False, "Anthropic rejected the key (401). Re-check it.", {}
    if status != 200:
        return False, f"Anthropic returned HTTP {status}", {}
    return True, "key valid; Claude API reachable", {}


def _verify_elsevier(key: str) -> tuple[bool, str, dict]:
    status, _, err = _http_json(
        "https://api.elsevier.com/content/article/doi/10.1016/j.procs.2018.10.404",
        headers={"X-ELS-APIKey": key, "Accept": "application/json"},
    )
    if status == 0:
        return False, f"could not reach api.elsevier.com ({err}) — saved anyway", {}
    if status in (401, 403):
        return False, f"Elsevier rejected the key (HTTP {status})", {}
    if status not in (200, 404, 429):  # 404 for the test DOI is fine; key accepted
        return False, f"Elsevier returned HTTP {status}", {}
    return True, "key valid; ScienceDirect API reachable", {}


def _verify_scopus(key: str) -> tuple[bool, str, dict]:
    status, _, err = _http_json(
        "https://api.elsevier.com/content/search/scopus?query=test&count=1",
        headers={"X-ELS-APIKey": key, "Accept": "application/json"},
    )
    if status == 0:
        return False, f"could not reach api.elsevier.com ({err}) — saved anyway", {}
    if status in (401, 403):
        return False, f"Scopus rejected the key (HTTP {status})", {}
    if status not in (200, 429):  # 429 = quota exceeded but key valid
        return False, f"Scopus returned HTTP {status}", {}
    return True, "key valid; Scopus search API reachable", {}


def _verify_wos_starter(key: str) -> tuple[bool, str, dict]:
    status, _, err = _http_json(
        "https://api.clarivate.com/apis/wos-starter/v1/documents?q=TS%3Dtest&limit=1&page=1",
        headers={"X-ApiKey": key, "Accept": "application/json"},
    )
    if status == 0:
        return False, f"could not reach api.clarivate.com ({err}) — saved anyway", {}
    if status in (401, 403):
        return False, f"WoS Starter rejected the key (HTTP {status})", {}
    if status != 200:
        return False, f"WoS Starter returned HTTP {status}", {}
    return True, "key valid; WoS Starter API reachable", {}


def _verify_semantic_scholar(key: str) -> tuple[bool, str, dict]:
    status, _, err = _http_json(
        "https://api.semanticscholar.org/graph/v1/paper/search?query=test&limit=1",
        headers={"x-api-key": key},
    )
    if status == 0:
        return False, f"could not reach api.semanticscholar.org ({err}) — saved anyway", {}
    if status in (401, 403):
        return False, f"Semantic Scholar rejected the key (HTTP {status})", {}
    if status != 200:
        return False, f"Semantic Scholar returned HTTP {status}", {}
    return True, "key valid; Semantic Scholar graph API reachable", {}


def _verify_wos_extended(key: str) -> tuple[bool, str, dict]:
    status, _, err = _http_json(
        "https://api.clarivate.com/api/wos?databaseId=WOK&usrQuery=TS%3Dtest&count=1&firstRecord=1",
        headers={"X-ApiKey": key, "Accept": "application/json"},
    )
    if status == 0:
        return False, f"could not reach api.clarivate.com ({err}) — saved anyway", {}
    if status in (401, 403):
        return False, f"WoS rejected the key (HTTP {status}) — check entitlement.", {}
    if status != 200:
        return False, f"WoS returned HTTP {status}", {}
    return True, "key valid; WoS Expanded API reachable", {}


def _verify_crossref_mailto(email: str) -> tuple[bool, str, dict]:
    pattern = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    if not pattern.match(email):
        return False, "not a valid email address", {}
    return True, "format looks valid (not contacted)", {}


def _verify_none(_key: str) -> tuple[bool, str, dict]:
    """Used for keys we cannot cheaply verify (e.g. Wiley TDM, OpenAlex paid)."""
    return True, "no inline check — will be exercised by pipeline scripts on first use", {}


# ---------------------------------------------------------------------------
# Key specifications
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeySpec:
    env_var: str
    toml_section: str
    toml_key: str
    label: str
    required: bool
    hidden: bool
    what: str        # plain-language one-liner about the service
    used_by: str     # which skills / pipeline stages use this key
    impact: str      # what happens if this key is not provided
    where: str       # how to get a key
    verify: Callable[[str], tuple[bool, str, dict]] = field(default=_verify_none)


KEYS: tuple[KeySpec, ...] = (
    KeySpec(
        "ZOTERO_API_KEY", "zotero", "api_key", "Zotero API key",
        required=True, hidden=True,
        what="Zotero is a free, open-source reference manager that stores your "
             "citations, PDFs, and notes (https://www.zotero.org). This plugin uses "
             "it as the single source of truth for your bibliography.",
        used_by="Every skill that touches citations: mcp-research, zotero-operations, "
                "systematic-review, fact-check.",
        impact="No skill in the plugin will work without this key — the plugin is "
               "built around Zotero.",
        where="https://www.zotero.org/settings/keys — create a key with write access.",
        verify=_verify_zotero,
    ),
    KeySpec(
        "ANTHROPIC_API_KEY", "anthropic", "api_key", "Anthropic API key",
        required=True, hidden=True,
        what="Anthropic is the company that builds Claude. This API key lets the "
             "plugin's screening and coding scripts call Claude directly — separate "
             "from your interactive Claude Code session.",
        used_by="systematic-review (Claude-driven abstract screening, full-text "
                "screening, and structured coding of included papers).",
        impact="Systematic-review screening pipelines will fail. Skills that don't "
               "call Claude directly still work — for instance, critic-loop uses "
               "your interactive Claude Code session, not this key.",
        where="https://console.anthropic.com/settings/keys",
        verify=_verify_anthropic,
    ),
    KeySpec(
        "WOS_API_KEY_EXTENDED", "wos", "expanded_key",
        "Web of Science Expanded API key",
        required=False, hidden=True,
        what="Web of Science is Clarivate's citation database, one of the two main "
             "commercial indexes of academic journals (the other is Scopus). The "
             "Expanded API tier supports full Boolean search and ISSN filters and is "
             "required for a real systematic-review search.",
        used_by="systematic-review (formal scripted bibliographic search).",
        impact="Systematic-review search drops to the Starter tier (field-limited, "
               "no ISSN filter) or to Scopus alone. Other skills unaffected.",
        where="https://developer.clarivate.com — institutional subscription required.",
        verify=_verify_wos_extended,
    ),
    KeySpec(
        "WOS_API_KEY", "wos", "starter_key", "Web of Science Starter API key",
        required=False, hidden=True,
        what="Same Web of Science database, Starter tier. Simpler queries and no "
             "ISSN filter, but free or cheaper at many institutions. Useful for "
             "piloting search terms before committing to a formal Extended-tier run.",
        used_by="systematic-review (piloting / keyword exploration — not the formal "
                "search).",
        impact="No impact if you have the Extended key. Piloting without either "
               "key falls back to Scopus-only volume estimation.",
        where="https://developer.clarivate.com — often from the same portal as Extended.",
        verify=_verify_wos_starter,
    ),
    KeySpec(
        "ELSEVIER_API_KEY", "elsevier", "api_key", "Elsevier / ScienceDirect API key",
        required=False, hidden=True,
        what="Elsevier is one of the largest academic publishers; they run "
             "ScienceDirect (their full-text journal site). An Elsevier API key "
             "lets the plugin fetch metadata and open-access / licensed PDFs for "
             "Elsevier journal articles directly.",
        used_by="systematic-review + zotero-operations (ScienceDirect full-text "
                "abstracts and PDFs for Elsevier DOIs, e.g. 10.1016/, 10.1006/).",
        impact="Full-text fetch for Elsevier journals falls back to slower sources. "
               "Metadata and abstracts from other providers still work.",
        where="https://dev.elsevier.com — institutional account usually required.",
        verify=_verify_elsevier,
    ),
    KeySpec(
        "SCOPUS_API_KEY", "scopus", "api_key", "Scopus API key",
        required=False, hidden=True,
        what="Scopus is Elsevier's citation database (the main alternative to Web "
             "of Science). Many institutions issue the same API key for both "
             "Scopus and ScienceDirect; a few issue them separately.",
        used_by="systematic-review (Scopus search via the Elsevier API — "
                "complementary to pybliometrics, which reads its own config file "
                "at ~/.config/pybliometrics.cfg).",
        impact="Direct Scopus search via the plugin's environment-driven path stops "
               "working. pybliometrics-based searches continue independently.",
        where="https://dev.elsevier.com — often the same Elsevier key works for both "
              "Scopus and ScienceDirect; some institutions issue them separately.",
        verify=_verify_scopus,
    ),
    KeySpec(
        "SEMANTIC_SCHOLAR_API_KEY", "semantic_scholar", "api_key",
        "Semantic Scholar API key",
        required=False, hidden=True,
        what="Semantic Scholar is a free AI-powered academic search engine run by "
             "the Allen Institute for AI (https://www.semanticscholar.org). Broad "
             "coverage, open abstracts, and citation graphs — a good free "
             "alternative to Scopus or Web of Science for metadata lookup.",
        used_by="mcp-research, fact-check, systematic-review (abstract lookups, "
                "citation graphs, backup when Crossref lacks an abstract).",
        impact="Requests fall back to the unauthenticated public endpoint with a "
               "much lower rate limit. Skills still work, just more slowly on "
               "large jobs.",
        where="https://www.semanticscholar.org/product/api#api-key-form — free to request.",
        verify=_verify_semantic_scholar,
    ),
    KeySpec(
        "CROSSREF_MAILTO", "crossref", "mailto", "Crossref polite-pool email",
        required=False, hidden=False,
        what="Crossref is the non-profit that registers scholarly DOIs and "
             "maintains the largest open metadata database for academic papers "
             "(https://www.crossref.org). The plugin queries Crossref first when "
             "fetching abstracts. No API key exists — instead, Crossref asks for "
             "an email so they can contact scripts that misbehave; providing one "
             "gets you into their 'polite' rate pool.",
        used_by="systematic-review + zotero-operations (Crossref abstract lookups, "
                "Text and Data Mining endpoints for PDFs).",
        impact="Crossref calls fall to the shared public rate pool — slower and "
               "more likely to be throttled. Not required for correctness.",
        where="Any valid email address; Crossref only uses it as an identifier.",
        verify=_verify_crossref_mailto,
    ),
    KeySpec(
        "WILEY_TDM_TOKEN", "wiley", "tdm_token",
        "Wiley Text and Data Mining token",
        required=False, hidden=True,
        what="Wiley is a major academic publisher. Their Text and Data Mining "
             "service is a dedicated API channel for programmatic PDF download "
             "under institutional text-and-data-mining agreements — cleaner and "
             "more reliable than web scraping. Your institution's librarian "
             "usually requests a token on behalf of researchers.",
        used_by="systematic-review (Wiley PDF retrieval stage).",
        impact="PDFs for Wiley journals (DOI prefixes 10.1002/, 10.1111/, "
               "10.1046/) fall back to a browser-based fetch that handles "
               "Cloudflare manually — slower and more fragile. Other publishers "
               "unaffected.",
        where="Your institution's librarian — issued under your institution's "
              "Wiley text-and-data-mining agreement.",
        verify=_verify_none,
    ),
    KeySpec(
        "OPENALEX_API_KEY", "openalex", "api_key",
        "OpenAlex Content API key (paid tier)",
        required=False, hidden=True,
        what="OpenAlex is a free, open index of scholarly works and authors "
             "(https://openalex.org), the main successor to the shut-down "
             "Microsoft Academic Graph. The free metadata tier is used "
             "extensively and needs no key. The paid Content API ($0.01 per PDF) "
             "unlocks bulk PDF retrieval.",
        used_by="systematic-review (one tier of the multi-source PDF retrieval "
                "cascade).",
        impact="PDF cascade drops one optional tier; the other six sources "
               "(Elsevier, Wiley, Crossref, PubMed Central, Unpaywall, OpenAlex "
               "OA metadata) still function.",
        where="https://openalex.org — paid tier only; skip unless you need "
              "high-volume PDF retrieval.",
        verify=_verify_none,
    ),
)


# ---------------------------------------------------------------------------
# Prompt / collection flow
# ---------------------------------------------------------------------------


def _print_header() -> None:
    print()
    print("=" * 64)
    print("  academic-research plugin — setup wizard")
    print("=" * 64)
    print()
    print("  This will:")
    print("    1. Collect API keys (hidden input) and verify each one")
    print(f"    2. Write {CONFIG_PATH} (mode 0600)")
    print(f"    3. Patch {SETTINGS_PATH} with permission rules")
    print()
    print("  Your keys stay on this machine. They do not pass through")
    print("  Claude's context at any point.")
    print()


def _prompt_key(spec: KeySpec, existing: str | None, interactive: bool,
                verify: bool) -> tuple[str, dict]:
    env_value = os.environ.get(spec.env_var, "").strip()

    if not interactive:
        value = env_value or (existing or "")
        if value and verify:
            ok, _msg, extras = spec.verify(value)
            return value if ok else value, extras if ok else {}
        return value, {}

    default = env_value or (existing or "")
    source = "environment" if env_value else ("existing config" if existing else "")

    required_tag = " [REQUIRED]" if spec.required else " [optional — Enter to skip]"
    default_tag = ""
    if default:
        display = "*" * 8 if spec.hidden else default
        default_tag = f" (from {source}: {display}; press Enter to keep)"

    print(f"\n  {spec.label}{required_tag}{default_tag}")
    print(f"    What it is: {spec.what}")
    print(f"    Used by:    {spec.used_by}")
    print(f"    If missing: {spec.impact}")
    print(f"    Get one at: {spec.where}")

    try:
        if spec.hidden:
            typed = getpass.getpass("    > ").strip()
        else:
            typed = input("    > ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  Aborted.")
        sys.exit(1)

    value = typed or default
    if not value:
        return "", {}

    if not verify:
        return value, {}

    print("    Verifying...", end=" ", flush=True)
    ok, msg, extras = spec.verify(value)
    print(f"{'✓' if ok else '✗'} {msg}")
    if not ok and spec.required:
        retry = input("    Try again with a different key? [Y/n] ").strip().lower()
        if retry in ("", "y", "yes"):
            return _prompt_key(spec, existing, interactive, verify)
        print("    Continuing with unverified key.")
    return value, extras


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


def _collect_keys(interactive: bool, verify: bool) -> dict[str, dict[str, str]]:
    existing = _load_existing_config()
    values: dict[str, dict[str, str]] = {}

    missing_required: list[str] = []
    for spec in KEYS:
        prior = existing.get(spec.toml_section, {}).get(spec.toml_key, "")
        val, extras = _prompt_key(spec, prior, interactive, verify)
        if spec.required and not val:
            missing_required.append(spec.env_var)
            continue
        if val:
            section = values.setdefault(spec.toml_section, {})
            section[spec.toml_key] = val
            for k, v in extras.items():
                if v:
                    section[k] = v

    if missing_required:
        print("\n  Required keys missing: " + ", ".join(missing_required))
        print("  Re-run the wizard and supply these before using the plugin.")
        sys.exit(2)

    return values


# ---------------------------------------------------------------------------
# File writing
# ---------------------------------------------------------------------------


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
    allow_new, deny_new = _permission_patterns()

    if SETTINGS_PATH.exists():
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  ERROR: cannot parse {SETTINGS_PATH}: {e}", file=sys.stderr)
            print("  Back up your settings.json, then re-run the wizard.", file=sys.stderr)
            sys.exit(3)
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
    path = shutil.which("claude")
    if not path:
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
    parser.add_argument(
        "--skip-verify", action="store_true",
        help="Skip online verification of each key (useful offline or for testing).",
    )
    args = parser.parse_args()

    interactive = not args.non_interactive
    verify = not args.skip_verify
    if interactive:
        _print_header()
        env_hits = [k.env_var for k in KEYS if os.environ.get(k.env_var, "").strip()]
        if env_hits:
            print(f"  Detected environment variables: {', '.join(env_hits)}")
            print("  These will be offered as defaults below (press Enter to accept).")
            print()

    values = _collect_keys(interactive, verify)
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
