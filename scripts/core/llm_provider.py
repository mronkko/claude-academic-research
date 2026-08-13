"""Unified LLM client layer for the academic-research plugin.

Three transports cover six providers. Anthropic and Google need their
own SDKs; OpenAI, OpenRouter, Ollama and LM Studio all speak the same
`/v1/chat/completions` shape and share one client — the difference
between them is an endpoint and a credential, not a protocol.

Routing used to be a `startswith("claude-")` chain, duplicated between
`get_provider` and `require_credentials`, which meant the plugin could
only talk to the two providers whose model names it recognised. Now the
configured provider decides, and the model-name sniff survives only as a
fallback for projects configured before `[llm] provider` existed.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = SCRIPT_DIR.parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from core import providers  # noqa: E402
from core.config_loader import get, require  # noqa: E402
from core.providers import ProviderSpec  # noqa: E402

#: Attempts per request before giving up. The SDKs default to 2, which
#: is thin for this workload: `abstract_screen.py` runs 8 parallel
#: workers and `fulltext_code.py` 5, so 429s are the steady state rather
#: than an edge case. Set explicitly so the value is a decision rather
#: than an accident of whichever SDK version is installed.
DEFAULT_MAX_RETRIES = 5


def max_retries() -> int:
    raw = get("llm", "max_retries", env="ACADEMIC_RESEARCH_MAX_RETRIES")
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_MAX_RETRIES


class LLMProvider:
    """Base class for unified LLM providers."""

    def generate(
        self,
        model: str,
        system: str,
        prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 1000,
    ) -> str:
        """Generate a response from the LLM."""
        raise NotImplementedError("Subclasses must implement generate().")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def configured_provider() -> str:
    """The provider name from `[llm] provider`, or the registry default."""
    return get("llm", "provider", env=providers.PROVIDER_ENV) or (
        providers.DEFAULT_PROVIDER
    )


def _configured_base_url(spec: ProviderSpec) -> str:
    if not spec.base_url_env:
        return ""
    return get(
        providers.config_section(spec), "base_url", env=spec.base_url_env,
    ) or ""


def base_url_for(spec: ProviderSpec) -> str:
    """Effective base URL for `spec`: configured override, else default."""
    return providers.base_url_for(spec, _configured_base_url(spec))


def anthropic_base_url() -> str:
    """Optional Anthropic-compatible endpoint, or `""` for the real API.

    Kept as a named function because `ANTHROPIC_BASE_URL` is the
    documented way to point screening at Open WebUI or LM Studio
    (issue #1) and predates the provider registry. It is now one case of
    the general `base_url_env` mechanism.
    """
    return get("anthropic", "base_url", env="ANTHROPIC_BASE_URL") or ""


def credential_status(spec: ProviderSpec) -> tuple[bool, str]:
    """Whether `spec` can be called as currently configured.

    Returns `(ok, missing_env_var)`. Follows the same rules as
    `_api_key_for` below — local providers declare no credential, and an
    Anthropic-compatible endpoint has one it does not check — so an `ok`
    here means a run will get past credential resolution rather than
    merely that some key is on file.

    Read-only and network-free: the setup scripts use it to report state
    without touching the provider.
    """
    if not spec.api_key_env:
        return True, ""
    if spec.transport == "anthropic" and anthropic_base_url():
        return True, ""
    section = providers.config_section(spec)
    if get(section, "api_key", env=spec.api_key_env).strip():
        return True, ""
    return False, spec.api_key_env


def _api_key_for(spec: ProviderSpec, *, required: bool = True) -> str:
    """Credential for `spec`, or a placeholder when none is needed.

    Local providers declare no credential at all, and a self-hosted
    Anthropic-compatible endpoint has one it does not check — in both
    cases demanding a key would block a setup that works.
    """
    if not spec.api_key_env:
        return "not-required-for-local-endpoint"
    section = providers.config_section(spec)
    if spec.transport == "anthropic" and anthropic_base_url():
        return get(section, "api_key", env=spec.api_key_env) or (
            "not-required-for-local-endpoint"
        )
    if required:
        return require(section, "api_key", env=spec.api_key_env)
    return get(section, "api_key", env=spec.api_key_env)


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------


class AnthropicProvider(LLMProvider):
    """Anthropic, or any endpoint speaking the Anthropic Messages API."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        spec = providers.require("anthropic")
        if base_url is None:
            base_url = anthropic_base_url()
        if not api_key:
            api_key = _api_key_for(spec)
        try:
            import anthropic
        except ImportError as e:
            raise ImportError(
                "Anthropic Python SDK is required for Claude models. "
                "Ensure 'anthropic' is installed."
            ) from e
        kwargs: dict = {"api_key": api_key, "max_retries": max_retries()}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = anthropic.Anthropic(**kwargs)

    def generate(
        self,
        model: str,
        system: str,
        prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 1000,
    ) -> str:
        resp = self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()


class GeminiProvider(LLMProvider):
    """Google Gemini via the official Google GenAI SDK."""

    def __init__(self, api_key: str | None = None):
        if not api_key:
            api_key = _api_key_for(providers.require("google"))
        try:
            from google import genai
        except ImportError as e:
            raise ImportError(
                "Google GenAI SDK is required for Gemini models. "
                "Ensure 'google-genai' is installed."
            ) from e
        self.client = genai.Client(api_key=api_key)

    def generate(
        self,
        model: str,
        system: str,
        prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 1000,
    ) -> str:
        clean_model = model.removeprefix("models/")

        from google.genai import types

        config = types.GenerateContentConfig(temperature=temperature)
        if system:
            config.system_instruction = system
        if max_tokens:
            config.max_output_tokens = max_tokens

        try:
            response = self.client.models.generate_content(
                model=clean_model, contents=prompt, config=config,
            )
            return response.text.strip()
        except Exception as e:
            raise RuntimeError(f"Gemini API returned error: {e}") from e


class OpenAICompatProvider(LLMProvider):
    """OpenAI, OpenRouter, Ollama and LM Studio.

    One class, not four: all of them accept `POST /v1/chat/completions`
    with the same body, and differ only in base URL and credential. The
    local two ignore the key entirely, which is why a placeholder is
    acceptable there — the SDK requires *a* value, not a valid one.
    """

    def __init__(self, spec: ProviderSpec, api_key: str | None = None):
        self.spec = spec
        if not api_key:
            api_key = _api_key_for(spec, required=not spec.local)
        try:
            import openai
        except ImportError as e:
            raise ImportError(
                f"The OpenAI Python SDK is required for {spec.label}. "
                f"Ensure 'openai' is installed."
            ) from e
        base = base_url_for(spec)
        self.client = openai.OpenAI(
            api_key=api_key or "not-required-for-local-endpoint",
            # Every one of these providers serves the OpenAI surface
            # under /v1, including Ollama — whose *listing* endpoint is
            # its own /api/tags, but whose chat endpoint is not.
            base_url=f"{base.rstrip('/')}/v1",
            max_retries=max_retries(),
        )

    def generate(
        self,
        model: str,
        system: str,
        prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 1000,
    ) -> str:
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        try:
            resp = self.client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as e:
            raise RuntimeError(f"{self.spec.label} returned error: {e}") from e
        return (resp.choices[0].message.content or "").strip()


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

#: Fallback only. A project configured before `[llm] provider` existed
#: has no provider recorded, and guessing from the model name is better
#: than failing — but it is a guess, and it is why `/setup` should be
#: re-run rather than relied upon.
_LEGACY_PREFIXES = (
    ("claude-", "anthropic"),
    ("gemini-", "google"),
    ("models/gemini", "google"),
    ("gpt-", "openai"),
    ("o1-", "openai"),
    ("o3-", "openai"),
)


def resolve_provider(model_name: str = "", provider_hint: str = "") -> ProviderSpec:
    """Which provider should serve `model_name`.

    Order: an explicit hint, then `[llm] provider`, then the model-name
    sniff for pre-registry projects. The configured provider wins over
    the model name on purpose — an OpenRouter user legitimately asks for
    `anthropic/claude-sonnet-5`, and routing that to Anthropic's own API
    would use the wrong endpoint and the wrong key.
    """
    if provider_hint:
        return providers.require(provider_hint)
    configured = get("llm", "provider", env=providers.PROVIDER_ENV)
    if configured:
        return providers.require(configured)
    lowered = (model_name or "").lower()
    for prefix, name in _LEGACY_PREFIXES:
        if lowered.startswith(prefix):
            return providers.require(name)
    return providers.require(providers.DEFAULT_PROVIDER)


def require_credentials(model_name: str = "", provider_hint: str = "") -> None:
    """Fail fast when the credential this run needs is missing.

    Called before a run starts so a missing key surfaces immediately
    rather than after the first item has been fetched. Shares
    `resolve_provider` with `get_provider`, which the two prefix chains
    this replaces did not — they could and did disagree.
    """
    spec = resolve_provider(model_name, provider_hint)
    if not spec.api_key_env:
        return
    if spec.transport == "anthropic" and anthropic_base_url():
        return
    require(providers.config_section(spec), "api_key", env=spec.api_key_env)


def get_provider(model_name: str = "", provider_hint: str = "") -> LLMProvider:
    """Build the client for the provider serving `model_name`."""
    spec = resolve_provider(model_name, provider_hint)
    if spec.transport == "anthropic":
        return AnthropicProvider()
    if spec.transport == "google":
        return GeminiProvider()
    return OpenAICompatProvider(spec)
