"""Which provider serves a run, and which credential it demands.

Routing used to be a `startswith("claude-")` chain written out twice —
once in `get_provider`, once in `require_credentials` — so the plugin
could only reach the two providers whose model names it recognised, and
the two copies could disagree about which one a name belonged to.

The configured provider now decides, and both functions ask the same
`resolve_provider`. These tests pin the routing table and, more
importantly, the two cases where the obvious rule is wrong.
"""

from __future__ import annotations

import pytest
from core import llm_provider, providers


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch):
    """Neutralise the developer's real config.toml and environment."""
    monkeypatch.setattr(llm_provider, "get", lambda *_a, **_kw: "")
    yield


def _with_config(monkeypatch, **values: str):
    """Patch `get(section, key)` lookups by `"section.key"`."""
    def fake_get(section, key, env=None, default=""):
        return values.get(f"{section}.{key}", default)

    monkeypatch.setattr(llm_provider, "get", fake_get)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "transport"),
    [
        ("anthropic", "anthropic"),
        ("google", "google"),
        ("openai", "openai_compat"),
        ("openrouter", "openai_compat"),
        ("ollama", "openai_compat"),
        ("lmstudio", "openai_compat"),
    ],
)
def test_configured_provider_selects_its_transport(monkeypatch, provider, transport):
    _with_config(monkeypatch, **{"llm.provider": provider})
    assert llm_provider.resolve_provider("some-model").transport == transport


def test_the_configured_provider_beats_the_model_name(monkeypatch) -> None:
    """An OpenRouter user legitimately asks for `anthropic/claude-sonnet-5`.

    Sniffing the name would send that to Anthropic's own API, with the
    wrong endpoint and the wrong key — a confusing 401 for a request
    that was correct.
    """
    _with_config(monkeypatch, **{"llm.provider": "openrouter"})
    spec = llm_provider.resolve_provider("anthropic/claude-sonnet-5")
    assert spec.name == "openrouter"


def test_an_explicit_hint_beats_everything(monkeypatch) -> None:
    _with_config(monkeypatch, **{"llm.provider": "anthropic"})
    assert llm_provider.resolve_provider("x", provider_hint="ollama").name == "ollama"


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-sonnet-5", "anthropic"),
        ("gemini-2.5-pro", "google"),
        ("gpt-5", "openai"),
    ],
)
def test_unconfigured_projects_fall_back_to_the_name_sniff(model, expected) -> None:
    """A project configured before `[llm] provider` existed still runs.

    A guess is better than a hard failure here, but it is a guess —
    which is why `/setup` records the provider explicitly.
    """
    assert llm_provider.resolve_provider(model).name == expected


def test_an_unrecognisable_name_falls_back_to_the_default() -> None:
    assert (
        llm_provider.resolve_provider("qwen3-30b").name
        == providers.DEFAULT_PROVIDER
    )


def test_an_unknown_provider_name_is_rejected_with_the_valid_set(monkeypatch):
    _with_config(monkeypatch, **{"llm.provider": "gpt4all"})
    with pytest.raises(ValueError, match="ollama"):
        llm_provider.resolve_provider("x")


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def test_local_providers_need_no_credential(monkeypatch) -> None:
    """Issue #1: a local-only user should need no key at all.

    Previously the Anthropic client invented the placeholder
    "not-required-for-local-endpoint"; now the registry says outright
    that Ollama and LM Studio declare no credential.
    """
    for name in ("ollama", "lmstudio"):
        _with_config(monkeypatch, **{"llm.provider": name})
        llm_provider.require_credentials("whatever")  # must not raise


def test_a_hosted_provider_without_its_key_fails_fast(monkeypatch) -> None:
    _with_config(monkeypatch, **{"llm.provider": "openai"})
    monkeypatch.setattr(
        llm_provider, "require",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("missing OPENAI_API_KEY")),
    )
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        llm_provider.require_credentials("gpt-5")


def test_anthropic_base_url_makes_the_key_optional(monkeypatch) -> None:
    """The other half of issue #1: Open WebUI / LM Studio speak the
    Anthropic Messages API, and do not check the key."""
    _with_config(
        monkeypatch,
        **{"llm.provider": "anthropic", "anthropic.base_url": "http://localhost:1234"},
    )
    monkeypatch.setattr(
        llm_provider, "require",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("should not require")),
    )
    llm_provider.require_credentials("qwen3-30b")  # must not raise


def test_routing_and_credentials_cannot_disagree(monkeypatch) -> None:
    """They used to be two hand-written prefix chains.

    Both now go through `resolve_provider`, so a name can never be
    routed to one provider while a different one's key is demanded.
    """
    seen: list[str] = []

    def fake_require(section, _key, env=None):
        seen.append(section)
        return "key"

    for name in ("anthropic", "google", "openai", "openrouter"):
        _with_config(monkeypatch, **{"llm.provider": name})
        monkeypatch.setattr(llm_provider, "require", fake_require)
        seen.clear()
        llm_provider.require_credentials("model-x")
        routed = llm_provider.resolve_provider("model-x")
        expected_section = routed.name if routed.name != "google" else "gemini"
        assert seen == [expected_section], (
            f"{name}: routed to {routed.name} but required {seen}"
        )


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------


def test_retries_are_set_explicitly_not_inherited() -> None:
    """8 parallel workers against a rate-limited API makes 429s the
    steady state; the SDK default of 2 attempts is thin."""
    assert llm_provider.DEFAULT_MAX_RETRIES >= 5
    assert llm_provider.max_retries() == llm_provider.DEFAULT_MAX_RETRIES


def test_retries_are_configurable(monkeypatch) -> None:
    _with_config(monkeypatch, **{"llm.max_retries": "9"})
    assert llm_provider.max_retries() == 9


def test_a_nonsense_retry_value_falls_back_to_the_default(monkeypatch) -> None:
    _with_config(monkeypatch, **{"llm.max_retries": "lots"})
    assert llm_provider.max_retries() == llm_provider.DEFAULT_MAX_RETRIES


# ---------------------------------------------------------------------------
# Base URLs
# ---------------------------------------------------------------------------


def test_local_defaults_point_at_localhost() -> None:
    assert llm_provider.base_url_for(providers.get("ollama")).endswith(":11434")
    assert llm_provider.base_url_for(providers.get("lmstudio")).endswith(":1234")


def test_a_configured_base_url_overrides_the_default(monkeypatch) -> None:
    _with_config(monkeypatch, **{"ollama.base_url": "http://gpu-box.local:11434/"})
    assert (
        llm_provider.base_url_for(providers.get("ollama"))
        == "http://gpu-box.local:11434"
    )
