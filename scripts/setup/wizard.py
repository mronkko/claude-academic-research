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

# MCP server connection statuses, parsed from `claude mcp list` output.
MCP_STATUS_CONNECTED = "connected"
MCP_STATUS_NEEDS_AUTH = "needs_auth"
MCP_STATUS_FAILED = "failed"
MCP_STATUS_UNKNOWN = "unknown"
MCP_STATUS_MISSING = "missing"  # not in `claude mcp list` at all

# Tiers for EXPECTED_MCP (drives summary grouping and banners in main()).
MCP_TIER_REQUIRED = "required"
MCP_TIER_SEARCH_DB = "search_database"
MCP_TIER_OPTIONAL = "optional"

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
        used_by="Every skill that touches citations: grounded-citations, zotero-operations, "
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
        used_by="grounded-citations, fact-check, systematic-review (abstract lookups, "
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
# MCP server registry
#
# The wizard checks five MCP (Model Context Protocol) servers, organised
# in three tiers:
#   - required:        zotero (every citation skill routes through it)
#   - search_database: scopus / semantic-scholar / openalex (at least one
#                      must be connected for literature search to work)
#   - optional:        paper-search (PDF cascade for ArXiv/PubMed/bioRxiv)
#
# Commands and homepages were verified against each project's README.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class McpServerSpec:
    name: str
    purpose: str
    add_args: tuple[str, ...]   # args after `claude mcp add`
    homepage: str
    install_cmd: str            # exact shell command, or "" if auto via npx/uvx
    install_note: str           # extra step or prerequisite
    tier: str                   # MCP_TIER_*


EXPECTED_MCP: tuple[McpServerSpec, ...] = (
    McpServerSpec(
        name="zotero",
        purpose="Reference manager — full-text retrieval, notes, citation keys.",
        add_args=("-s", "user", "zotero", "--", "zotero-mcp"),
        homepage="https://github.com/54yyyu/zotero-mcp",
        install_cmd="uv tool install zotero-mcp-server",
        install_note="After install, run: zotero-mcp setup. "
                     "PyPI alt: pip install zotero-mcp-server.",
        tier=MCP_TIER_REQUIRED,
    ),
    McpServerSpec(
        name="scopus",
        purpose="Elsevier's bibliographic database for systematic-review search.",
        add_args=("-s", "user", "scopus", "--", "scopus-mcp"),
        homepage="https://github.com/qwe4559999/scopus-mcp",
        install_cmd="uv tool install scopus-mcp",
        install_note="PyPI alt: pip install scopus-mcp. "
                     "SCOPUS_API_KEY is read from your shell env.",
        tier=MCP_TIER_SEARCH_DB,
    ),
    McpServerSpec(
        name="semantic-scholar",
        purpose="Free AI-powered academic search with open citation graphs.",
        add_args=("-s", "user", "semantic-scholar", "--",
                  "npx", "-y", "aira-semanticscholar"),
        homepage="https://github.com/hamid-vakilzadeh/AIRA-SemanticScholar",
        install_cmd="",   # auto-installed by npx on first call
        install_note="Requires Node.js + npm. npx downloads the package "
                     "automatically on first use.",
        tier=MCP_TIER_SEARCH_DB,
    ),
    McpServerSpec(
        name="openalex",
        purpose="Open catalog of 240M+ scholarly works, authors, venues.",
        add_args=("-s", "user", "openalex", "--",
                  "npx", "-y", "openalex-research-mcp"),
        homepage="https://github.com/oksure/openalex-research-mcp",
        install_cmd="",
        install_note="Requires Node.js + npm. npx downloads the package "
                     "automatically on first use.",
        tier=MCP_TIER_SEARCH_DB,
    ),
    McpServerSpec(
        name="paper-search",
        purpose="ArXiv / PubMed / bioRxiv discovery and PDF download.",
        add_args=("-s", "user", "paper-search", "--",
                  "uvx", "--from", "paper-search-mcp",
                  "python", "-m", "paper_search_mcp.server"),
        homepage="https://github.com/openags/paper-search-mcp",
        install_cmd="",
        install_note="Requires uv (https://astral.sh/uv). uvx fetches the "
                     "package automatically on first use.",
        tier=MCP_TIER_OPTIONAL,
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


# ---------------------------------------------------------------------------
# Zotero Connector detection (v0.4.0).
#
# The Connector fallback handler (fetchers/browser/connector.py) needs
# the unpacked Zotero Connector extension on disk. We probe the
# per-OS Chrome default-profile location; if nothing is found, the
# wizard prints an install hint and leaves `[zotero_connector]` unset.
# The browser-mode pipeline surfaces a matching error on first use.
# ---------------------------------------------------------------------------

_CONNECTOR_EXT_ID = "ekhagklcjbdpajgpjgmbionohlpdbjgc"


def _connector_probe_paths() -> list[Path]:
    home = Path.home()
    paths = [
        home / "Library" / "Application Support" / "Google" / "Chrome"
        / "Default" / "Extensions" / _CONNECTOR_EXT_ID,
        home / ".config" / "google-chrome" / "Default"
        / "Extensions" / _CONNECTOR_EXT_ID,
    ]
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        paths.append(
            Path(local_appdata) / "Google" / "Chrome" / "User Data"
            / "Default" / "Extensions" / _CONNECTOR_EXT_ID,
        )
    return paths


def _resolve_connector_path(base: Path) -> Path | None:
    """Highest-versioned subdir under the extension base (or the base
    itself when it already contains `manifest.json`)."""
    if not base.exists():
        return None
    if (base / "manifest.json").exists():
        return base
    try:
        subs = [d for d in base.iterdir() if d.is_dir()]
    except OSError:
        return None
    if not subs:
        return None
    subs.sort(key=lambda p: p.name)
    return subs[-1]


def _detect_and_prompt_connector(
    interactive: bool,
    existing: dict,
) -> dict[str, object]:
    """Return `{extension_dir: "..."}` to merge into values, or `{}`.

    Picks up an existing `[zotero_connector] extension_dir` from the
    config and offers it first. When detecting freshly, probes the
    platform defaults and asks the user to confirm.
    """
    existing_dir = (existing.get("zotero_connector", {}) or {}).get(
        "extension_dir", ""
    )
    if existing_dir and (Path(existing_dir) / "manifest.json").exists():
        return {"extension_dir": existing_dir}

    detected = None
    for base in _connector_probe_paths():
        detected = _resolve_connector_path(base)
        if detected is not None:
            break

    if detected is None:
        if interactive:
            print("\n  Zotero Connector (optional fallback for library-only PDFs):")
            print(
                "  The Zotero Connector Chrome extension was not detected.\n"
                "  Install it from:\n"
                "    https://www.zotero.org/download/connectors/\n"
                "  (use Google Chrome, not Chrome for Testing). Re-run this\n"
                "  wizard afterwards so the plugin can locate the extension.",
            )
        return {}

    if not interactive:
        return {"extension_dir": str(detected)}

    print("\n  Zotero Connector (for library-routed PDFs via EBSCO/JSTOR/…):")
    print(f"    Detected extension at: {detected}")
    answer = input("    Use this? [Y/n] ").strip().lower()
    if answer in ("", "y", "yes"):
        return {"extension_dir": str(detected)}
    return {}


# ---------------------------------------------------------------------------
# [library] no_access editor (v0.4.0).
#
# The runtime failure prompt appends publisher names to this list.
# The wizard is the undo path: it shows the current list and lets
# the user delete entries. The wizard does NOT ask "can you access X
# directly?" because the user can't reliably answer — access is
# usually library-mediated, not a personal subscription.
# ---------------------------------------------------------------------------


def _offer_no_access_editor(
    interactive: bool,
    existing: dict,
) -> list[str]:
    """Return the updated `[library] no_access` list.

    Only mutates on explicit user request. Unchanged when the user
    just presses Enter, or on non-interactive runs.
    """
    current_raw = (existing.get("library", {}) or {}).get("no_access", [])
    if isinstance(current_raw, list):
        current = [str(s).strip() for s in current_raw if s]
    elif isinstance(current_raw, str):
        current = [s.strip() for s in current_raw.split(",") if s.strip()]
    else:
        current = []

    if not interactive:
        return current

    print("\n  Publishers currently set to skip direct-access attempts:")
    if not current:
        print("    (none — direct handlers are tried for every publisher.")
        print("     If one consistently fails during a run, the pipeline")
        print("     will prompt you to opt out.)")
        return current

    for i, name in enumerate(current, 1):
        print(f"    {i}. {name}")
    print(
        "  Remove any from this list? Enter numbers separated by spaces,\n"
        "  or press Enter to keep all.",
    )
    raw = input("    > ").strip()
    if not raw:
        return current

    try:
        indices = {int(tok) for tok in raw.split() if tok}
    except ValueError:
        print("    (could not parse — leaving the list unchanged.)")
        return current

    keep = [name for i, name in enumerate(current, 1) if i not in indices]
    removed = [name for i, name in enumerate(current, 1) if i in indices]
    if removed:
        print(f"    Removed: {', '.join(removed)}")
    return keep


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


def _collect_keys(
    interactive: bool, verify: bool,
) -> dict[str, dict[str, object]]:
    existing = _load_existing_config()
    values: dict[str, dict[str, object]] = {}

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


def _escape_toml(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _render_toml_value(val: object) -> str:
    """Render one TOML value. Strings and lists-of-strings only — the
    two shapes the plugin writes. Added for v0.4.0's
    `[library] no_access` list support."""
    if isinstance(val, list):
        inner = ", ".join(f'"{_escape_toml(str(v))}"' for v in val)
        return f"[{inner}]"
    return f'"{_escape_toml(str(val))}"'


def _write_config(values: dict[str, dict]) -> None:
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
            lines.append(f"{key} = {_render_toml_value(val)}")
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
    # Deny patterns for the config file. Claude Code's permission matcher
    # is prefix-based, so we enumerate the common shapes (absolute path,
    # tilde, with/without redirects). Not exhaustive — reading `~/.claude/`
    # config via an obscure tool (xxd, od, strings, inline python) can slip
    # through. The skill-level "never read config.toml" red flags are the
    # first line of defence; these deny patterns are belt-and-suspenders.
    config_abs = f"{home}/.config/academic-research/config.toml"
    config_tilde = "~/.config/academic-research/config.toml"
    deny_paths = [config_abs, config_tilde]
    deny = [
        f"Read({absolute_home_pattern}/.config/academic-research/config.toml)",
        "Read(~/.config/academic-research/config.toml)",
    ]
    for path in deny_paths:
        for cmd in ("cat", "head", "tail", "grep", "less", "more",
                    "awk", "sed", "od", "xxd", "strings", "bat"):
            deny.append(f"Bash({cmd} {path}:*)")
            deny.append(f"Bash({cmd} {path})")
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


def _parse_mcp_list(stdout: str) -> dict[str, str]:
    """Parse `claude mcp list` output into {name: status}.

    Each interesting line has the shape:
        <name>: <command-or-url> - <status-emoji> <status-text>
    e.g.:
        zotero: zotero-mcp  - ✓ Connected
        scopus: scopus-mcp  - ! Needs authentication
        openalex: npx -y openalex-research-mcp - ✗ Failed

    Built-in claude.ai servers ("claude.ai Google Calendar: …") have a
    space in the name and are skipped — they are not in EXPECTED_MCP.
    """
    out: dict[str, str] = {}
    for raw in stdout.splitlines():
        line = raw.rstrip()
        if not line or line.startswith(" "):
            continue
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        name = name.strip()
        # Skip "claude.ai Google Calendar"-style built-ins (have whitespace
        # in the name) and any non-name junk lines.
        if not name or " " in name:
            continue

        lowered = rest.lower()
        if "✓" in rest or "connected" in lowered:
            status = MCP_STATUS_CONNECTED
        elif "needs authentication" in lowered or "needs auth" in lowered:
            status = MCP_STATUS_NEEDS_AUTH
        elif "✗" in rest or "failed" in lowered or "error" in lowered:
            status = MCP_STATUS_FAILED
        else:
            status = MCP_STATUS_UNKNOWN
        out[name] = status
    return out


ZOTERO_LOCAL_URL = "http://localhost:23119/api/"
ZOTERO_LOCAL_STATUS_OK = "ok"
ZOTERO_LOCAL_STATUS_NOT_RUNNING = "not_running"
ZOTERO_LOCAL_STATUS_SERVER_DISABLED = "server_disabled"

ZOTERO_BBT_URL = "http://localhost:23119/better-bibtex/json-rpc"
ZOTERO_BBT_STATUS_OK = "ok"
ZOTERO_BBT_STATUS_MISSING = "missing"
ZOTERO_BBT_STATUS_UNREACHABLE = "unreachable"


def _check_zotero_local(timeout: int = 3) -> tuple[str, str]:
    """Probe the local Zotero HTTP API at localhost:23119/api/.

    Returns (status, message) where status is one of:
      - "ok"               : HTTP 200 — Zotero is running, local API on.
      - "server_disabled"  : Connection refused — Zotero is running but
                             hasn't opened the local server port.
      - "not_running"      : Connection refused OR DNS/timeout — most
                             likely Zotero desktop is not running at all.
                             Without extra probes we can't always tell
                             these two apart, so the message covers both.

    The message is a one-line human summary suitable for the final
    summary block.
    """
    status, _, err = _http_json(ZOTERO_LOCAL_URL, timeout=timeout)
    if status == 200:
        return ZOTERO_LOCAL_STATUS_OK, "reachable at localhost:23119"
    # status=0 from _http_json means the connection itself failed
    # (refused, timeout, DNS). We can't distinguish "Zotero not running"
    # from "Zotero running but local server off" without a second probe,
    # so we merge them into a single actionable status.
    return ZOTERO_LOCAL_STATUS_NOT_RUNNING, err or f"HTTP {status}"


def _print_zotero_local_help() -> None:
    """Print the actionable message when the local Zotero API is unreachable.

    Pipeline scripts that call ZoteroClient with prefer_local=True (the
    default) need this endpoint. Without it, every read falls back to
    api.zotero.org — slow and rate-limited for large libraries.
    """
    print("  *** WARNING: local Zotero API is not reachable ***")
    print("  Pipeline scripts default to local reads (fast, no rate limit).")
    print("  Without it, reads fall back to api.zotero.org — much slower.")
    print()
    print("  To fix:")
    print("  1. Open Zotero desktop (download: https://www.zotero.org/download/).")
    print("  2. Zotero → Settings → Advanced → General:")
    print("     tick 'Allow other applications on this computer to communicate")
    print("     with Zotero'.")
    print("  3. Leave Zotero running; re-run this wizard to confirm.")


def _check_zotero_bbt(timeout: int = 3) -> tuple[str, str]:
    """Probe the Better BibTeX JSON-RPC endpoint.

    BBT is a Zotero plugin — separate from Zotero itself — that pipeline
    scripts (`generate_bib.py`) and the `grounded-citations` rule both
    depend on for citation keys. A missing BBT breaks both.

    Behaviour on a bare GET against the JSON-RPC URL:
      - 4xx other than 404 (e.g. 400, 405): endpoint exists, BBT is
        installed — the server rejected our GET because the endpoint
        expects POST, but that's fine, we only wanted to know it exists.
      - 404: Zotero is up but BBT is not installed.
      - status 0 (connection failure): Zotero itself is unreachable —
        `_check_zotero_local` already surfaces the actionable message.

    Returns (status, message) mirroring `_check_zotero_local`.
    """
    status, _, err = _http_json(ZOTERO_BBT_URL, timeout=timeout)
    if status == 0:
        return ZOTERO_BBT_STATUS_UNREACHABLE, err or "Zotero not reachable"
    if status == 404:
        return ZOTERO_BBT_STATUS_MISSING, "Better BibTeX plugin not installed"
    return ZOTERO_BBT_STATUS_OK, "Better BibTeX JSON-RPC reachable"


def _print_zotero_bbt_help() -> None:
    """Print the actionable message when Better BibTeX is missing.

    BBT is an XPI plugin that users install into Zotero; it's not
    bundled with Zotero itself. The `grounded-citations` rule requires
    BBT keys, and `generate_bib.py` exports `references.bib` via BBT's
    JSON-RPC.
    """
    print("  *** WARNING: Better BibTeX is not installed in Zotero ***")
    print("  The grounded-citations rule needs BBT citation keys, and")
    print("  generate_bib.py exports references.bib via BBT's JSON-RPC.")
    print("  Without BBT, neither works.")
    print()
    print("  To fix:")
    print("  1. Download the latest BBT .xpi from:")
    print("     https://github.com/retorquere/zotero-better-bibtex/releases/latest")
    print("     (under 'Assets', grab the .xpi file — not the source tarballs).")
    print("  2. In Zotero: Tools → Add-ons → gear icon →")
    print("     'Install Add-on From File…' → pick the .xpi.")
    print("  3. Restart Zotero.")
    print("  4. Re-run this wizard to confirm.")


def _check_mcp_servers() -> dict[str, str]:
    """Run `claude mcp list` and return {name: status}.

    Fail-open: returns {} if the `claude` CLI is missing or the call
    fails for any reason. Callers must treat an empty dict as "unknown",
    not "everything is missing".
    """
    if not shutil.which("claude"):
        return {}
    import subprocess
    try:
        result = subprocess.run(
            ["claude", "mcp", "list"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return {}
        return _parse_mcp_list(result.stdout)
    except Exception:
        return {}


def _format_register_command(spec: McpServerSpec) -> str:
    """Render the `claude mcp add ...` command as a copy-pasteable string."""
    return "claude mcp add " + " ".join(spec.add_args)


def _print_mcp_offer(spec: McpServerSpec, status: str) -> None:
    if status == MCP_STATUS_MISSING:
        headline = f"{spec.name} — not registered"
    elif status == MCP_STATUS_NEEDS_AUTH:
        headline = f"{spec.name} — registered but needs authentication"
    elif status == MCP_STATUS_FAILED:
        headline = f"{spec.name} — registered but failed to connect"
    else:
        headline = f"{spec.name} — status: {status}"

    install_line = (
        spec.install_cmd if spec.install_cmd
        else "(auto-installed on first use; no separate install command)"
    )

    print(f"\n  {headline}")
    print(f"    What it is: {spec.purpose}")
    print(f"    Project:    {spec.homepage}")
    print(f"    Install:    {install_line}")
    if spec.install_note:
        print(f"                {spec.install_note}")
    print(f"    Register:   {_format_register_command(spec)}")


def _run_claude_mcp(args: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """Run a `claude mcp <args>` command. Returns (returncode, stdout, stderr)."""
    import subprocess
    try:
        result = subprocess.run(
            ["claude", "mcp", *args],
            capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except FileNotFoundError as e:
        return 127, "", str(e)
    except Exception as e:
        return 1, "", str(e)


_MISSING_BINARY_HINTS = (
    "command not found", "no such file", "enoent",
    "is not recognized", "executable not found",
)


def _looks_like_missing_binary(stderr: str) -> bool:
    s = stderr.lower()
    return any(hint in s for hint in _MISSING_BINARY_HINTS)


def _offer_register_mcp(
    specs: tuple[McpServerSpec, ...],
    current: dict[str, str],
    interactive: bool,
) -> tuple[int, dict[str, str]]:
    """For each spec not currently connected, offer to register it.

    Returns (registered_count, updated_status_map). The status map is
    `current` augmented with any servers we successfully registered
    (status = "connected" once `claude mcp add` returns 0). On failure
    we fall back to MCP_STATUS_MISSING / FAILED.

    In non-interactive mode we don't prompt or call `claude mcp add` —
    we just return the current map unchanged so the summary can report
    the state.
    """
    updated = dict(current)
    if not interactive:
        return 0, updated
    if not shutil.which("claude"):
        return 0, updated

    registered = 0
    for spec in specs:
        status = current.get(spec.name, MCP_STATUS_MISSING)
        if status == MCP_STATUS_CONNECTED:
            continue

        _print_mcp_offer(spec, status)

        if status == MCP_STATUS_MISSING:
            prompt = "    Register now? [Y/n] "
        else:
            prompt = "    Re-register now (will replace the existing entry)? [Y/n] "

        try:
            answer = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            print("    Skipped (input ended).")
            continue

        if answer not in ("", "y", "yes"):
            print("    Skipped.")
            continue

        # If already registered (needs_auth/failed), remove first so the
        # add doesn't fail with "already exists".
        if status in (MCP_STATUS_NEEDS_AUTH, MCP_STATUS_FAILED, MCP_STATUS_UNKNOWN):
            rc, _out, err = _run_claude_mcp(["remove", spec.name, "-s", "user"])
            if rc != 0 and "not found" not in err.lower():
                print(f"    ✗ Could not remove existing {spec.name}: {err.strip() or 'unknown error'}")
                continue

        rc, _out, err = _run_claude_mcp(["add", *spec.add_args])
        if rc == 0:
            print(f"    ✓ Registered {spec.name}.")
            updated[spec.name] = MCP_STATUS_CONNECTED
            registered += 1
            if spec.name == "zotero":
                print("    Note: for local-mode (Zotero desktop instead of cloud), "
                      "re-run with `-e ZOTERO_LOCAL=true` — see the project page.")
        else:
            err_clean = err.strip() or "unknown error"
            print(f"    ✗ Registration failed: {err_clean}")
            if _looks_like_missing_binary(err) and spec.install_cmd:
                print("    The required command isn't on your PATH.")
                print(f"    Install it with: {spec.install_cmd}")
                if spec.install_note:
                    print(f"                    {spec.install_note}")
                print("    Then re-run this wizard.")
            updated[spec.name] = updated.get(spec.name, MCP_STATUS_MISSING)

    return registered, updated


def _print_mcp_summary(current: dict[str, str]) -> tuple[bool, bool]:
    """Print the tiered MCP summary block.

    Returns (zotero_missing, all_search_dbs_missing) so main() can emit
    the appropriate banners and exit code.
    """
    by_tier: dict[str, list[McpServerSpec]] = {
        MCP_TIER_REQUIRED: [],
        MCP_TIER_SEARCH_DB: [],
        MCP_TIER_OPTIONAL: [],
    }
    for spec in EXPECTED_MCP:
        by_tier[spec.tier].append(spec)

    tier_labels = {
        MCP_TIER_REQUIRED: "Required:",
        MCP_TIER_SEARCH_DB: "Citation databases (at least one needed for literature search):",
        MCP_TIER_OPTIONAL: "Optional:",
    }

    status_glyphs = {
        MCP_STATUS_CONNECTED: "✓ connected",
        MCP_STATUS_NEEDS_AUTH: "! needs authentication",
        MCP_STATUS_FAILED: "✗ failed to connect",
        MCP_STATUS_UNKNOWN: "? unknown status",
        MCP_STATUS_MISSING: "✗ not registered",
    }

    print("    MCP servers")
    name_width = max(len(s.name) for s in EXPECTED_MCP)
    for tier in (MCP_TIER_REQUIRED, MCP_TIER_SEARCH_DB, MCP_TIER_OPTIONAL):
        print(f"      {tier_labels[tier]}")
        for spec in by_tier[tier]:
            status = current.get(spec.name, MCP_STATUS_MISSING)
            glyph = status_glyphs.get(status, status_glyphs[MCP_STATUS_UNKNOWN])
            print(f"        {spec.name:<{name_width}}  {glyph}")
            if status != MCP_STATUS_CONNECTED:
                if spec.install_cmd:
                    print(f"          Install:  {spec.install_cmd}")
                else:
                    print("          Install:  (auto via npx/uvx — see project page)")
                print(f"          Project:  {spec.homepage}")

    zotero_status = current.get("zotero", MCP_STATUS_MISSING)
    zotero_missing = zotero_status != MCP_STATUS_CONNECTED

    search_dbs = [s.name for s in EXPECTED_MCP if s.tier == MCP_TIER_SEARCH_DB]
    all_search_dbs_missing = all(
        current.get(name) != MCP_STATUS_CONNECTED for name in search_dbs
    )

    return zotero_missing, all_search_dbs_missing


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

    # Preserve (or extend) non-key sections across re-runs.
    existing_cfg = _load_existing_config()

    connector_entry = _detect_and_prompt_connector(interactive, existing_cfg)
    if connector_entry:
        values["zotero_connector"] = connector_entry

    updated_no_access = _offer_no_access_editor(interactive, existing_cfg)
    if updated_no_access:
        values.setdefault("library", {})["no_access"] = updated_no_access

    _write_config(values)
    allow_added, deny_added = _patch_settings()

    # Local Zotero API probe. Pipeline scripts default to local reads for
    # speed; failing here doesn't block setup but surfaces a clear warning.
    zotero_local_status, zotero_local_message = _check_zotero_local()
    # Better BibTeX is a separate plugin — skip the probe if Zotero itself
    # isn't up, since that would just duplicate the Zotero-local warning.
    if zotero_local_status == ZOTERO_LOCAL_STATUS_OK:
        zotero_bbt_status, zotero_bbt_message = _check_zotero_bbt()
    else:
        zotero_bbt_status, zotero_bbt_message = (
            ZOTERO_BBT_STATUS_UNREACHABLE,
            "skipped — Zotero local API not reachable",
        )

    current_mcp = _check_mcp_servers()
    if interactive:
        print()
        print("  Checking MCP servers (Model Context Protocol — provides Claude")
        print("  with tools for Zotero, citation databases, and PDF retrieval).")
        registered, current_mcp = _offer_register_mcp(
            EXPECTED_MCP, current_mcp, interactive=True,
        )
        if registered:
            # Re-poll so the final summary reflects post-registration state.
            current_mcp = _check_mcp_servers() or current_mcp

    print()
    print("  Setup complete.")
    print(f"    Config:   {CONFIG_PATH} (mode 0600)")
    print(f"    Settings: {SETTINGS_PATH} (+{allow_added} allow, +{deny_added} deny)")
    glyph = "✓" if zotero_local_status == ZOTERO_LOCAL_STATUS_OK else "✗"
    print(f"    Zotero local API: {glyph} {zotero_local_message}")
    bbt_glyph = "✓" if zotero_bbt_status == ZOTERO_BBT_STATUS_OK else "✗"
    print(f"    Better BibTeX:    {bbt_glyph} {zotero_bbt_message}")
    zotero_missing, all_search_dbs_missing = _print_mcp_summary(current_mcp)

    if zotero_local_status != ZOTERO_LOCAL_STATUS_OK:
        print()
        _print_zotero_local_help()
    elif zotero_bbt_status == ZOTERO_BBT_STATUS_MISSING:
        print()
        _print_zotero_bbt_help()

    if zotero_missing:
        print()
        print("  *** REQUIRED: Zotero MCP is not connected. ***")
        print("  Every academic-research skill routes through Zotero.")
        print("  Install and register it (see the Install/Project lines above),")
        print("  then re-run this wizard. The wizard is idempotent.")
    if all_search_dbs_missing:
        print()
        print("  *** WARNING: no citation database is reachable. ***")
        print("  Literature search will not work without at least one of:")
        print("  scopus, semantic-scholar, openalex. Other skills (e.g.")
        print("  critic-loop, fact-check on existing items) still work.")

    print()
    print("  Return to your Claude Code session and tell Claude setup is done.")
    print()
    return 4 if zotero_missing else 0


if __name__ == "__main__":
    sys.exit(main())
