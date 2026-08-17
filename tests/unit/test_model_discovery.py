"""Model discovery, without a single pinned version in code.

The defect this replaces: `scripts/core/models.py` hardcoded five model
IDs, so every provider release made the plugin ship a stale default to
everyone who had already installed it. Now nothing in code names a
model — the plugin asks the provider what it serves and an agent and
user choose from the answer.

An earlier pass tried to make that choice automatically, ranking by tier
hints and version numbers. `test_nothing_here_picks_a_model` guards
against it coming back, and says why.

These tests use recorded-shape listings rather than live calls.
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
    so prose that *explains* a version (the module docstring names
    claude-haiku-4.5:batch to say why auto-selection was removed) does
    not count as pinning one.
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
# Nothing selects a model — the property that replaced selection
# ---------------------------------------------------------------------------


def test_nothing_here_picks_a_model() -> None:
    """Auto-selection was removed; it must not come back by accident.

    It shipped once and was wrong for two of six providers against their
    live listings. On OpenRouter the fast tier resolved to
    `anthropic/claude-haiku-4.5:batch` — the asynchronous Batch API,
    which a synchronous screening run cannot use — because the suffix
    won a string tiebreak against the plain ID. On Google the deep tier
    resolved to `deep-research-pro-preview-12-2025`, because `12-2025`
    parses as version 12.2025 and outranks every real Gemini.

    Neither is fixable by tuning. Suppressing them means a blocklist of
    provider-specific substrings that goes stale exactly as fast as the
    hardcoded model IDs this design exists to eliminate — and the one
    caller, `resolve_models.py`, always runs with an agent reading its
    output and a user present to confirm.
    """
    banned = {"pick_for_tier", "resolve_tier", "_sort_key", "Resolution"}
    assert not banned & set(dir(md)), (
        f"model_discovery regrew automatic selection: {banned & set(dir(md))}"
    )
    assert not {"matches_tier", "hint_rank"} & set(dir(providers))
    assert not any(getattr(s, "tier_excludes", None) for s in providers.PROVIDERS)


def test_tier_hints_classify_but_do_not_rank() -> None:
    """`tier_of` still has to place a model, for pricing and the listing.

    Cheapest-first, so a model matching several hints lands where a
    cost-conscious user expects: "gemini-2.5-flash" is fast, not
    balanced, even though "flash" appears in both tiers' hints.
    """
    google = providers.get("google")
    assert providers.tier_of(google, "gemini-2.5-flash") == "fast"
    assert providers.tier_of(google, "gemini-2.5-pro") == "deep"

    anthropic = providers.get("anthropic")
    assert providers.tier_of(anthropic, "claude-haiku-4-5") == "fast"
    assert providers.tier_of(anthropic, "claude-sonnet-5") == "balanced"
    assert providers.tier_of(anthropic, "claude-opus-5") == "deep"


def test_an_unplaceable_model_is_labelled_not_guessed() -> None:
    """The listing column must admit ignorance rather than invent a tier."""
    spec = providers.get("anthropic")
    assert providers.tier_of(spec, "some-unrelated-model") == ""
    assert providers.tier_label(spec, "some-unrelated-model") == "?"
    assert providers.tier_label(spec, "claude-haiku-4-5") == "fast"


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

    # An institutional gateway is plain OpenAI on the wire; the models it
    # lists are whatever that institution hosts.
    gateway = md._normalise(
        providers.get("gateway"),
        {"data": [{"id": "Qwen/Qwen3-32B", "created": 42}]},
    )
    assert gateway[0].id == "Qwen/Qwen3-32B" and gateway[0].created == 42


# ---------------------------------------------------------------------------
# Catalogue fallback — the only path with nobody in the loop
# ---------------------------------------------------------------------------


def test_the_catalog_offers_a_menu_per_tier() -> None:
    """What `resolve_models.py` shows when the provider cannot be asked."""
    got = md.catalog_suggestions("anthropic")
    assert [tier for tier, _m in got] == ["fast", "balanced", "deep"]
    assert all(model for _t, model in got)


def test_a_provider_with_no_catalog_entry_offers_nothing(monkeypatch) -> None:
    """OpenRouter proxies other providers; suggesting a pin would be worse
    than reporting that there is none."""
    assert md.catalog_suggestions("openrouter") == []
    assert md.catalog_suggestions("ollama") == [], (
        "a local provider serves whatever the user pulled"
    )
    assert md.catalog_suggestions("gateway") == [], (
        "an institution hosts whichever models it chose; the plugin "
        "cannot suggest one, and must not price one either"
    )


def test_catalog_lookup_survives_a_missing_file(tmp_path) -> None:
    """A malformed or absent catalogue must degrade, not raise — this is
    already the failure path."""
    assert md.load_catalog(tmp_path / "nope.toml") == {}
    assert md.catalog_model("anthropic", "fast", {}) == ""
    assert md.catalog_suggestions("anthropic", {}) == []


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
    """Three hosting kinds, three obligations.

    A vendor provider must name a key and a page to get it from. A
    bring-your-own-endpoint provider cannot: its key page is whatever
    its institution runs, so it owes an address variable instead, and
    must ship no hostname at all. Local providers are covered above.
    """
    for spec in providers.PROVIDERS:
        if spec.local:
            continue
        if spec.byo_endpoint:
            # The inverse of a vendor provider: nothing is knowable in
            # advance, so it must declare *no* address, *no* key page,
            # and no environment variable either. The settings live in
            # config.toml under the provider's own section.
            assert not spec.default_base_url, (
                f"{spec.name} is bring-your-own-endpoint but ships "
                f"{spec.default_base_url!r} as a default"
            )
            assert not spec.api_key_env, (
                f"{spec.name} is bring-your-own-endpoint but claims the "
                f"variable {spec.api_key_env} — an invented name would "
                f"collide with whatever the user already exports"
            )
            assert not spec.base_url_env, (
                f"{spec.name} is bring-your-own-endpoint but claims "
                f"{spec.base_url_env}"
            )
        else:
            assert spec.api_key_env, f"{spec.name} names no credential"
            assert spec.key_url, f"{spec.name} does not say where to get a key"
            assert spec.default_base_url, f"{spec.name} ships no default URL"


def test_hosting_kinds_are_exclusive() -> None:
    """`local` and `byo_endpoint` answer different questions and cannot
    both be true: one says the endpoint is on this machine, the other
    that its address is unknowable to the plugin."""
    for spec in providers.PROVIDERS:
        assert not (spec.local and spec.byo_endpoint), (
            f"{spec.name} claims to be both local and bring-your-own"
        )


def test_transports_are_all_implementable() -> None:
    assert {s.transport for s in providers.PROVIDERS} == {
        "anthropic", "google", "openai_compat",
    }


def test_require_lists_the_valid_names_on_error() -> None:
    with pytest.raises(ValueError, match="anthropic"):
        providers.require("gpt4all")
