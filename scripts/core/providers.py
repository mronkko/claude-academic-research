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

`tier_hints` maps a capability tier onto the words a provider puts in its
own model IDs ("haiku", "flash", "mini"). It is a *classifier*, not a
chooser: `tier_of` uses it to price a model back into a tier, and
`resolve_models.py --list` uses it to annotate a listing so a reader can
skim it. Nothing picks a model from these hints — that judgement belongs
to the agent and the user, who can tell that `deep-research-pro-preview`
is an async research API and `:batch` is a queue, which no substring
list encodes durably.
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
    #: Checked lowercase, in order; first match wins. Advisory only —
    #: see the module docstring on why nothing selects a model from these.
    tier_hints: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: True when the provider runs on the user's own machine — no key,
    #: no per-paper cost, and no point verifying a credential.
    local: bool = False
    #: True when the plugin ships no hostname for this provider and the
    #: user must supply one. Distinct from `local`: the endpoint is
    #: remote and authenticated, but its address, its key page, and even
    #: the name a user would give its environment variable are all
    #: institution-specific, so there is nothing honest to put in
    #: `default_base_url`, `key_url`, `api_key_env` or `base_url_env`.
    #: Mutually exclusive with `local`.
    #:
    #: This is why `api_key_env == ""` cannot mean "needs no credential"
    #: on its own: a local provider has no credential, a bring-your-own
    #: provider has one the plugin cannot name. Code asking "is a key
    #: required here?" must branch on `local`, not on `api_key_env`.
    #:
    #: A field rather than a `default_base_url == ""` predicate because
    #: the derived form is invisible at the call sites that need it —
    #: `OpenAICompatProvider.__init__`, `check_model_connection.py`, the
    #: wizard menu — and a vendor provider that temporarily lacked a
    #: default URL would silently acquire bring-your-own semantics.
    byo_endpoint: bool = False
    #: Where to get a key, for the wizard's prompt. Empty for
    #: `byo_endpoint` providers, where the answer is institutional and
    #: lives in the wizard's `KeySpec.where` instead.
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
        key_url="https://openrouter.ai/keys",
    ),
    ProviderSpec(
        name="gateway",
        label="Institutional gateway (OpenAI-compatible endpoint)",
        transport="openai_compat",
        # No canonical environment variable, deliberately. Every other
        # provider's is an ecosystem convention its own SDK already
        # reads — ANTHROPIC_API_KEY, OPENAI_API_KEY — so naming it costs
        # the user nothing. A gateway has no such convention, and any
        # name the plugin invented would collide with whatever the user
        # actually calls theirs (UNI_LLM_TOKEN, MY_LLM_KEY, …),
        # forcing them to export the same secret twice. So `[gateway]`
        # in config.toml is the home, and a user who prefers an
        # environment variable names their own via
        # `[gateway] api_key_env` / `base_url_env`.
        api_key_env="",
        base_url_env="",
        # No default: a university's LLM gateway has no address the
        # plugin could guess, and inventing one would send a user's
        # abstracts to a host they did not choose.
        default_base_url="",
        list_models_url="{base}/v1/models",
        # Gateways serve open-weight models, whose IDs carry a parameter
        # count rather than a vendor's tier word. Leading hyphens matter:
        # a bare "1b" also matches "31b", which is the bug `lmstudio`'s
        # hints have. `-120b` and `-20b` stay separate because the
        # character before "20b" in "gpt-oss-120b" is "1", not "-".
        #
        # The sizes are enumerated rather than pattern-matched because
        # `tier_hints` is substring data and must stay that way — see the
        # module docstring on why nothing here may grow into a chooser.
        # The list covers what real gateways serve; an ID that misses it
        # is labelled `?`, which is honest and costs only a hint in the
        # listing. A first pass against a live gateway left six of eight
        # models unplaceable (`-30b`, `-31b`, `e4b` were all missing), so
        # these are worth keeping current.
        tier_hints={
            TIER_FAST: (
                "-1b", "-2b", "-3b", "-4b", "e4b", "-7b", "-8b", "-9b",
                "mini", "small", "lite",
            ),
            TIER_BALANCED: (
                "-12b", "-13b", "-14b", "-20b", "-22b", "-24b", "-27b",
                "-30b", "-31b", "-32b", "-34b", "medium",
            ),
            TIER_DEEP: (
                "-70b", "-72b", "-120b", "-235b", "-405b", "-480b", "large",
            ),
        },
        byo_endpoint=True,
        # Institution-specific; the wizard's KeySpec.where explains it.
        key_url="",
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


def config_section(spec: ProviderSpec) -> str:
    """The `config.toml` section holding this provider's settings.

    Section name equals provider name, with one exception: Google's key
    has lived under `[gemini]` since before this registry existed, and
    renaming it would silently un-configure everyone who has already run
    `/setup`. Kept in one function because the exception is invisible at
    every call site that hardcodes it.
    """
    return "gemini" if spec.name == "google" else spec.name


def base_url_for(spec: ProviderSpec, configured: str = "") -> str:
    """Effective base URL: an explicit override, else the default."""
    return (configured or "").strip().rstrip("/") or spec.default_base_url


def credential_location(spec: ProviderSpec, env_name: str = "") -> str:
    """Where this provider's API key belongs, phrased for a user.

    A provider with a canonical variable is told by name. One without —
    a bring-your-own gateway — is pointed at its `config.toml` section,
    because "set ''" helps nobody. `env_name` lets a caller that has
    resolved a user-declared variable name include it.

    Defined here, once, so the six places that report a missing
    credential cannot describe it differently.
    """
    where = f"config.toml [{config_section(spec)}].api_key"
    name = env_name or spec.api_key_env
    return f"{name} or {where}" if name else where


def base_url_location(spec: ProviderSpec, env_name: str = "") -> str:
    """Where this provider's base URL belongs. See `credential_location`."""
    where = f"config.toml [{config_section(spec)}].base_url"
    name = env_name or spec.base_url_env
    return f"{name} or {where}" if name else where


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


def tier_label(spec: ProviderSpec, model_id: str) -> str:
    """`tier_of`, rendered for a listing column. `"?"` when unclassifiable.

    A display helper rather than a decision: the caller is printing a
    menu for a human to choose from, and a blank cell reads as an error
    where a question mark reads as "this one I cannot place".
    """
    return tier_of(spec, model_id) or "?"
