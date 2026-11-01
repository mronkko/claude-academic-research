"""Unified LLM client layer for the academic-research plugin.

Three transports cover every provider in the registry. Anthropic and
Google need their own SDKs; OpenAI, OpenRouter, an institutional
gateway, Ollama and LM Studio all speak the same
`/v1/chat/completions` shape and share one client — the difference
between them is an endpoint and a credential, not a protocol. (The
count used to be written out here and drifted every time the registry
grew; `core.providers` is the place that knows.)

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
        request_params: dict | None = None,
    ) -> str:
        """Generate a response from the LLM.

        `request_params` are extra request fields; only the Anthropic
        provider uses them (see `anthropic_request`).
        """
        raise NotImplementedError("Subclasses must implement generate().")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def configured_provider() -> str:
    """The provider name from `[llm] provider`, or the registry default."""
    return get("llm", "provider", env=providers.PROVIDER_ENV) or (
        providers.DEFAULT_PROVIDER
    )


def credential_env(spec: ProviderSpec) -> str:
    """Name of the environment variable holding `spec`'s API key, if any.

    Usually the registry's own constant. A bring-your-own-endpoint
    provider has none — the plugin cannot know what an institution's
    variable is called — so it may declare one in config:

        [gateway]
        api_key_env = "UNI_LLM_TOKEN"

    That indirection exists so a user who already exports the secret
    under their own name is not made to export it a second time under
    a name this plugin invented. Returns `""` when the key lives only in
    `config.toml`, which is the ordinary case and what `config_loader`
    already handles by skipping the environment lookup.
    """
    if spec.api_key_env:
        return spec.api_key_env
    if spec.byo_endpoint:
        return get(providers.config_section(spec), "api_key_env") or ""
    return ""


def base_url_env(spec: ProviderSpec) -> str:
    """Name of the environment variable holding `spec`'s base URL, if any.

    Same rule as `credential_env`, via `[<section>] base_url_env`.
    """
    if spec.base_url_env:
        return spec.base_url_env
    if spec.byo_endpoint:
        return get(providers.config_section(spec), "base_url_env") or ""
    return ""


def _configured_base_url(spec: ProviderSpec) -> str:
    if not (spec.base_url_env or spec.byo_endpoint):
        return ""
    return get(
        providers.config_section(spec), "base_url", env=base_url_env(spec),
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

    Returns `(ok, where_it_should_go)` — an environment variable name
    when the provider has one, else the `config.toml` location, because
    for a bring-your-own provider there is no variable to name and
    telling the user to "set ''" would be useless.

    Follows the same rules as `_api_key_for` below — local providers
    declare no credential, and an Anthropic-compatible endpoint has one
    it does not check — so an `ok` here means a run will get past
    credential resolution rather than merely that some key is on file.

    Read-only and network-free: the setup scripts use it to report state
    without touching the provider.
    """
    # `local`, not `api_key_env`: a bring-your-own provider also has no
    # registry-declared variable, but it very much does need a key.
    if spec.local:
        return True, ""
    if spec.transport == "anthropic" and anthropic_base_url():
        return True, ""
    section = providers.config_section(spec)
    if get(section, "api_key", env=credential_env(spec)).strip():
        return True, ""
    return False, credential_env(spec) or f"config.toml [{section}].api_key"


def _api_key_for(spec: ProviderSpec, *, required: bool = True) -> str:
    """Credential for `spec`, or a placeholder when none is needed.

    Local providers declare no credential at all, and a self-hosted
    Anthropic-compatible endpoint has one it does not check — in both
    cases demanding a key would block a setup that works.
    """
    if spec.local:
        return "not-required-for-local-endpoint"
    section = providers.config_section(spec)
    env = credential_env(spec)
    if spec.transport == "anthropic" and anthropic_base_url():
        return get(section, "api_key", env=env) or (
            "not-required-for-local-endpoint"
        )
    if required:
        return require(section, "api_key", env=env)
    return get(section, "api_key", env=env)


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------


#: Above this `max_tokens` a call is streamed: the SDK refuses a
#: non-streaming call whose budget implies more than ten minutes.
_NONSTREAMING_MAX_TOKENS = 16000

#: The Messages API fields the SDK takes as keywords; everything else a
#: request carries goes through `extra_body`, so a field newer than the
#: installed SDK still reaches the API.
_SDK_KEYWORDS = ("model", "max_tokens", "system", "messages")


def anthropic_request(
    model: str, system: str, prompt: str, *, max_tokens: int,
    temperature: float = 0.0, params: dict | None = None, cache_ttl: str = "5m",
) -> tuple[dict, dict]:
    """`(body, headers)` for one Messages API request.

    The system prompt — the same for every paper in a run — carries a
    cache breakpoint with `cache_ttl` (or the review's
    `cache_system_prompt`), so after the first request it is read at a
    tenth of the input price. A prompt under the model's cacheable
    minimum is simply not cached; the API does not error.

    Claude 5+ start from `providers.CLAUDE_REQUEST_DEFAULTS` — the output
    cap and medium effort — in place of the caller's `max_tokens`, which
    is sized for the answer alone and cannot know how long the model will
    think. Older models keep the caller's budget. `params` (the review's
    `*_REQUEST_PARAMS`) go on top of either; `extra_headers` among them
    become HTTP headers, e.g. `{"anthropic-beta": "..."}`.
    """
    body: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }
    if providers.anthropic_thinks_by_default(model):
        body = providers.merge_request_params(body, providers.CLAUDE_REQUEST_DEFAULTS)
    if providers.anthropic_accepts_temperature(model):
        body["temperature"] = temperature
    body = providers.merge_request_params(
        body, providers.check_request_params(params))
    headers = body.pop("extra_headers", None) or {}
    ttl = body.pop(providers.CACHE_SYSTEM_PROMPT, None)
    ttl = cache_ttl if ttl is None else ttl
    if ttl and system:
        control = {"type": "ephemeral", **({"ttl": "1h"} if ttl == "1h" else {})}
        body["system"] = [{"type": "text", "text": system, "cache_control": control}]
    return body, headers


def _response_text(resp) -> str:
    """Join the `text` blocks of a Messages response.

    Claude 5.x models think by default, so `content[0]` is usually a
    `thinking` block with no `.text`. Blocks are picked by type.
    """
    text = "".join(
        b.text for b in resp.content if getattr(b, "type", None) == "text"
    )
    if not text.strip():
        kinds = [getattr(b, "type", "?") for b in resp.content]
        raise ValueError(
            f"Anthropic response has no text block (stop_reason="
            f"{resp.stop_reason!r}, blocks={kinds}); thinking may have "
            "used the whole max_tokens budget."
        )
    return text.strip()


def _rejects_temperature(exc: Exception) -> bool:
    """True for a 400 whose message blames the `temperature` parameter."""
    return getattr(exc, "status_code", None) == 400 and "temperature" in str(exc)


class AnthropicProvider(LLMProvider):
    """Anthropic, or any endpoint speaking the Anthropic Messages API."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 request_params: dict | None = None):
        #: The review's request params, applied to every call this client makes.
        self.request_params = providers.check_request_params(request_params)
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
        request_params: dict | None = None,
    ) -> str:
        # Claude 5+ runs at the model's default sampling: the API rejects
        # `temperature` there (see `providers.anthropic_accepts_temperature`).
        body, headers = anthropic_request(
            model, system, prompt, max_tokens=max_tokens,
            temperature=temperature,
            params=providers.merge_request_params(
                self.request_params, request_params or {}, keep_none=True),
        )
        return _response_text(self._create(body, headers))

    def _create(self, body: dict, headers: dict):
        kw = {k: body[k] for k in _SDK_KEYWORDS if k in body}
        extra = {k: v for k, v in body.items() if k not in _SDK_KEYWORDS}
        if headers:
            kw["extra_headers"] = headers
        try:
            return self._send({**kw, **({"extra_body": extra} if extra else {})})
        except Exception as e:
            if "temperature" not in extra or not _rejects_temperature(e):
                raise
            extra = {k: v for k, v in extra.items() if k != "temperature"}
            return self._send({**kw, **({"extra_body": extra} if extra else {})})

    def _send(self, kw: dict):
        if kw["max_tokens"] > _NONSTREAMING_MAX_TOKENS:
            with self.client.messages.stream(**kw) as stream:
                return stream.get_final_message()
        return self.client.messages.create(**kw)


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
        request_params: dict | None = None,
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
        if spec.byo_endpoint and not base:
            # Otherwise the SDK is handed base_url="/v1" and the failure
            # surfaces per item, mid-run, as an opaque connection error.
            raise RuntimeError(
                f"{spec.label} has no endpoint configured. Set "
                f"{providers.base_url_location(spec, base_url_env(spec))} "
                f"(or run `/setup`) — the plugin ships no default "
                f"address for it."
            )
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
        request_params: dict | None = None,
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
    if spec.local:
        return
    if spec.transport == "anthropic" and anthropic_base_url():
        return
    require(
        providers.config_section(spec), "api_key", env=credential_env(spec),
    )


def get_provider(model_name: str = "", provider_hint: str = "",
                 request_params: dict | None = None) -> LLMProvider:
    """Build the client for the provider serving `model_name`.

    `request_params` are Anthropic-shaped; another provider ignores them,
    with a warning, rather than sending fields its API would reject.
    """
    spec = resolve_provider(model_name, provider_hint)
    if spec.transport == "anthropic":
        return AnthropicProvider(request_params=request_params)
    if request_params:
        print(f"WARNING: request params ({', '.join(request_params)}) apply to "
              f"Anthropic models only; ignored for {model_name}.", flush=True)
    if spec.transport == "google":
        return GeminiProvider()
    return OpenAICompatProvider(spec)


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------


def preflight(model_name: str, provider_hint: str = ""):
    """Probe the provider once before a run. Returns a `ConnectionResult`.

    `require_credentials` only proves a key is *present*. That is not the
    same as usable: the failure that motivated this was a key which was
    present, valid, and out of quota. One real request is the only thing
    that tells those apart, and it costs about four tokens.
    """
    from core import model_health

    spec = resolve_provider(model_name, provider_hint)
    return model_health.check_connection(
        spec,
        model_name,
        # `not spec.local` rather than `spec.api_key_env`: a gateway
        # declares no variable but still has a key to send.
        api_key=_api_key_for(spec, required=False) if not spec.local else "",
        base_url=_configured_base_url(spec),
    )


def preflight_or_exit(model_name: str, provider_hint: str = "") -> None:
    """`preflight`, but abort the process when the model cannot be reached.

    Called by the screening and coding orchestrators before their main
    loop. Exiting here rather than letting the loop discover it per item
    is the entire lesson of the incident this was written for: 244 items
    each spent 131 seconds in a retry ladder against an exhausted quota,
    wrote 244 `decision=error` rows, and reported nothing legible until
    the run was over.
    """
    result = preflight(model_name, provider_hint)
    if result.ok:
        print(result.format(), flush=True)
        return
    print("", flush=True)
    print(result.format(), file=sys.stderr, flush=True)
    print("", file=sys.stderr, flush=True)
    raise SystemExit(2)


def classify_failure(exc: BaseException):
    """Classify a mid-run SDK exception. Returns a `ConnectionResult`.

    Used by the worker loops so a per-item failure says *why* rather
    than stringifying whatever the SDK raised, and so the run can tell a
    transient blip apart from a condition that will fail every remaining
    item too.
    """
    from core import model_health

    status, detail, http_status = model_health.classify_exception(exc)
    return model_health.ConnectionResult(
        status=status,
        provider=configured_provider(),
        model="",
        detail=detail,
        http_status=http_status,
    )
