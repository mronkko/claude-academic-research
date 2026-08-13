#!/usr/bin/env python3
"""Interactive setup wizard for the academic-research plugin.

Runs in the user's terminal. Prompts for API keys with hidden input
(getpass), tests each key against its provider, writes
~/.config/academic-research/config.toml mode 0600, patches
~/.claude/settings.json with the permission rules the plugin needs,
and reports status.

API keys entered here NEVER pass through the invoking AI assistant's
context (Claude Code or Antigravity) — the wizard is a normal process
reading the terminal directly. The assistant only sees the final
summary line.

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
import random
import re
import shutil
import sys
import textwrap
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

# Sibling import. Running wizard.py as a script puts its directory on
# sys.path[0] automatically, but loading it by path (as the unit tests do,
# via importlib.util.spec_from_file_location) does not.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
# `core.providers` is the single registry of LLM providers. Importing it
# rather than re-listing the providers here is what keeps the wizard's
# menu and the runtime router from drifting apart. It is stdlib-only, so
# the `scripts/setup/` no-third-party rule still holds.
_SCRIPTS_ROOT = _HERE.parent
if str(_SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_ROOT))

from core import providers  # noqa: E402
from zotero_mcp_floor import (  # noqa: E402
    PIP_INSTALL_CMD as ZOTERO_MCP_PIP_INSTALL_CMD,
)
from zotero_mcp_floor import (  # noqa: E402
    UV_INSTALL_CMD as ZOTERO_MCP_INSTALL_CMD,
)

CONFIG_DIR = Path.home() / ".config" / "academic-research"
CONFIG_PATH = CONFIG_DIR / "config.toml"
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

AGY_HOME = Path.home() / ".gemini"
AGY_MCP_CONFIG_PATH = AGY_HOME / "config" / "mcp_config.json"

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


# Retry policy for `_http_json`. Deliberately hand-rolled rather than
# reusing `pipelines/http_client.py`: skills invoke this file as
# `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/setup/wizard.py` with no `uv`
# and no venv, so `scripts/setup/` must stay stdlib-only. `requests`,
# `urllib3`, and `tenacity` are not importable here.
_RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3
_BASE_DELAY_S = 1.0
_MAX_DELAY_S = 8.0


def _retry_after_seconds(value: str | None) -> float | None:
    """Parse a `Retry-After` header value into seconds.

    Only the delta-seconds form is handled; the HTTP-date form returns
    None so the caller falls back to exponential backoff. Providers that
    throttle key-verification calls (Semantic Scholar, Crossref) all use
    delta-seconds.
    """
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        return None


def _backoff_delay(attempt: int, retry_after: float | None) -> float:
    """Seconds to wait before `attempt` (1-based), with full jitter.

    `Retry-After` wins when the server sent one. Otherwise exponential
    from `_BASE_DELAY_S`, capped, with jitter so a wizard run and a
    concurrent pipeline run don't retry in lockstep.
    """
    if retry_after is not None:
        return min(retry_after, _MAX_DELAY_S)
    ceiling = min(_BASE_DELAY_S * (2 ** (attempt - 1)), _MAX_DELAY_S)
    return random.uniform(0.0, ceiling)  # noqa: S311  # jitter, not crypto


def _http_json(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: int = 10,
) -> tuple[int, dict | None, str]:
    """Plain urllib GET returning (status, json_or_none, error_message).

    Retries transient failures — 408/429/5xx and transport errors — with
    exponential backoff and jitter, honouring `Retry-After`. Auth
    failures (401/403) and other 4xx are returned on the first attempt:
    they are answers, not blips, and the caller reports them to the user.
    """
    req = urllib.request.Request(url, headers=headers or {})
    status, data, err = 0, None, ""
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        retry_after: float | None = None
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                body = response.read()
                try:
                    return response.status, json.loads(body), ""
                except json.JSONDecodeError:
                    return response.status, None, "non-JSON response"
        except urllib.error.HTTPError as e:
            status, data, err = e.code, None, f"{e.code} {e.reason}"
            if e.code not in _RETRY_STATUSES:
                return status, data, err
            retry_after = _retry_after_seconds(e.headers.get("Retry-After"))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            status, data, err = 0, None, str(e)
        if attempt < _MAX_ATTEMPTS:
            time.sleep(_backoff_delay(attempt, retry_after))
    return status, data, err


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


def _verify_gemini(key: str) -> tuple[bool, str, dict]:
    status, data, err = _http_json(
        f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
    )
    if status == 0:
        return False, f"could not reach generativelanguage.googleapis.com ({err}) — saved anyway", {}
    if status in (400, 403, 401):
        return False, "Gemini API rejected the key. Re-check the key.", {}
    if status != 200:
        return False, f"Gemini returned HTTP {status}", {}
    return True, "key valid; Gemini API reachable", {}



def _verify_openai(key: str) -> tuple[bool, str, dict]:
    status, data, err = _http_json(
        "https://api.openai.com/v1/models",
        headers={"Authorization": f"Bearer {key}"},
    )
    if status == 0:
        return False, f"could not reach api.openai.com ({err}) — saved anyway", {}
    if status in (401, 403):
        return False, "OpenAI rejected the key (401/403). Re-check it.", {}
    if status != 200:
        return False, f"OpenAI returned HTTP {status}", {}
    n = len((data or {}).get("data") or [])
    return True, f"key valid; {n} models visible to this account", {}


def _verify_openrouter(key: str) -> tuple[bool, str, dict]:
    status, data, err = _http_json(
        "https://openrouter.ai/api/v1/models",
        headers={"Authorization": f"Bearer {key}"},
    )
    if status == 0:
        return False, f"could not reach openrouter.ai ({err}) — saved anyway", {}
    if status in (401, 403):
        return False, "OpenRouter rejected the key (401/403). Re-check it.", {}
    if status != 200:
        return False, f"OpenRouter returned HTTP {status}", {}
    n = len((data or {}).get("data") or [])
    return True, f"key valid; {n} models available via OpenRouter", {}


def _verify_local_endpoint(
    url: str, path: str, label: str, payload_key: str,
) -> tuple[bool, str, dict]:
    """Probe a local model server's listing endpoint.

    Local providers have no credential, so reachability *is* the
    verification — and "the server isn't running" or "no model is
    loaded" are the two failures these users actually hit. Both are
    worth catching at setup time rather than in the middle of a
    screening run.
    """
    status, data, err = _http_json(
        f"{url.rstrip('/')}{path}", timeout=5,
    )
    if status == 0:
        return False, (
            f"nothing answered at {url} ({err}). Start {label} and re-run, "
            f"or leave this blank if you are not using it."
        ), {}
    if status != 200:
        return False, f"{label} returned HTTP {status} from {path}", {}
    models = (data or {}).get(payload_key) or []
    if not models:
        return True, f"{label} is running but has no models loaded yet", {}
    return True, f"{label} reachable; {len(models)} model(s) available", {}


def _verify_ollama_base_url(url: str) -> tuple[bool, str, dict]:
    return _verify_local_endpoint(url, "/api/tags", "Ollama", "models")


def _verify_lmstudio_base_url(url: str) -> tuple[bool, str, dict]:
    return _verify_local_endpoint(url, "/v1/models", "LM Studio", "data")


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
    #: Name in `core.providers` when this key configures one LLM provider.
    #: Such a spec is only prompted for when that provider is the chosen
    #: one — a new user should answer one credential question, not six.
    #: Empty means "always ask": every non-LLM key here is used whatever
    #: the screening pipelines run on.
    llm_provider: str = ""


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
        required=False, hidden=True,
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
        llm_provider="anthropic",
    ),
    KeySpec(
        "ANTHROPIC_BASE_URL", "anthropic", "base_url",
        "Anthropic-compatible endpoint URL",
        required=False, hidden=False,
        what="An alternative endpoint speaking the Anthropic Messages API, "
             "instead of api.anthropic.com. Open WebUI and LM Studio both "
             "expose one, so this is how you point screening at a locally "
             "hosted model. Not a secret: a plain URL, safe to paste in view. "
             "Leave blank to use Anthropic's own API.",
        used_by="systematic-review (abstract screening and full-text coding, "
                "via the --model flag on abstract_screen.py / "
                "fulltext_code.py).",
        impact="None if blank — the pipelines call api.anthropic.com as "
               "usual. When set, ANTHROPIC_API_KEY becomes optional, since "
               "local endpoints generally do not check it.",
        where="LM Studio: Developer tab → server URL (default "
              "http://localhost:1234). Open WebUI: your instance's base URL. "
              "Give the base only — the SDK appends /v1/messages itself.",
        verify=_verify_none,
        llm_provider="anthropic",
    ),
    KeySpec(
        "GEMINI_API_KEY", "gemini", "api_key", "Gemini API key",
        required=False, hidden=True,
        what="Google is the company that builds Gemini. This API key lets the "
             "plugin's screening and coding scripts call Gemini directly — separate "
             "from Antigravity's own Google sign-in.",
        used_by="systematic-review (Gemini-driven abstract screening, full-text "
                "screening, and structured coding of included papers).",
        impact="Systematic-review screening pipelines will fail if you configure them "
               "to use Gemini models and do not provide this key.",
        where="https://aistudio.google.com/app/apikey",
        verify=_verify_gemini,
        llm_provider="google",
    ),
    KeySpec(
        "OPENAI_API_KEY", "openai", "api_key", "OpenAI API key",
        required=False, hidden=True,
        what="OpenAI builds the GPT models. This API key lets the plugin's "
             "screening and coding scripts call them directly.",
        used_by="systematic-review (abstract screening, full-text screening, "
                "and structured coding of included papers).",
        impact="Screening pipelines will fail while OpenAI is your selected "
               "provider. Skills that use your interactive assistant "
               "session instead — critic-loop, fact-check — are unaffected.",
        where="https://platform.openai.com/api-keys",
        verify=_verify_openai,
        llm_provider="openai",
    ),
    KeySpec(
        "OPENAI_BASE_URL", "openai", "base_url",
        "OpenAI-compatible endpoint URL",
        required=False, hidden=False,
        what="An alternative endpoint speaking the OpenAI "
             "/v1/chat/completions API instead of api.openai.com — vLLM, "
             "LiteLLM, a university gateway, or any other compatible "
             "server. Not a secret: a plain URL, safe to paste in view. "
             "Leave blank to use OpenAI's own API.",
        used_by="systematic-review (abstract screening and full-text "
                "coding, whenever OpenAI is the selected provider).",
        impact="None if blank — the pipelines call api.openai.com as "
               "usual. When set, OPENAI_API_KEY may still be required, "
               "depending on what your gateway checks.",
        where="Your gateway's documentation. Give the base only — the SDK "
              "appends /v1/chat/completions itself.",
        verify=_verify_none,
        llm_provider="openai",
    ),
    KeySpec(
        "OPENROUTER_API_KEY", "openrouter", "api_key", "OpenRouter API key",
        required=False, hidden=True,
        what="OpenRouter (https://openrouter.ai) is a single API in front "
             "of models from many vendors — Anthropic, Google, OpenAI, "
             "Meta and others. One key, one bill, and a way to compare "
             "models without opening an account per vendor.",
        used_by="systematic-review (abstract screening, full-text "
                "screening, and structured coding of included papers).",
        impact="Screening pipelines will fail while OpenRouter is your "
               "selected provider.",
        where="https://openrouter.ai/keys",
        verify=_verify_openrouter,
        llm_provider="openrouter",
    ),
    KeySpec(
        "OLLAMA_BASE_URL", "ollama", "base_url", "Ollama server URL",
        required=False, hidden=False,
        what="Ollama (https://ollama.com) runs open-weight models on your "
             "own machine. No API key and no per-paper cost — screening "
             "thousands of abstracts costs electricity rather than "
             "dollars. Not a secret: a plain URL, safe to paste in view.",
        used_by="systematic-review (abstract screening and full-text "
                "coding, whenever Ollama is the selected provider).",
        impact=f"Blank means the default, {providers.BY_NAME['ollama'].default_base_url}, "
               f"which is where Ollama listens unless you changed it. Set "
               f"this only if you moved the port or run it on another host.",
        where="Ollama serves on http://localhost:11434 by default. Check "
              "with `ollama list`; if that works, the default URL is right.",
        verify=_verify_ollama_base_url,
        llm_provider="ollama",
    ),
    KeySpec(
        "LMSTUDIO_BASE_URL", "lmstudio", "base_url", "LM Studio server URL",
        required=False, hidden=False,
        what="LM Studio (https://lmstudio.ai) is a desktop app for running "
             "open-weight models locally, with an OpenAI-compatible "
             "server built in. No API key and no per-paper cost. Not a "
             "secret: a plain URL, safe to paste in view.",
        used_by="systematic-review (abstract screening and full-text "
                "coding, whenever LM Studio is the selected provider).",
        impact=f"Blank means the default, {providers.BY_NAME['lmstudio'].default_base_url}, "
               f"which is where LM Studio listens unless you changed it.",
        where="LM Studio → Developer tab → Start Server; the URL is shown "
              "there (default http://localhost:1234). Load a model before "
              "running a screening pass — an idle server has none.",
        verify=_verify_lmstudio_base_url,
        llm_provider="lmstudio",
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
    KeySpec(
        "LIBRARY_OPENURL_BASE", "library", "openurl_base",
        "Library link-resolver base URL",
        required=False, hidden=False,
        what="Your institutional library's link-resolver endpoint — either an "
             "SFX/OpenURL base URL, or (for Ex Libris Alma/Primo libraries, now "
             "the majority) the Alma `uresolver` URL. Not an API key: a plain "
             "URL, safe to paste in view.",
        used_by="Browser-fetch PDF stage (zotero-operations, systematic-review): "
                "a pre-flight check that skips items your library has no "
                "full-text route to, before opening a browser for them.",
        impact="No pre-flight check — the browser handler tries every item, "
               "including ones with no library access, which is slower but not "
               "broken. Every other skill is unaffected.",
        where="SFX: your library's existing OpenURL/citation-manager "
              "documentation, or ask your library. Alma: open a Primo VE "
              "\"Get it\"/\"View it\" link for any item and read the outbound "
              "request in your browser's Network tab — it's the "
              "`.../view/uresolver/<inst_code>/openurl` URL up to the `?`. "
              "Your library's electronic-resources staff usually has this "
              "handy (it's shared for LibKey Nomad / Lean Library / similar "
              "integrations).",
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
        add_args=("-s", "user", "zotero",
                  "-e", "ZOTERO_MCP_TOOLSETS=libraries,search-admin,pdf-geometry,duplicates,scite",
                  "--", "zotero-mcp"),
        homepage="https://github.com/mronkko/zotero-mcp",
        install_cmd=ZOTERO_MCP_INSTALL_CMD,
        install_note="Installs the [scite,semantic] extras: scite powers the "
                     "retraction-check step the systematic-review and "
                     "zotero-operations skills run; semantic enables semantic "
                     "library search. After install, run: zotero-mcp setup. "
                     f"PyPI alt: {ZOTERO_MCP_PIP_INSTALL_CMD}. "
                     "ZOTERO_MCP_TOOLSETS above adds duplicates/scite to the "
                     "package's own default profile (libraries, search-admin, "
                     "pdf-geometry) — dropping the env var narrows back to just "
                     "those three.",
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


# Hard-cap output at 80 columns — the wizard is often read in the side panel
# of an IDE (VS Code, Positron), where anything wider soft-wraps ugly.
_WRAP_COLS = 80
_LABEL_COL = 16  # "    What it is: " / "    Used by:    " — labels pad to 12 chars after a 4-space indent


def _wrap_labeled(label: str, text: str) -> str:
    """Wrap a labeled help line. Continuation lines align under the text,
    not the label, so the label stands out."""
    first = "    " + (label + " " * 12)[:12]  # pad/truncate label to 12 chars
    rest = " " * _LABEL_COL
    return textwrap.fill(
        text,
        width=_WRAP_COLS,
        initial_indent=first,
        subsequent_indent=rest,
        break_long_words=False,
        break_on_hyphens=False,
    )


def _wrap_body(text: str, indent: int = 2) -> str:
    """Wrap a body paragraph at 80 cols with a uniform left indent."""
    pad = " " * indent
    return textwrap.fill(
        text,
        width=_WRAP_COLS,
        initial_indent=pad,
        subsequent_indent=pad,
        break_long_words=False,
        break_on_hyphens=False,
    )


def _print_header() -> None:
    print()
    print("=" * 64)
    print("  academic-research plugin — setup wizard")
    print("=" * 64)
    print()
    print("  This will:")
    print("    1. Ask which LLM provider should run your screening pipelines")
    print("    2. Collect API keys (hidden input) and verify each one")
    print(f"    3. Write {CONFIG_PATH} (mode 0600)")
    print(f"    4. Patch {SETTINGS_PATH} with permission rules")
    print()
    print("  Your keys stay on this machine. They do not pass through")
    print("  your AI assistant's context at any point.")
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

    print()
    print(_wrap_body(f"{spec.label}{required_tag}{default_tag}"))
    print(_wrap_labeled("What it is:", spec.what))
    print(_wrap_labeled("Used by:", spec.used_by))
    print(_wrap_labeled("If missing:", spec.impact))
    print(_wrap_labeled("Get one at:", spec.where))
    print()

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


# ---------------------------------------------------------------------------
# LLM provider selection.
#
# Asked before any key question, because the answer decides which key
# questions there are. Before this existed the wizard asked for an
# Anthropic key and a Gemini key and nothing else, so the tier system
# only engaged for users who knew to set ACADEMIC_RESEARCH_PROVIDER by
# hand — the feature shipped invisible.
# ---------------------------------------------------------------------------


def _provider_default(existing: dict) -> str:
    """The provider to offer, in the runtime's own precedence order.

    Env beats the config file beats the registry default, matching
    `llm_provider.resolve_provider`. An unrecognised value in either
    place falls through rather than being offered back to the user.
    """
    candidates = (
        os.environ.get(providers.PROVIDER_ENV, "").strip(),
        str((existing.get("llm", {}) or {}).get("provider", "")).strip(),
    )
    for name in candidates:
        if providers.get(name) is not None:
            return name.lower()
    return providers.DEFAULT_PROVIDER


def _choose_provider(interactive: bool, existing: dict) -> str:
    """Ask which LLM provider the screening pipelines should call.

    Returns a name from `core.providers`. Non-interactive runs take the
    default without prompting, which is what makes
    `--non-interactive` with `ACADEMIC_RESEARCH_PROVIDER` set a
    reproducible fresh-machine setup.
    """
    default = _provider_default(existing)
    if not interactive:
        return default

    print()
    print(_wrap_body("Which LLM should run the screening pipelines?"))
    print()
    print(_wrap_body(
        "This chooses a provider, not a model. The plugin then asks that "
        "provider which models it currently serves and pins one per stage "
        "— a cheap model for screening thousands of abstracts, a stronger "
        "one for coding full texts. Only the credential belonging to the "
        "provider you pick here is asked for below.",
    ))
    print()
    for i, spec in enumerate(providers.PROVIDERS, 1):
        current = "  <- current" if spec.name == default else ""
        print(f"    {i}. {spec.label}{current}")
        need = (
            f"no API key; runs on your machine at {spec.default_base_url}"
            if spec.local else f"needs {spec.api_key_env}"
        )
        print(f"       {need}")
    print()
    print(f"  Enter a number or a name; press Enter to keep {default}.")

    valid = {p.name for p in providers.PROVIDERS}
    while True:
        try:
            typed = input("    > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.")
            sys.exit(1)
        if not typed:
            return default
        if typed.isdigit() and 1 <= int(typed) <= len(providers.PROVIDERS):
            return providers.PROVIDERS[int(typed) - 1].name
        if typed in valid:
            return typed
        print(f"    Not one of: {', '.join(sorted(valid))}. Try again.")


def _llm_credential_present(
    provider: str, values: dict, existing: dict,
) -> tuple[bool, str]:
    """Whether `provider` can actually be called, and what is missing.

    Mirrors `llm_provider._api_key_for` rather than guessing: local
    providers declare no credential at all, and an Anthropic-compatible
    self-hosted endpoint has one it does not check. Getting this wrong in
    either direction is a bad outcome — a false warning trains users to
    ignore the wizard, and a missing one lets a run fail on item 1.
    """
    spec = providers.get(provider) or providers.require(providers.DEFAULT_PROVIDER)
    if not spec.api_key_env:
        return True, ""
    section = "gemini" if spec.name == "google" else spec.name

    def _value(key: str) -> str:
        return str(
            (values.get(section, {}) or {}).get(key)
            or (existing.get(section, {}) or {}).get(key)
            or ""
        ).strip()

    if _value("api_key"):
        return True, ""
    # A local endpoint speaking the Anthropic Messages API ignores the
    # key, so a base URL alone is a working setup (issue #1).
    if spec.transport == "anthropic" and _value("base_url"):
        return True, ""
    hint = f"{spec.api_key_env} is not set"
    if spec.base_url_env and _value("base_url"):
        hint += (
            f", and {spec.base_url_env} alone is not enough for this "
            f"provider — set any non-empty {spec.api_key_env} if your "
            f"endpoint does not check it"
        )
    return False, hint


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


def _carry_forward(
    spec: KeySpec, existing: dict, values: dict[str, dict[str, object]],
) -> None:
    """Preserve a key this run did not ask about.

    `_write_config` rewrites the file from `values` alone, so anything
    not collected is deleted. A user who configured Anthropic last month
    and selects OpenAI today must not lose the Anthropic key — providers
    get compared and switched back, and re-issuing a key is a trip to a
    console the wizard cannot make for them.
    """
    prior = (existing.get(spec.toml_section, {}) or {}).get(spec.toml_key, "")
    if prior:
        values.setdefault(spec.toml_section, {})[spec.toml_key] = prior


def _collect_keys(
    interactive: bool, verify: bool, provider: str = "",
) -> dict[str, dict[str, object]]:
    existing = _load_existing_config()
    values: dict[str, dict[str, object]] = {}
    provider = provider or providers.DEFAULT_PROVIDER

    missing_required: list[str] = []
    for spec in KEYS:
        if spec.llm_provider and spec.llm_provider != provider:
            _carry_forward(spec, existing, values)
            continue
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

    ok, hint = _llm_credential_present(provider, values, existing)
    if not ok:
        spec = providers.require(provider)
        print(f"\n  WARNING: no credential configured for {spec.label} —")
        print(f"           {hint}.")
        print("           Screening pipelines will fail until it is set.")
        print("           Re-run this wizard, or pick a different provider:")
        print("           local providers (Ollama, LM Studio) need no key.")

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
    # POSIX permission bits don't apply on Windows (os.chmod only toggles
    # the read-only flag there). The config path is under the user's home
    # directory, which NTFS protects per-user by default, so skipping is
    # safe on Windows.
    if sys.platform != "win32":
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
    if sys.platform != "win32":
        os.chmod(CONFIG_PATH, 0o600)


@dataclass(frozen=True)
class PermissionCategory:
    """One bucket of permission allow rules the wizard offers as a
    single Y/n decision. Each rule carries its own one-line purpose
    so the user sees exactly what is being added.
    """
    name: str
    purpose: str
    skip_impact: str
    rules: tuple[tuple[str, str], ...]   # (rule, per-rule purpose)


def _permission_categories() -> tuple[list[PermissionCategory], list[str]]:
    """Return (categorised allow rules, flat deny list).

    Allow rules are grouped so the wizard can prompt category-by-
    category. Deny rules are non-optional security guardrails — they
    are added unconditionally.
    """
    home = str(Path.home())
    absolute_home_pattern = f"//{home.lstrip('/')}"

    categories = [
        PermissionCategory(
            name="Pipeline script execution",
            purpose=(
                "Lets the agent run the plugin's shipped pipeline "
                "scripts (search, screening, coding) via uv or "
                "python3, plus the one-time Playwright install for "
                "browser-based PDF retrieval."
            ),
            skip_impact=(
                "Every pipeline-stage call (and every helper script "
                "the skills invoke) will trigger a permission prompt."
            ),
            rules=(
                (f"Bash(uv run {PLUGIN_ROOT_ENV}/scripts/**)",
                 "uv run + script path (default invocation)"),
                (f"Bash(uv run -s {PLUGIN_ROOT_ENV}/scripts/**)",
                 "uv run -s (PEP 723 inline-deps marker, alt form)"),
                (f"Bash(uv run --script {PLUGIN_ROOT_ENV}/scripts/**)",
                 "uv run --script (newer alternate form)"),
                (f"Bash(python3 {PLUGIN_ROOT_ENV}/scripts/**)",
                 "Direct python3 invocation (used by skill helpers)"),
                (f"Bash({PLUGIN_ROOT_ENV}/scripts/**.py:*)",
                 "Direct script execution when shebang resolves"),
                ("Bash(playwright install chromium)",
                 "One-time browser install for browser PDF fetch"),
                ("Bash(uvx playwright install chromium)",
                 "Same install via uvx (no playwright CLI on PATH)"),
                ("Bash(playwright install-deps)",
                 "One-time system dependencies for Playwright"),
                ("Bash(uvx playwright install-deps)",
                 "Same system-deps install via uvx"),
            ),
        ),
        PermissionCategory(
            name="Safe read-only command-line inspection",
            purpose=(
                "Lets the agent run common read-only tools the SLR "
                "skills routinely invoke — grep, head, tail, wc, file, "
                "find, which, etc. None of these mutate state. "
                "Conservative by design: state-changing commands "
                "(curl, mkdir, cp, mv, rm, ln) are intentionally NOT "
                "auto-approved — those operations are the job of "
                "shipped scripts, not ad-hoc Bash."
            ),
            skip_impact=(
                "Every grep / head / tail / wc / etc. invocation will "
                "trigger a permission prompt. SLR work involves dozens "
                "of these per session (CSV inspection, screening "
                "audits, diff comparisons)."
            ),
            rules=(
                ("Bash(grep:*)",
                 "grep with any arguments (project-file search)"),
                ("Bash(rg:*)",
                 "ripgrep — faster grep alternative"),
                ("Bash(sed -n:*)",
                 "sed in print-only mode (no in-place edits)"),
                ("Bash(awk:*)",
                 "awk text processing (read-only)"),
                ("Bash(head:*)",
                 "Show first N lines of a file"),
                ("Bash(tail:*)",
                 "Show last N lines of a file"),
                ("Bash(wc:*)",
                 "Word / line / byte counts"),
                ("Bash(cat:*)",
                 "Print file contents (read-only — config.toml is "
                 "still denied below)"),
                ("Bash(ls:*)",
                 "List directory contents"),
                ("Bash(file:*)",
                 "Identify file type"),
                ("Bash(stat:*)",
                 "Show file metadata"),
                ("Bash(find:*)",
                 "Locate files matching a pattern (read-only)"),
                ("Bash(which:*)",
                 "Locate an executable"),
                ("Bash(command -v:*)",
                 "Shell-built-in alternative to which"),
                ("Bash(python3 -c:*)",
                 "Single-line Python introspection (no improvised "
                 "pipelines — those go through shipped scripts)"),
                ("Bash(python --version)",
                 "Check Python version"),
                ("Bash(python3 --version)",
                 "Check Python 3 version"),
            ),
        ),
        PermissionCategory(
            name="Plugin file inspection",
            purpose=(
                "Lets the agent list directories under the plugin "
                "root. Plugin source is public on GitHub; agents "
                "routinely `ls scripts/pipelines/` to orient "
                "themselves to the available stages."
            ),
            skip_impact=(
                "Every `ls` of a plugin directory will trigger a "
                "permission prompt."
            ),
            rules=(
                (f"Bash(ls {PLUGIN_ROOT_ENV}/**)",
                 "List directories under the plugin root"),
                (f"Bash(ls -l {PLUGIN_ROOT_ENV}/**)",
                 "Long-form list under the plugin root"),
                (f"Bash(ls -la {PLUGIN_ROOT_ENV}/**)",
                 "Long-form list with hidden files"),
                (f"Read({absolute_home_pattern}/.config/academic-research/)",
                 "Read the config DIRECTORY (the config file itself "
                 "stays denied — see the deny rules below)"),
            ),
        ),
        PermissionCategory(
            name="MCP citation databases (read-only)",
            purpose=(
                "Auto-approves search and metadata-lookup calls to "
                "Scopus, OpenAlex, Semantic Scholar, and the two "
                "paper-search MCP servers. All tools in these "
                "servers are read-only."
            ),
            skip_impact=(
                "Every search / abstract-fetch call (typically "
                "dozens per screening run) will trigger a "
                "permission prompt."
            ),
            rules=(
                ("mcp__scopus__*",
                 "All Scopus tools (search, get_abstract, etc.)"),
                ("mcp__openalex__*",
                 "All OpenAlex tools (~30; search, analyze, find)"),
                ("mcp__semantic-scholar__*",
                 "All Semantic Scholar tools (search, citations)"),
                ("mcp__paper-search__*",
                 "Paper-search Python MCP (arXiv, PubMed, etc.)"),
                ("mcp__paper-search-nodejs__*",
                 "Paper-search Node.js MCP (broader publishers)"),
            ),
        ),
        PermissionCategory(
            name="MCP Zotero (read-only)",
            purpose=(
                "Auto-approves Zotero queries (search, list, get_*). "
                "Zotero WRITES are deliberately NOT auto-approved — "
                "your library is user-owned data and write tools "
                "(add, update, delete, merge) keep prompting so you "
                "see every change before it lands."
            ),
            skip_impact=(
                "Every metadata read, search, and listing of your "
                "Zotero library will trigger a prompt."
            ),
            rules=(
                ("mcp__zotero__scite_check_retractions",
                 "Check for retractions via Scite (read-only)"),
                ("mcp__zotero__zotero_advanced_search",
                 "Advanced query"),
                ("mcp__zotero__zotero_find_duplicates",
                 "Find duplicates (read-only; merge stays denied)"),
                ("mcp__zotero__zotero_get_annotations",
                 "Get annotations on items"),
                ("mcp__zotero__zotero_get_collection_items",
                 "Items in a collection"),
                ("mcp__zotero__zotero_get_collections",
                 "List collections"),
                ("mcp__zotero__zotero_get_item_children",
                 "Children of an item, one key or many in one call"),
                ("mcp__zotero__zotero_get_item_fulltext",
                 "Full-text of an item (if attached)"),
                ("mcp__zotero__zotero_get_item_metadata",
                 "Metadata for an item"),
                ("mcp__zotero__zotero_get_notes",
                 "List notes on items, or search note text with query="),
                ("mcp__zotero__zotero_get_pdf_outline",
                 "Table of contents of a PDF"),
                ("mcp__zotero__zotero_get_recent",
                 "Recently added items"),
                ("mcp__zotero__zotero_get_search_database_status",
                 "Search index status"),
                ("mcp__zotero__zotero_get_tags",
                 "All tags in the library"),
                ("mcp__zotero__zotero_list_libraries",
                 "List accessible libraries (you + groups)"),
                ("mcp__zotero__zotero_search_by_citation_key",
                 "Find by BBT citekey"),
                ("mcp__zotero__zotero_search_by_tag",
                 "Find by tag"),
                ("mcp__zotero__zotero_search_collections",
                 "Search collection names"),
                ("mcp__zotero__zotero_search_items",
                 "Search items"),
                ("mcp__zotero__zotero_semantic_search",
                 "Semantic search"),
            ),
        ),
        PermissionCategory(
            name="zotero-cli (read-only)",
            purpose=(
                "Auto-approves read-only zotero-cli subcommands. "
                "zotero-cli ships with the zotero-mcp-server package "
                "installed above and gives the agent a Bash-callable "
                "path to Zotero for one-off operations MCP doesn't "
                "cover (see the zotero-operations skill's IRON RULE). "
                "Writes (edit, add, tags, notes, duplicates merge) "
                "deliberately stay prompt-gated, matching the MCP "
                "Zotero write policy above."
            ),
            skip_impact=(
                "Every read-only zotero-cli call (search, get, "
                "config, duplicates find) will trigger a permission "
                "prompt."
            ),
            rules=(
                ("Bash(zotero-cli search:*)",
                 "Search the library (items, tag, citekey, semantic)"),
                ("Bash(zotero-cli s:*)",
                 "Short alias for search"),
                ("Bash(zotero-cli get:*)",
                 "Read metadata, collections, tags, recent, etc."),
                ("Bash(zotero-cli g:*)",
                 "Short alias for get"),
                ("Bash(zotero-cli config)",
                 "Show current Zotero configuration"),
                ("Bash(zotero-cli duplicates find:*)",
                 "Find duplicates (read-only; merge stays denied)"),
                ("Bash(zotero-cli outline:*)",
                 "Get a PDF's table of contents"),
            ),
        ),
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
    return categories, deny


def _permission_patterns() -> tuple[list[str], list[str]]:
    """Backwards-compat wrapper for tests that consume a flat allow list."""
    categories, deny = _permission_categories()
    allow = [rule for cat in categories for rule, _ in cat.rules]
    return allow, deny


def _patch_settings(interactive: bool = True) -> tuple[int, int]:
    if not (Path.home() / ".claude").exists():
        if interactive:
            print("\n  No Claude Code environment (~/.claude) detected; skipping settings.json patch.")
        return 0, 0

    categories, deny_new = _permission_categories()

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

    if interactive:
        print()
        print(_wrap_body(
            "Permission allow rules. The wizard groups them by purpose; "
            "each category is a single Y/n decision. Skip any category "
            "you don't want auto-approved — the agent will then prompt "
            "you on every relevant call instead.",
        ))

    allow_added = 0
    for cat in categories:
        new_rules = [(r, p) for r, p in cat.rules if r not in allow_list]
        if not new_rules:
            continue

        if interactive:
            print()
            print(f"  ── {cat.name} ({len(new_rules)} new rule"
                  f"{'' if len(new_rules) == 1 else 's'}) ──")
            print(_wrap_body(cat.purpose, indent=4))
            print(_wrap_body(f"Skipping these means: {cat.skip_impact}",
                             indent=4))
            print()
            for rule, purpose in new_rules:
                print(f"      + {rule}")
                print(f"        → {purpose}")
            print()
            try:
                answer = input("    Add all? [Y/n] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\n    Skipped.")
                continue
            if answer in ("n", "no"):
                print("    Skipped.")
                continue

        for rule, _ in new_rules:
            allow_list.append(rule)
            allow_added += 1
        if interactive:
            print(f"    Added {len(new_rules)} rule"
                  f"{'' if len(new_rules) == 1 else 's'}.")

    deny_added = 0
    for p in deny_new:
        if p not in deny_list:
            deny_list.append(p)
            deny_added += 1
    if interactive and deny_added:
        print()
        print(_wrap_body(
            f"Added {deny_added} security deny rules (non-optional). "
            "These block the Read tool from `~/.config/academic-research"
            "/config.toml` and block common Bash readers (cat, head, "
            "grep, ...) from the same path so API keys cannot reach "
            "Claude's context.",
        ))

    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return allow_added, deny_added


def _maybe_add_claude_to_gitignore(cwd: Path | None = None) -> Path | None:
    """If we're inside a git repo, ensure `.claude/` is in its `.gitignore`.

    Pipeline scripts write per-project artefacts under `.claude/` (audit
    keys files, critic-loop reports, fact-check reports). A user who
    commits their research project shouldn't also commit those — they
    are ephemeral run-outputs, not source.

    Returns the .gitignore Path if the entry was added, else None.
    Silent no-op when the CWD isn't inside a git repo (e.g. the user
    ran the wizard from $HOME). Cross-platform — uses `git rev-parse`
    via subprocess list form, which works on Windows.
    """
    import subprocess
    cwd = cwd or Path.cwd()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None  # not a git repo, or git not installed, or network FS oddity

    repo_root = Path(result.stdout.strip())
    if not repo_root.exists():
        return None

    gi = repo_root / ".gitignore"
    existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
    # Match exact entry — don't treat `.claude-plugin/` as a hit.
    lines = {line.strip() for line in existing.splitlines()}
    if ".claude/" in lines or ".claude" in lines:
        return None

    separator = "" if (not existing) or existing.endswith("\n") else "\n"
    gi.write_text(f"{existing}{separator}.claude/\n", encoding="utf-8")
    return gi


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


ZOTERO_CLI_STATUS_OK = "ok"
ZOTERO_CLI_STATUS_MISSING = "missing"
ZOTERO_CLI_STATUS_STALE_SHADOW = "stale_shadow"


def _check_zotero_cli() -> tuple[str, str]:
    """Check whether `zotero-cli` resolves on PATH.

    `zotero-cli` is the standalone CLI shipped inside the same
    `zotero-mcp-server` package the `zotero` MCP entry installs (see
    EXPECTED_MCP above) — no separate install step, just a PATH check.

    One known trap: the *old* PyPI package was published under the
    plain name `zotero-mcp` (last released 0.1.6, well before the CLI
    existed). A `uv tool install zotero-mcp` from stale instructions
    or muscle memory silently shadows the real `zotero-mcp` console
    script from `zotero-mcp-server` without providing `zotero-cli` at
    all. We can't fully distinguish the two packages from PATH alone,
    so we detect the likely case (zotero-mcp present, zotero-cli
    absent) and name it explicitly rather than just saying "missing".
    """
    if shutil.which("zotero-cli"):
        return ZOTERO_CLI_STATUS_OK, "zotero-cli found on PATH"
    if shutil.which("zotero-mcp"):
        return (
            ZOTERO_CLI_STATUS_STALE_SHADOW,
            "zotero-mcp is on PATH but zotero-cli is not — likely the "
            "stale PyPI package `zotero-mcp` (0.1.6) rather than "
            "`zotero-mcp-server`",
        )
    return ZOTERO_CLI_STATUS_MISSING, "zotero-cli not found on PATH"


def _print_zotero_cli_help(status: str) -> None:
    """Print the actionable message when zotero-cli isn't usable.

    zotero-cli is optional — every skill degrades to MCP tools and
    zotero_io.py without it — so this is informational, not a hard
    failure gate like the local-API / BBT checks above.
    """
    print("  *** NOTE: zotero-cli is not available ***")
    if status == ZOTERO_CLI_STATUS_STALE_SHADOW:
        print("  Found `zotero-mcp` on PATH, but not `zotero-cli`. This")
        print("  usually means the stale PyPI package `zotero-mcp` (0.1.6,")
        print("  no CLI) is installed instead of `zotero-mcp-server`.")
        print("  Fix:")
        print("    uv tool uninstall zotero-mcp   # if present")
        print(f"    {ZOTERO_MCP_INSTALL_CMD}")
    else:
        print(f"  Install: {ZOTERO_MCP_INSTALL_CMD}")
    print("  Optional — zotero-operations and systematic-review fall back")
    print("  to MCP tools and zotero_io.py without it.")


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


def _mcp_spec_to_agy_entry(spec: McpServerSpec) -> dict:
    """Convert a `claude mcp add` argv (`spec.add_args`) into an Antigravity
    `mcpServers` entry (`~/.gemini/config/mcp_config.json`).

    `-s user <name>` scope flags have no Antigravity equivalent — that
    config is already a flat, user-level dict — so everything before `--`
    is dropped except any `-e KEY=VAL` pairs, which become `env`.
    """
    args = list(spec.add_args)
    env: dict[str, str] = {}
    i = 0
    while i < len(args):
        if args[i] == "-e" and i + 1 < len(args):
            key, _, value = args[i + 1].partition("=")
            env[key] = value
            del args[i:i + 2]
            continue
        i += 1

    sep = args.index("--")
    command, *rest = args[sep + 1:]
    entry: dict = {"command": command, "args": rest}
    if env:
        entry["env"] = env
    return entry


def _merge_mcp_status(claude_status: dict[str, str], agy_status: dict[str, str]) -> dict[str, str]:
    """Combine Claude-CLI and Antigravity MCP status maps.

    A server counts as connected if *either* surface reports it connected —
    an Antigravity-only user shouldn't see "not connected" for a server
    `claude mcp list` doesn't know about, and vice versa.
    """
    merged: dict[str, str] = {}
    for name in set(claude_status) | set(agy_status):
        c = claude_status.get(name, MCP_STATUS_MISSING)
        a = agy_status.get(name, MCP_STATUS_MISSING)
        if c == MCP_STATUS_CONNECTED or a == MCP_STATUS_CONNECTED:
            merged[name] = MCP_STATUS_CONNECTED
        elif c != MCP_STATUS_MISSING:
            merged[name] = c
        else:
            merged[name] = a
    return merged


def _agy_available() -> bool:
    """True if Antigravity (`agy`) appears to be installed on this machine."""
    return AGY_HOME.exists() or shutil.which("agy") is not None


def _check_agy_mcp_servers() -> dict[str, str]:
    """Read `~/.gemini/config/mcp_config.json` and return {name: status}.

    This reflects what's *configured*, not a live connectivity check (agy
    has no `mcp list` equivalent we can shell out to). An entry present and
    not `disabled` is reported as MCP_STATUS_CONNECTED; `disabled: true` or
    a missing/unreadable file is reported as MCP_STATUS_MISSING / `{}`.
    """
    if not AGY_MCP_CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(AGY_MCP_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    servers = data.get("mcpServers", {})
    return {
        name: MCP_STATUS_MISSING if entry.get("disabled") else MCP_STATUS_CONNECTED
        for name, entry in servers.items()
    }


def _load_agy_mcp_config() -> dict:
    """Load `~/.gemini/config/mcp_config.json`, backing up first if it
    exists. Mirrors `_patch_settings`'s read-backup-merge-write pattern."""
    if not AGY_MCP_CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(AGY_MCP_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  ERROR: cannot parse {AGY_MCP_CONFIG_PATH}: {e}", file=sys.stderr)
        print("  Back up your mcp_config.json, then re-run the wizard.", file=sys.stderr)
        sys.exit(3)
    backup = AGY_MCP_CONFIG_PATH.with_suffix(".json.bak-wizard")
    shutil.copy2(AGY_MCP_CONFIG_PATH, backup)
    return data


def _write_agy_mcp_config(data: dict) -> None:
    AGY_MCP_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    AGY_MCP_CONFIG_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _print_mcp_offer(spec: McpServerSpec, status: str, register_line: str | None = None) -> None:
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

    print()
    print(_wrap_body(headline))
    print(_wrap_labeled("What it is:", spec.purpose))
    print(_wrap_labeled("Project:", spec.homepage))
    print(_wrap_labeled("Install:", install_line))
    if spec.install_note:
        print(_wrap_labeled("", spec.install_note))
    print(_wrap_labeled("Register:", register_line if register_line is not None else _format_register_command(spec)))
    print()


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
                print(_wrap_body(
                    "Note: for local-mode (Zotero desktop instead of cloud), "
                    "re-run with `-e ZOTERO_LOCAL=true` — see the project page.",
                    indent=4,
                ))
        else:
            err_clean = err.strip() or "unknown error"
            print(_wrap_body(f"✗ Registration failed: {err_clean}", indent=4))
            if _looks_like_missing_binary(err) and spec.install_cmd:
                print(_wrap_body("The required command isn't on your PATH.", indent=4))
                print(_wrap_labeled("Install:", spec.install_cmd))
                if spec.install_note:
                    print(_wrap_labeled("", spec.install_note))
                print(_wrap_body("Then re-run this wizard.", indent=4))
            updated[spec.name] = updated.get(spec.name, MCP_STATUS_MISSING)

    return registered, updated


def _offer_register_agy_mcp(
    specs: tuple[McpServerSpec, ...],
    current: dict[str, str],
    interactive: bool,
) -> tuple[int, dict[str, str]]:
    """For each spec not currently connected in `~/.gemini/config/mcp_config.json`,
    offer to add it.

    Mirrors `_offer_register_mcp`, but merges a JSON entry into Antigravity's
    `mcpServers` config instead of shelling out to `claude mcp add`. Returns
    (registered_count, updated_status_map).
    """
    updated = dict(current)
    if not interactive:
        return 0, updated

    registered = 0
    for spec in specs:
        status = current.get(spec.name, MCP_STATUS_MISSING)
        if status == MCP_STATUS_CONNECTED:
            continue

        _print_mcp_offer(spec, status, register_line=f"add to {AGY_MCP_CONFIG_PATH}")

        try:
            answer = input("    Register now? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            print("    Skipped (input ended).")
            continue

        if answer not in ("", "y", "yes"):
            print("    Skipped.")
            continue

        config = _load_agy_mcp_config()
        servers = config.setdefault("mcpServers", {})
        servers[spec.name] = _mcp_spec_to_agy_entry(spec)
        _write_agy_mcp_config(config)

        print(f"    ✓ Registered {spec.name}.")
        updated[spec.name] = MCP_STATUS_CONNECTED
        registered += 1

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

    print("    MCP (Model Context Protocol) servers")
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

    # Preserve (or extend) non-key sections across re-runs.
    existing_cfg = _load_existing_config()

    provider = _choose_provider(interactive, existing_cfg)
    values = _collect_keys(interactive, verify, provider)
    # Keep any other `[llm]` key the user set by hand (`max_retries`).
    values["llm"] = {**(existing_cfg.get("llm", {}) or {}), "provider": provider}

    connector_entry = _detect_and_prompt_connector(interactive, existing_cfg)
    if connector_entry:
        values["zotero_connector"] = connector_entry

    updated_no_access = _offer_no_access_editor(interactive, existing_cfg)
    if updated_no_access:
        values.setdefault("library", {})["no_access"] = updated_no_access

    _write_config(values)
    allow_added, deny_added = _patch_settings(interactive=interactive)
    gitignore_updated = _maybe_add_claude_to_gitignore()

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

    zotero_cli_status, zotero_cli_message = _check_zotero_cli()

    current_mcp = _check_mcp_servers()
    if interactive:
        print()
        print(_wrap_body(
            "Checking MCP (Model Context Protocol) servers. These are small "
            "helper programs that let your AI assistant read your Zotero "
            "library, search citation databases, and fetch PDFs. The plugin uses "
            "five of them and offers to register any that are missing.",
        ))
        registered, current_mcp = _offer_register_mcp(
            EXPECTED_MCP, current_mcp, interactive=True,
        )
        if registered:
            # Re-poll so the final summary reflects post-registration state.
            current_mcp = _check_mcp_servers() or current_mcp

    # MCP registration only — Antigravity has no per-project permission/allow-list
    # file analogous to `~/.claude/settings.json` for `_patch_settings` to target.
    agy_available = _agy_available()
    agy_mcp: dict[str, str] = {}
    agy_registered = 0
    if agy_available:
        agy_mcp = _check_agy_mcp_servers()
        if interactive:
            print()
            print(_wrap_body(
                "Antigravity detected. Registering the same MCP servers in "
                f"{AGY_MCP_CONFIG_PATH} so they're available there too.",
            ))
            agy_registered, agy_mcp = _offer_register_agy_mcp(
                EXPECTED_MCP, agy_mcp, interactive=True,
            )

    current_mcp = _merge_mcp_status(current_mcp, agy_mcp)

    print()
    print("  Setup complete.")
    print(f"    Config:   {CONFIG_PATH} (mode 0600)")
    print(f"    LLM:      {providers.require(provider).label}")
    print(f"    Settings: {SETTINGS_PATH} (+{allow_added} allow, +{deny_added} deny)")
    if gitignore_updated is not None:
        print(f"    Gitignore: added .claude/ entry to {gitignore_updated}")
    if agy_available:
        print(f"    Antigravity MCP: {AGY_MCP_CONFIG_PATH} (+{agy_registered} registered)")
    glyph = "✓" if zotero_local_status == ZOTERO_LOCAL_STATUS_OK else "✗"
    print(f"    Zotero local API: {glyph} {zotero_local_message}")
    bbt_glyph = "✓" if zotero_bbt_status == ZOTERO_BBT_STATUS_OK else "✗"
    print(f"    Better BibTeX:    {bbt_glyph} {zotero_bbt_message}")
    cli_glyph = "✓" if zotero_cli_status == ZOTERO_CLI_STATUS_OK else "○"
    print(f"    zotero-cli:       {cli_glyph} {zotero_cli_message}")
    zotero_missing, all_search_dbs_missing = _print_mcp_summary(current_mcp)

    if zotero_local_status != ZOTERO_LOCAL_STATUS_OK:
        print()
        _print_zotero_local_help()
    elif zotero_bbt_status == ZOTERO_BBT_STATUS_MISSING:
        print()
        _print_zotero_bbt_help()

    if zotero_cli_status != ZOTERO_CLI_STATUS_OK:
        print()
        _print_zotero_cli_help(zotero_cli_status)

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
    print(_wrap_body(
        "Screening runs on whichever models your provider currently serves. "
        "Inside a systematic-review project, pin them into "
        "screening_config.py with:",
    ))
    print(f"    python3 {_HERE / 'resolve_models.py'}")

    claude_code_available = (Path.home() / ".claude").exists()
    print()
    if claude_code_available and agy_available:
        print("  Return to your Claude Code or Antigravity session and tell it setup is done.")
    elif agy_available:
        print("  Return to your Antigravity session and tell Gemini setup is done.")
    else:
        print("  Return to your Claude Code session and tell Claude setup is done.")
    print()
    return 4 if zotero_missing else 0


if __name__ == "__main__":
    sys.exit(main())
