"""Ask a provider which models it currently serves, and pick one per tier.

This is what replaces the hardcoded model IDs. `providers.py` knows that
Anthropic marks cheap models with "haiku"; this module asks Anthropic
which "haiku" models exist right now and takes the newest. When a
provider ships a new generation, the plugin finds it with no code change.

Deliberately stdlib-only for the HTTP call, with its own bounded
backoff. The wizard imports this, and `scripts/setup/` runs under a bare
`python3` with no venv — see the stdlib-only rule in CLAUDE.md. That
also keeps `/setup` working before any pipeline dependency is installed.
"""

from __future__ import annotations

import json
import random
import re
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
# Selection
# ---------------------------------------------------------------------------

_VERSION = re.compile(r"(\d+(?:[.\-]\d+)*)")


def _sort_key(m: ModelInfo) -> tuple:
    """Newest-first ordering: version numbers first, `created` to break ties.

    Version wins over the timestamp deliberately. The failure that
    matters is silently picking an *older generation*, and the numbers
    in the ID encode generation explicitly, while `created` is whatever
    the provider chose to report — absent on several, and not
    necessarily monotonic with capability. `claude-haiku-4-5` must beat
    `claude-haiku-3-5` regardless of what either timestamp says.

    Within one generation the numbers are equal, so `created` and then
    the dated suffix decide, which picks the more recent snapshot.
    """
    parts = tuple(
        int(n) for chunk in _VERSION.findall(m.id)
        for n in re.split(r"[.\-]", chunk) if n.isdigit()
    )
    return (parts, m.created, m.id)


def pick_for_tier(models: list[ModelInfo], spec: ProviderSpec, tier: str) -> str:
    """The best-fitting, newest model for `tier`, or `""`.

    Two-level choice: the tier's hints are ordered best-first, so
    `deep: ("opus", "sonnet")` takes an Opus when one exists and falls
    back to Sonnet when none does. Only within one hint does recency
    decide.

    Pure — given the same listing it always returns the same ID, which
    is what makes a pinned choice reproducible and testable against a
    recorded fixture.
    """
    candidates = [m for m in models if providers.matches_tier(spec, m.id, tier)]
    if not candidates:
        return ""
    return max(
        candidates,
        key=lambda m: (-providers.hint_rank(spec, m.id, tier), *_sort_key(m)),
    ).id


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


@dataclass
class Resolution:
    """What `resolve_tier` decided, and how it decided it."""

    model: str
    source: str               # "discovered" | "catalog" | "none"
    detail: str = ""          # why discovery was not used, when it wasn't

    @property
    def is_stale_risk(self) -> bool:
        """True when the answer came from a file rather than the provider."""
        return self.source == "catalog"


def resolve_tier(
    spec: ProviderSpec,
    tier: str,
    *,
    api_key: str = "",
    base_url: str = "",
    catalog: dict | None = None,
) -> Resolution:
    """Pick a concrete model for `tier`, falling back loudly.

    Never raises: a bootstrap that cannot reach the provider should
    still produce a working project, and should say what happened
    rather than pinning something stale in silence.
    """
    try:
        models = list_models(spec, api_key=api_key, base_url=base_url)
    except DiscoveryError as e:
        fallback = catalog_model(spec.name, tier, catalog)
        if fallback:
            return Resolution(fallback, "catalog", str(e))
        return Resolution("", "none", str(e))
    picked = pick_for_tier(models, spec, tier)
    if picked:
        return Resolution(picked, "discovered")
    fallback = catalog_model(spec.name, tier, catalog)
    if fallback:
        return Resolution(
            fallback, "catalog",
            f"{len(models)} models listed, none matching the {tier} tier",
        )
    return Resolution("", "none", f"no {tier}-tier model among {len(models)} listed")


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
