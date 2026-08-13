"""Ask a provider which models it currently serves.

This is what replaces the hardcoded model IDs: instead of shipping a
model name that goes stale on the provider's next release, the plugin
asks the provider, shows the answer, and lets the agent and the user
choose. `resolve_models.py` writes the choice into the project.

**This module does not pick a model, and should not learn to.** It once
did, ranking candidates by tier hints and version numbers, and the
ranking was wrong in ways no amount of tuning fixes: OpenRouter's
`anthropic/claude-haiku-4.5:batch` (the async Batch API) beat the plain
ID on a string tiebreak, and Google's `deep-research-pro-preview-12-2025`
won the deep tier because `12-2025` parses as version 12.2025. Both are
obvious to anyone who can read a model name and knows what a Batch API
is — and this module runs in exactly one place, `resolve_models.py`,
which is invoked from a SKILL.md with an agent reading the output and a
user present to confirm. Encoding that judgement as substring
blocklists just recreates the staleness treadmill one level down.

`templates/model_catalog.toml` remains the answer for the case with no
human in the loop: an unreachable provider, or a project that never
pinned anything.

Deliberately stdlib-only for the HTTP call, with its own bounded
backoff. The wizard imports this, and `scripts/setup/` runs under a bare
`python3` with no venv — see the stdlib-only rule in CLAUDE.md. That
also keeps `/setup` working before any pipeline dependency is installed.
"""

from __future__ import annotations

import json
import random
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from core import providers
from core.providers import ProviderSpec

#: Shipped alongside the templates so `install_templates.py` can copy it
#: and `resolve_models.py` can read it without a project scaffold.
CATALOG_PATH = (
    Path(__file__).resolve().parent.parent.parent / "templates" / "model_catalog.toml"
)

_RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3
_MAX_DELAY_S = 8.0


@dataclass(frozen=True)
class ModelInfo:
    """One model a provider currently serves."""

    id: str
    created: int = 0          # epoch seconds; 0 when the provider omits it
    display_name: str = ""


class DiscoveryError(RuntimeError):
    """The provider could not be asked. Callers fall back to the catalog."""


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def _get_json(url: str, headers: dict[str, str], timeout: int = 10) -> dict:
    req = urllib.request.Request(url, headers=headers)
    last = ""
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code not in _RETRY_STATUSES:
                raise DiscoveryError(last) from e
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            last = str(e)
        if attempt < _MAX_ATTEMPTS:
            time.sleep(random.uniform(0.0, min(2.0 ** attempt, _MAX_DELAY_S)))  # noqa: S311
    raise DiscoveryError(last or "unreachable")


def _auth_headers(spec: ProviderSpec, api_key: str) -> dict[str, str]:
    if spec.transport == "anthropic":
        return {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    if spec.transport == "google":
        return {}  # key rides in the query string
    if api_key:
        return {"Authorization": f"Bearer {api_key}"}
    return {}


def _normalise(spec: ProviderSpec, payload: dict) -> list[ModelInfo]:
    """Flatten each provider's listing shape into ModelInfo.

    Four shapes in the wild: Anthropic and OpenAI-compatible both use
    `{"data": [...]}` but name the timestamp differently, Google uses
    `{"models": [{"name": "models/gemini-..."}]}`, and Ollama uses
    `{"models": [{"name": "llama3:8b", "modified_at": "..."}]}`.
    """
    out: list[ModelInfo] = []
    if spec.transport == "google":
        for m in payload.get("models") or []:
            ident = str(m.get("name", "")).removeprefix("models/")
            if ident:
                out.append(ModelInfo(id=ident, display_name=m.get("displayName", "")))
        return out
    if spec.name == "ollama":
        for m in payload.get("models") or []:
            ident = str(m.get("name", ""))
            if ident:
                out.append(ModelInfo(id=ident))
        return out
    for m in payload.get("data") or []:
        ident = str(m.get("id", ""))
        if not ident:
            continue
        created = m.get("created") or m.get("created_at") or 0
        out.append(ModelInfo(
            id=ident,
            created=int(created) if isinstance(created, int) else 0,
            display_name=str(m.get("display_name", "") or ""),
        ))
    return out


def list_models(spec: ProviderSpec, api_key: str = "", base_url: str = "") -> list[ModelInfo]:
    """Every model `spec` currently serves. Raises DiscoveryError."""
    if not spec.list_models_url:
        raise DiscoveryError(f"{spec.name} has no model-listing endpoint")
    base = providers.base_url_for(spec, base_url)
    url = spec.list_models_url.format(base=base, key=api_key)
    payload = _get_json(url, _auth_headers(spec, api_key))
    models = _normalise(spec, payload)
    if not models:
        raise DiscoveryError(f"{spec.name} returned no models")
    return models


# ---------------------------------------------------------------------------
# Catalog fallback
# ---------------------------------------------------------------------------


def load_catalog(path: Path | None = None) -> dict:
    """Read `model_catalog.toml`. Empty dict when absent or malformed."""
    p = path or CATALOG_PATH
    try:
        with p.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def catalog_model(provider: str, tier: str, catalog: dict | None = None) -> str:
    entry = (catalog if catalog is not None else load_catalog())
    return str(entry.get(provider, {}).get(tier, {}).get("model", ""))


def catalog_prices(provider: str, tier: str, catalog: dict | None = None) -> tuple[float, float]:
    """`(input_per_mtok, output_per_mtok)` in USD; `(0, 0)` if unknown."""
    entry = (catalog if catalog is not None else load_catalog())
    row = entry.get(provider, {}).get(tier, {})
    return (
        float(row.get("input_per_mtok", 0.0) or 0.0),
        float(row.get("output_per_mtok", 0.0) or 0.0),
    )


def catalog_suggestions(provider: str, catalog: dict | None = None) -> list[tuple[str, str]]:
    """`[(tier, model)]` the shipped catalogue offers for `provider`.

    The fallback menu when the provider cannot be asked. Empty for
    OpenRouter (it proxies everyone, so no single pin is defensible) and
    for the local providers, which serve whatever the user pulled.
    """
    entry = (catalog if catalog is not None else load_catalog()).get(provider, {})
    out = []
    for tier in providers.TIERS:
        model = str(entry.get(tier, {}).get("model", ""))
        if model:
            out.append((tier, model))
    return out


# ---------------------------------------------------------------------------
# Cost estimate
# ---------------------------------------------------------------------------

#: Rough token counts per paper, measured against this plugin's own
#: prompts. Used for the pre-run estimate, which is an order-of-magnitude
#: aid, not an invoice.
ABSTRACT_INPUT_TOKENS = 1_200      # system prompt + title + abstract
ABSTRACT_OUTPUT_TOKENS = 50        # DECISION + REASON, two lines
FULLTEXT_OUTPUT_TOKENS = 800       # the coding JSON
#: A 10,000-word paper at ~1.3 tokens/word, plus the coding prompt.
FULLTEXT_INPUT_TOKENS = 13_800


def estimate_cost(
    provider: str,
    tier: str,
    n_items: int,
    *,
    stage: str = "abstract_screening",
    catalog: dict | None = None,
) -> float:
    """Projected USD for running `stage` over `n_items`.

    Returns 0.0 when the provider is local or the price is unknown —
    the caller should say "no estimate available" rather than "free".
    """
    in_price, out_price = catalog_prices(provider, tier, catalog)
    if not in_price and not out_price:
        return 0.0
    if stage == "fulltext_coding":
        in_tok, out_tok = FULLTEXT_INPUT_TOKENS, FULLTEXT_OUTPUT_TOKENS
    else:
        in_tok, out_tok = ABSTRACT_INPUT_TOKENS, ABSTRACT_OUTPUT_TOKENS
    return n_items * (in_tok * in_price + out_tok * out_price) / 1_000_000
