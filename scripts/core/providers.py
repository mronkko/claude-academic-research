"""Which LLM providers this plugin can talk to, and how to reach them.

A data-only registry. **No model version string appears in this file**,
and none should: model IDs go stale on every release, and a plugin that
pins them ships broken defaults to everyone who installed it before the
release. What is stable is the *shape* of a provider — its endpoint, the
credential it wants, and the words its own model IDs use to mark a tier
("haiku", "flash", "mini"). Those survive releases.

The three-layer split:

  providers.py       what a provider is        (this file — no versions)
  model_discovery.py what it currently serves  (asks the provider)
  screening_config.py what this project pinned (written at bootstrap)

`tier_hints` is the load-bearing idea. Rather than knowing that Haiku 4.5
is the cheap Anthropic model, the plugin knows that Anthropic marks its
cheap models with "haiku" in the ID, and asks the API which of those
exist today. When Anthropic ships Haiku 5, discovery finds it with no
code change.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Stage-independent capability tiers. Every provider maps each of these
#: onto its own naming, via `tier_hints`.
TIER_FAST = "fast"
TIER_BALANCED = "balanced"
TIER_DEEP = "deep"
TIERS: tuple[str, ...] = (TIER_FAST, TIER_BALANCED, TIER_DEEP)


@dataclass(frozen=True)
class ProviderSpec:
    """Everything the plugin needs to know about one provider.

    `transport` selects the client class in `llm_provider`, not a URL
    shape: several providers share the OpenAI-compatible wire format and
    differ only in endpoint and credential, so they share one client.
    """

    name: str
    label: str
    transport: str            # "anthropic" | "google" | "openai_compat"
    api_key_env: str          # "" when the provider needs no key at all
    base_url_env: str = ""
    default_base_url: str = ""
    #: Endpoint that lists available models. `{base}` is substituted with
    #: the effective base URL, `{key}` with the API key (Google puts the
    #: key in the query string rather than a header).
    list_models_url: str = ""
    #: Substrings that identify a tier inside this provider's model IDs.
    #: Checked lowercase, in order; first match wins.
    tier_hints: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: Substrings that disqualify a model from a tier even though one of
    #: that tier's hints matched. Needed because provider naming nests:
    #: "gemini-2.5-flash-lite" contains "flash", so without an exclusion
    #: the balanced tier picks the cheap model. Explicit rather than
    #: inferred from hint length — the nesting is a naming accident, not
    #: a rule, and a wrong guess here silently downgrades every run.
    tier_excludes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: True when the provider runs on the user's own machine — no key,
    #: no per-paper cost, and no point verifying a credential.
    local: bool = False
    #: Where to get a key, for the wizard's prompt.
    key_url: str = ""


PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        name="anthropic",
        label="Anthropic (Claude)",
        transport="anthropic",
        api_key_env="ANTHROPIC_API_KEY",
        base_url_env="ANTHROPIC_BASE_URL",
        list_models_url="{base}/v1/models?limit=100",
        default_base_url="https://api.anthropic.com",
        tier_hints={
            TIER_FAST: ("haiku",),
            TIER_BALANCED: ("sonnet",),
            TIER_DEEP: ("opus", "sonnet"),
        },
        key_url="https://console.anthropic.com/settings/keys",
    ),
    ProviderSpec(
        name="google",
        label="Google (Gemini)",
        transport="google",
        api_key_env="GEMINI_API_KEY",
        default_base_url="https://generativelanguage.googleapis.com",
        list_models_url="{base}/v1beta/models?key={key}&pageSize=200",
        tier_hints={
            TIER_FAST: ("flash-lite", "flash"),
            TIER_BALANCED: ("flash",),
            TIER_DEEP: ("pro",),
        },
        tier_excludes={TIER_BALANCED: ("flash-lite",)},
        key_url="https://aistudio.google.com/app/apikey",
    ),
    ProviderSpec(
        name="openai",
        label="OpenAI",
        transport="openai_compat",
        api_key_env="OPENAI_API_KEY",
        base_url_env="OPENAI_BASE_URL",
        default_base_url="https://api.openai.com",
        list_models_url="{base}/v1/models",
        tier_hints={
            TIER_FAST: ("nano", "mini"),
            TIER_BALANCED: ("mini",),
            TIER_DEEP: ("gpt",),
        },
        tier_excludes={
            TIER_BALANCED: ("nano",),
            # Otherwise "gpt" matches every model the account can see.
            TIER_DEEP: ("nano", "mini", "audio", "realtime", "embedding",
                        "image", "tts", "whisper", "moderation"),
        },
        key_url="https://platform.openai.com/api-keys",
    ),
    ProviderSpec(
        name="openrouter",
        label="OpenRouter (one key, many models)",
        transport="openai_compat",
        api_key_env="OPENROUTER_API_KEY",
        base_url_env="OPENROUTER_BASE_URL",
        default_base_url="https://openrouter.ai/api",
        list_models_url="{base}/v1/models",
        tier_hints={
            TIER_FAST: ("haiku", "flash", "mini"),
            TIER_BALANCED: ("sonnet", "flash"),
            TIER_DEEP: ("opus", "pro", "sonnet"),
        },
        tier_excludes={
            TIER_BALANCED: ("flash-lite",),
            TIER_DEEP: ("mini", "flash-lite"),
        },
        key_url="https://openrouter.ai/keys",
    ),
    ProviderSpec(
        name="ollama",
        label="Ollama (local, no API key)",
        transport="openai_compat",
        api_key_env="",
        base_url_env="OLLAMA_BASE_URL",
        default_base_url="http://localhost:11434",
        # Ollama's own listing endpoint; `model_discovery` normalises the
        # `{"models": [{"name": ...}]}` shape to the OpenAI one.
        list_models_url="{base}/api/tags",
        tier_hints={
            TIER_FAST: (":1b", ":3b", ":4b", "mini", "small"),
            TIER_BALANCED: (":7b", ":8b", ":9b", ":12b", ":14b"),
            TIER_DEEP: (":27b", ":30b", ":32b", ":70b", ":72b"),
        },
        local=True,
    ),
    ProviderSpec(
        name="lmstudio",
        label="LM Studio (local, no API key)",
        transport="openai_compat",
        api_key_env="",
        base_url_env="LMSTUDIO_BASE_URL",
        default_base_url="http://localhost:1234",
        list_models_url="{base}/v1/models",
        tier_hints={
            TIER_FAST: ("1b", "3b", "4b", "mini", "small"),
            TIER_BALANCED: ("7b", "8b", "9b", "12b", "14b"),
            TIER_DEEP: ("27b", "30b", "32b", "70b", "72b"),
        },
        local=True,
    ),
)

BY_NAME: dict[str, ProviderSpec] = {p.name: p for p in PROVIDERS}

#: The provider a user gets if they never choose one. Anthropic because
#: the plugin's prompts and worker counts are tuned against it — not a
#: claim that it is best.
DEFAULT_PROVIDER = "anthropic"

#: Environment override for `[llm] provider`, following the plugin-wide
#: rule that env beats config file. Named here so the wizard, the
#: setup scripts, and the runtime router cannot drift on the spelling.
PROVIDER_ENV = "ACADEMIC_RESEARCH_PROVIDER"


def get(name: str) -> ProviderSpec | None:
    """Look up a provider by name, case-insensitively."""
    return BY_NAME.get((name or "").strip().lower())


def require(name: str) -> ProviderSpec:
    """Like `get`, but raise with the valid names listed."""
    spec = get(name)
    if spec is None:
        raise ValueError(
            f"Unknown model provider {name!r}. "
            f"Choose one of: {', '.join(sorted(BY_NAME))}."
        )
    return spec


def base_url_for(spec: ProviderSpec, configured: str = "") -> str:
    """Effective base URL: an explicit override, else the default."""
    return (configured or "").strip().rstrip("/") or spec.default_base_url


def tier_of(spec: ProviderSpec, model_id: str) -> str:
    """Which tier `model_id` looks like for this provider, or `""`.

    Checked cheapest-first so a model matching several hints lands in
    the tier a cost-conscious user expects: `gemini-2.5-flash` is
    `fast`, not `balanced`, even though "flash" appears in both.
    """
    lowered = (model_id or "").lower()
    for tier in TIERS:
        if any(hint in lowered for hint in spec.tier_hints.get(tier, ())):
            return tier
    return ""


def hint_rank(spec: ProviderSpec, model_id: str, tier: str) -> int:
    """Index of the first `tier` hint that `model_id` matches.

    Hints are ordered best-first, so a lower rank is a better fit. This
    is what makes `deep: ("opus", "sonnet")` mean "Opus, or Sonnet if
    there is no Opus" rather than "either, whichever sorts later".
    Returns a large number when nothing matches.
    """
    lowered = (model_id or "").lower()
    for i, hint in enumerate(spec.tier_hints.get(tier, ())):
        if hint in lowered:
            return i
    return 99


def matches_tier(spec: ProviderSpec, model_id: str, tier: str) -> bool:
    """True if `model_id` is eligible for `tier`.

    Deliberately not `tier_of(...) == tier`: a deep-tier search should
    accept `claude-sonnet-*` even though `tier_of` calls it balanced.
    Selection is a filter, classification is a single label.

    `tier_excludes` is checked first so a nested name cannot sneak into
    a tier above its own — "gemini-2.5-flash-lite" contains "flash" and
    would otherwise satisfy the balanced tier.
    """
    lowered = (model_id or "").lower()
    if any(bad in lowered for bad in spec.tier_excludes.get(tier, ())):
        return False
    return any(hint in lowered for hint in spec.tier_hints.get(tier, ()))
