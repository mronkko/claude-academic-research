"""Stage → tier mapping, aliases, and the `--model` precedence chain.

This file used to assert that two literals in `templates/screening_config.py`
equalled two constants in `scripts/core/models.py` — a guard that kept a
duplicated model pin in sync. Both pins are gone: the template now ships
empty and `resolve_models.py` fills it at bootstrap from whatever the
user's provider currently serves. So the invariant flipped, from "these
two literals match" to "there are no literals".

What still needs pinning is the behaviour around that: which tier each
stage asks for, that conversational aliases resolve through the active
provider, that explicit model IDs pass through untouched, and that an
override announces itself.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from core import models, providers

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "templates" / "screening_config.py"


def _template_module():
    """Load the template by path, without writing a .pyc.

    `test_zotero_mcp_sync.py` scans `templates/**/*` as UTF-8, so a
    stray bytecode file there breaks an unrelated test.
    """
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location("screening_config", TEMPLATE)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.dont_write_bytecode = False


# ---------------------------------------------------------------------------
# The template ships no pin at all
# ---------------------------------------------------------------------------


def test_template_ships_empty_model_pins() -> None:
    """A shipped pin is a stale pin the day the provider releases.

    `resolve_models.py` writes these at bootstrap; until then the
    pipeline falls back to the catalogue and says so.
    """
    template = _template_module()
    assert template.ABSTRACT_SCREENING_MODEL == ""
    assert template.FULLTEXT_CODING_MODEL == ""


def test_template_explains_how_the_pin_gets_filled() -> None:
    """An empty constant with no explanation is just a broken config."""
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "resolve_models.py" in text
    assert "--model" in text


def test_orchestrators_do_not_hardcode_a_model_id() -> None:
    """Unchanged in spirit from the original guard, widened in scope.

    The old version matched only `claude-` and `gemini-` prefixes; a
    provider-agnostic plugin has to reject an OpenAI pin too.
    """
    import re

    pattern = re.compile(
        r"getattr\([^)]*MODEL[^)]*\"(claude|gemini|gpt|llama|mistral)-",
    )
    for name in ("abstract_screen.py", "fulltext_code.py"):
        text = (ROOT / "scripts" / "pipelines" / name).read_text(encoding="utf-8")
        assert not pattern.search(text), f"{name} hardcodes a model id"


# ---------------------------------------------------------------------------
# Stage → tier
# ---------------------------------------------------------------------------


def test_screening_is_cheap_and_coding_is_not() -> None:
    assert models.tier_for_stage("abstract_screening") == providers.TIER_FAST
    assert models.tier_for_stage("fulltext_coding") == providers.TIER_BALANCED


def test_coding_default_did_not_silently_become_opus() -> None:
    """The pre-tier defaults were Haiku + Sonnet.

    Mapping full-text coding to `deep` would resolve to Opus where one
    exists and roughly double the per-paper cost for every existing
    project. That is the user's decision, not a side effect of this
    refactor — `--model deep` remains available.
    """
    assert models.tier_for_stage("fulltext_coding") != providers.TIER_DEEP


def test_unknown_stage_gets_a_safe_middle_tier() -> None:
    assert models.tier_for_stage("some_future_stage") == providers.TIER_BALANCED


# ---------------------------------------------------------------------------
# Aliases resolve through the provider, not to a fixed ID
# ---------------------------------------------------------------------------


def test_every_alias_names_a_real_tier() -> None:
    for alias, tier in models.TIER_ALIASES.items():
        assert tier in providers.TIERS, f"{alias} maps to unknown tier {tier}"


def test_the_same_alias_means_different_models_per_provider() -> None:
    """"Screen these with the fast model" is provider-relative.

    This is the whole point of tiers: the user says what they want, not
    which vendor SKU delivers it.
    """
    anthropic = models.resolve_model("fast", provider="anthropic")
    google = models.resolve_model("fast", provider="google")
    assert anthropic.startswith("claude-haiku")
    assert google.startswith("gemini")
    assert anthropic != google


@pytest.mark.parametrize(
    ("alias", "tier"),
    [("haiku", "fast"), ("sonnet", "balanced"), ("opus", "deep"),
     ("cheap", "fast"), ("best", "deep")],
)
def test_conversational_words_map_to_tiers(alias, tier) -> None:
    assert models.TIER_ALIASES[alias] == tier


def test_resolve_model_is_case_and_space_insensitive() -> None:
    assert models.resolve_model("  Haiku ", provider="anthropic").startswith(
        "claude-haiku",
    )
    assert models.resolve_model("FAST", provider="anthropic").startswith(
        "claude-haiku",
    )


def test_resolve_model_passes_explicit_ids_through() -> None:
    """The contract that keeps every existing project working.

    A user who pinned `claude-opus-4-1-20250805`, or who runs a locally
    served `qwen3-30b`, must not have it rewritten.
    """
    for name in ("claude-opus-4-1-20250805", "qwen3-30b", "some/custom:model"):
        assert models.resolve_model(name) == name


def test_resolve_model_empty_returns_empty() -> None:
    assert models.resolve_model("") == ""


def test_an_alias_with_no_catalogue_entry_is_handed_back() -> None:
    """Better the provider rejects an alias than that we invent an ID."""
    assert models.resolve_model("deep", provider="openrouter") == "deep"


# ---------------------------------------------------------------------------
# Precedence chain — unchanged, and load-bearing for reproducibility
# ---------------------------------------------------------------------------


def test_effective_model_falls_back_to_config_when_flag_absent(capsys) -> None:
    assert models.effective_model("", "claude-sonnet-5", stage="X") == "claude-sonnet-5"
    assert capsys.readouterr().out == ""


def test_effective_model_flag_wins_and_announces(capsys) -> None:
    """Silence here would let screening_config.py describe a run it did
    not configure — the reviewer checks that file first."""
    out_model = models.effective_model(
        "haiku", "claude-sonnet-5", stage="FULLTEXT_CODING_MODEL",
    )
    printed = capsys.readouterr().out
    assert out_model != "claude-sonnet-5"
    assert "FULLTEXT_CODING_MODEL" in printed
    assert "claude-sonnet-5" in printed
    assert "screening_config.py is unchanged" in printed


def test_effective_model_silent_when_flag_matches_config(capsys) -> None:
    pin = models.resolve_model("fast", provider="anthropic")
    models.effective_model(pin, pin, stage="X")
    assert capsys.readouterr().out == ""


def test_model_flag_help_lists_every_alias() -> None:
    help_text = models.model_flag_help("the stage default")
    for alias in models.TIER_ALIASES:
        assert alias in help_text
    assert "the stage default" in help_text


# ---------------------------------------------------------------------------
# Dry-run cost estimate
# ---------------------------------------------------------------------------


def test_cost_estimate_scales_with_the_item_count() -> None:
    one = models.cost_estimate_line(
        "claude-haiku-4-5", stage="abstract_screening", n_items=1,
        provider="anthropic",
    )
    many = models.cost_estimate_line(
        "claude-haiku-4-5", stage="abstract_screening", n_items=1000,
        provider="anthropic",
    )
    assert "~$" in one and "~$" in many
    assert one != many
    assert "1,000 item(s)" in many


def test_cost_estimate_prices_the_model_not_the_stage_default() -> None:
    """`--model deep` on the screening stage must be priced as deep.

    Quoting the stage's default tier would understate a run the user is
    about to pay for.
    """
    cheap = models.cost_estimate_line(
        "claude-haiku-4-5", stage="abstract_screening", n_items=500,
        provider="anthropic",
    )
    dear = models.cost_estimate_line(
        "claude-opus-4-1", stage="abstract_screening", n_items=500,
        provider="anthropic",
    )
    assert "fast tier" in cheap
    assert "deep tier" in dear
    assert _usd(dear) > _usd(cheap)


def test_fulltext_coding_costs_more_per_item_than_screening() -> None:
    """Same model, more tokens: a 40-page paper is not an abstract."""
    screening = models.cost_estimate_line(
        "claude-sonnet-5", stage="abstract_screening", n_items=100,
        provider="anthropic",
    )
    coding = models.cost_estimate_line(
        "claude-sonnet-5", stage="fulltext_coding", n_items=100,
        provider="anthropic",
    )
    assert _usd(coding) > _usd(screening)


def _usd(line: str) -> float:
    return float(line.split("~$")[1].split(" ")[0].replace(",", ""))


def test_cost_estimate_says_unknown_rather_than_zero() -> None:
    """"Free" is the one wrong answer a dry run must never give."""
    line = models.cost_estimate_line(
        "some-model-nobody-priced", stage="abstract_screening", n_items=10,
        provider="openrouter",
    )
    assert "unknown" in line
    assert "$0" not in line


def test_cost_estimate_is_explicit_that_local_is_free() -> None:
    line = models.cost_estimate_line(
        "llama3:8b", stage="abstract_screening", n_items=10_000,
        provider="ollama",
    )
    assert "none" in line
    assert "own machine" in line


def test_cost_estimate_handles_an_unrecognised_provider() -> None:
    line = models.cost_estimate_line(
        "x", stage="abstract_screening", n_items=1, provider="bogus",
    )
    assert "unknown" in line
