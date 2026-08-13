"""Tier-based model selection, without a single pinned version in code.

The defect this replaces: `scripts/core/models.py` hardcoded five model
IDs, so every provider release made the plugin ship a stale default to
everyone who had already installed it. Now the code knows only that
Anthropic marks cheap models with "haiku", and asks the API which ones
exist today.

These tests use recorded-shape listings rather than live calls, so they
pin the *selection rule* — which is the part that has to stay correct
when the model names change under it.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from core import model_discovery as md
from core import providers

ROOT = Path(__file__).resolve().parents[2]


def _models(*ids: str) -> list[md.ModelInfo]:
    return [md.ModelInfo(id=i) for i in ids]


# ---------------------------------------------------------------------------
# No versions in code — the property the whole design exists for
# ---------------------------------------------------------------------------


_VERSIONED = __import__("re").compile(
    r"\b(claude-[a-z]+-[\d.]+|gemini-[\d.]+|gpt-[\d.]+[a-z-]*)\b",
)


def _code_strings(path: Path) -> list[str]:
    """Every string literal in `path` except docstrings.

    Comments never enter the AST, and docstrings are skipped explicitly,
    so prose that *explains* a version (the `tier_excludes` note names
    gemini-2.5-flash-lite to say why the exclusion exists) does not
    count as pinning one.
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        ast.get_docstring(node, clean=False)
        for node in ast.walk(tree)
        if isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
        )
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value not in docstrings
    ]


def test_providers_module_contains_no_model_version() -> None:
    """A version string here is the bug we are removing.

    `providers.py` may name a *family* ("haiku", "sonnet", "flash") —
    those are stable. A version is what goes stale, and a plugin that
    pins one ships a broken default to everyone who installed it before
    the next release.
    """
    pinned = {
        s for s in _code_strings(ROOT / "scripts" / "core" / "providers.py")
        if _VERSIONED.search(s)
    }
    assert not pinned, f"providers.py pins model versions: {pinned}"


def test_the_pipelines_pin_no_model_version_either() -> None:
    """The same rule for everything that runs a screening pass."""
    offenders: dict[str, set[str]] = {}
    for rel in (
        "scripts/core/models.py",
        "scripts/core/llm_provider.py",
        "scripts/core/model_discovery.py",
        "scripts/pipelines/abstract_screen.py",
        "scripts/pipelines/fulltext_code.py",
    ):
        path = ROOT / rel
        if not path.exists():
            continue
        found = {s for s in _code_strings(path) if _VERSIONED.search(s)}
        if found:
            offenders[rel] = found
    assert not offenders, f"model versions pinned in code: {offenders}"


def test_the_catalog_is_the_only_place_versions_live() -> None:
    catalog = tomllib.loads(
        (ROOT / "templates" / "model_catalog.toml").read_text(encoding="utf-8"),
    )
    assert catalog["anthropic"]["fast"]["model"].startswith("claude-haiku")
    assert "checked" in catalog, "the catalog must record when it was verified"


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def test_anthropic_tiers() -> None:
    spec = providers.get("anthropic")
    listing = _models(
        "claude-haiku-4-5-20251001", "claude-haiku-3-5-20241022",
        "claude-sonnet-5", "claude-sonnet-4-6", "claude-opus-5",
    )
    assert md.pick_for_tier(listing, spec, "fast") == "claude-haiku-4-5-20251001"
    assert md.pick_for_tier(listing, spec, "balanced") == "claude-sonnet-5"
    assert md.pick_for_tier(listing, spec, "deep") == "claude-opus-5"


def test_hint_order_decides_within_a_tier() -> None:
    """`deep: ("opus", "sonnet")` means Opus, or Sonnet if there is none.

    Without hint ranking, "claude-sonnet-5" beat "claude-opus-5" on the
    string tiebreak — the plugin would have quietly picked the weaker
    model for the stage that needs the stronger one.
    """
    spec = providers.get("anthropic")
    with_opus = _models("claude-sonnet-5", "claude-opus-5")
    without = _models("claude-sonnet-5", "claude-haiku-4-5")
    assert md.pick_for_tier(with_opus, spec, "deep") == "claude-opus-5"
    assert md.pick_for_tier(without, spec, "deep") == "claude-sonnet-5"


def test_a_newer_generation_beats_a_newer_timestamp() -> None:
    """Version numbers outrank `created`.

    Providers report `created` inconsistently — absent on several, and
    not monotonic with capability. Picking an older generation because
    it happens to carry a later timestamp is the failure that matters.
    """
    spec = providers.get("anthropic")
    listing = [
        md.ModelInfo(id="claude-haiku-4-5", created=1_000),
        md.ModelInfo(id="claude-haiku-3-5", created=9_999_999),
    ]
    assert md.pick_for_tier(listing, spec, "fast") == "claude-haiku-4-5"


def test_nested_names_do_not_leak_into_a_higher_tier() -> None:
    """"gemini-2.5-flash-lite" contains "flash".

    Without an explicit exclusion the balanced tier picks the cheap
    model, and every full-text coding run is silently downgraded.
    """
    spec = providers.get("google")
    listing = _models("gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro")
    assert md.pick_for_tier(listing, spec, "fast") == "gemini-2.5-flash-lite"
    assert md.pick_for_tier(listing, spec, "balanced") == "gemini-2.5-flash"
    assert md.pick_for_tier(listing, spec, "deep") == "gemini-2.5-pro"


def test_openai_deep_ignores_non_chat_models() -> None:
    """An OpenAI account lists embeddings, TTS and moderation too."""
    spec = providers.get("openai")
    listing = _models(
        "gpt-5", "gpt-5-mini", "gpt-5-nano", "gpt-4o",
        "text-embedding-3-large", "whisper-1", "tts-1", "omni-moderation-latest",
    )
    assert md.pick_for_tier(listing, spec, "deep") == "gpt-5"
    assert md.pick_for_tier(listing, spec, "fast") == "gpt-5-nano"


def test_local_providers_pick_by_parameter_count() -> None:
    spec = providers.get("ollama")
    listing = _models("llama3.3:70b", "llama3.2:3b", "qwen2.5:14b")
    assert md.pick_for_tier(listing, spec, "fast") == "llama3.2:3b"
    assert md.pick_for_tier(listing, spec, "deep") == "llama3.3:70b"


def test_no_match_returns_empty_not_a_wrong_guess() -> None:
    spec = providers.get("anthropic")
    assert md.pick_for_tier(_models("some-unrelated-model"), spec, "fast") == ""


# ---------------------------------------------------------------------------
# Listing shapes
# ---------------------------------------------------------------------------


def test_normalises_each_providers_listing_shape() -> None:
    """Four shapes in the wild; all must flatten to ModelInfo."""
    anthropic = md._normalise(
        providers.get("anthropic"),
        {"data": [{"id": "claude-opus-5", "created": 17}]},
    )
    assert anthropic[0].id == "claude-opus-5" and anthropic[0].created == 17

    google = md._normalise(
        providers.get("google"),
        {"models": [{"name": "models/gemini-2.5-pro", "displayName": "Pro"}]},
    )
    assert google[0].id == "gemini-2.5-pro"  # the models/ prefix is stripped

    ollama = md._normalise(
        providers.get("ollama"), {"models": [{"name": "llama3.2:3b"}]},
    )
    assert ollama[0].id == "llama3.2:3b"


# ---------------------------------------------------------------------------
# Fallback — must be loud, never silent
# ---------------------------------------------------------------------------


def test_unreachable_provider_falls_back_to_the_catalog(monkeypatch) -> None:
    def boom(*_a, **_kw):
        raise md.DiscoveryError("could not reach api.anthropic.com")

    monkeypatch.setattr(md, "list_models", boom)
    res = md.resolve_tier(providers.get("anthropic"), "fast")
    assert res.source == "catalog"
    assert res.model.startswith("claude-haiku")
    assert res.is_stale_risk, "a catalog answer must be flagged as possibly stale"
    assert "api.anthropic.com" in res.detail, "the reason must reach the user"


def test_discovery_success_is_not_flagged_stale(monkeypatch) -> None:
    monkeypatch.setattr(
        md, "list_models", lambda *_a, **_kw: _models("claude-haiku-9-9"),
    )
    res = md.resolve_tier(providers.get("anthropic"), "fast")
    assert res.source == "discovered"
    assert res.model == "claude-haiku-9-9"
    assert not res.is_stale_risk


def test_a_provider_with_no_catalog_entry_says_so(monkeypatch) -> None:
    """OpenRouter proxies other providers; guessing a pin would be worse
    than reporting the failure."""
    monkeypatch.setattr(
        md, "list_models",
        lambda *_a, **_kw: (_ for _ in ()).throw(md.DiscoveryError("offline")),
    )
    res = md.resolve_tier(providers.get("openrouter"), "deep")
    assert res.source == "none"
    assert res.model == ""


def test_resolve_tier_never_raises(monkeypatch) -> None:
    """A bootstrap that cannot reach the provider must still finish."""
    monkeypatch.setattr(
        md, "list_models",
        lambda *_a, **_kw: (_ for _ in ()).throw(md.DiscoveryError("nope")),
    )
    for spec in providers.PROVIDERS:
        for tier in providers.TIERS:
            md.resolve_tier(spec, tier)  # must not raise


# ---------------------------------------------------------------------------
# Cost estimate
# ---------------------------------------------------------------------------


def test_abstract_screening_estimate_is_in_the_right_range() -> None:
    """250 abstracts on Anthropic's fast tier is well under a dollar."""
    cost = md.estimate_cost("anthropic", "fast", 250)
    assert 0.20 < cost < 0.60, cost


def test_fulltext_estimate_matches_the_documented_figure() -> None:
    """125 papers on the balanced tier — the run this plugin was tuned on."""
    cost = md.estimate_cost("anthropic", "balanced", 125, stage="fulltext_coding")
    assert 5.0 < cost < 9.0, cost


def test_local_providers_cost_nothing() -> None:
    assert md.estimate_cost("ollama", "deep", 1000, stage="fulltext_coding") == 0.0


def test_unknown_provider_returns_zero_not_a_guess() -> None:
    """Caller must say "no estimate available", not "free"."""
    assert md.estimate_cost("openrouter", "deep", 100) == 0.0


def test_estimate_scales_linearly() -> None:
    one = md.estimate_cost("anthropic", "fast", 100)
    four = md.estimate_cost("anthropic", "fast", 400)
    assert four == pytest.approx(one * 4)


# ---------------------------------------------------------------------------
# Registry integrity
# ---------------------------------------------------------------------------


def test_every_provider_declares_the_tiers_it_supports() -> None:
    for spec in providers.PROVIDERS:
        assert spec.tier_hints.get("fast"), f"{spec.name} has no fast tier"
        assert spec.tier_hints.get("deep"), f"{spec.name} has no deep tier"


def test_local_providers_need_no_key() -> None:
    for spec in providers.PROVIDERS:
        if spec.local:
            assert spec.api_key_env == "", (
                f"{spec.name} is local but demands {spec.api_key_env}"
            )
            assert spec.default_base_url.startswith("http://localhost")


def test_hosted_providers_name_a_key_and_where_to_get_it() -> None:
    for spec in providers.PROVIDERS:
        if not spec.local:
            assert spec.api_key_env, f"{spec.name} names no credential"
            assert spec.key_url, f"{spec.name} does not say where to get a key"


def test_transports_are_all_implementable() -> None:
    assert {s.transport for s in providers.PROVIDERS} == {
        "anthropic", "google", "openai_compat",
    }


def test_require_lists_the_valid_names_on_error() -> None:
    with pytest.raises(ValueError, match="anthropic"):
        providers.require("gpt4all")
