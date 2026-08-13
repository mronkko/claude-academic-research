"""Which model each screening stage runs on.

**No model version appears in this file.** It used to hold five of them,
and that was the defect: every provider release made an installed plugin
ship a stale default, and a user who preferred a different provider had
no supported path at all. What lives here now is the mapping from a
*stage* to a *tier* — stable facts about the work, not about any
vendor's current catalogue.

Three jobs:

1. **Stage → tier.** Abstract screening is high-volume and shallow, so
   it wants the cheap tier; full-text coding is low-volume and demands
   structured extraction from 40 pages, so it wants a stronger one.
   That relationship does not change when models do.

2. **Short aliases, so a model can be chosen in conversation.** "Screen
   these with Haiku" should become `--model haiku`, not an edit to the
   user's `screening_config.py`. Aliases resolve through the *active
   provider*, so `--model fast` means Haiku for an Anthropic user and
   Flash for a Gemini user.

3. **A loud precedence chain.** `--model` beats the project config
   beats the stage default, and an override announces itself, because
   `screening_config.py` is what a reviewer reads to reconstruct the
   review and it must not silently describe a run it did not configure.

Concrete model IDs come from one of two places, never from here:
`resolve_models.py` writes a discovered pin into the project's
`screening_config.py` at bootstrap, and `templates/model_catalog.toml`
is the data-only fallback when the provider cannot be reached.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_ROOT = Path(__file__).resolve().parent.parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from core import providers  # noqa: E402
from core.providers import TIER_BALANCED, TIER_DEEP, TIER_FAST  # noqa: E402

#: Which tier each screening stage wants.
#:
#: Full-text coding maps to `balanced`, not `deep`, deliberately. The
#: pre-tier defaults were Haiku + Sonnet, and `deep` resolves to Opus
#: where one exists — roughly double the cost per paper. Moving the
#: default is a decision for the user, not a side effect of this
#: refactor (BACKLOG.md flagged exactly this). `--model deep` and a
#: `screening_config.py` edit both remain available.
TIER_FOR_STAGE: dict[str, str] = {
    "abstract_screening": TIER_FAST,
    "fulltext_coding": TIER_BALANCED,
}

#: Short names accepted by `--model`, resolved against the active
#: provider. Tier names themselves are aliases too, so `--model fast`
#: works whoever you are pointed at.
TIER_ALIASES: dict[str, str] = {
    TIER_FAST: TIER_FAST,
    TIER_BALANCED: TIER_BALANCED,
    TIER_DEEP: TIER_DEEP,
    # Provider-flavoured words users actually say. These name a tier,
    # not a model: "sonnet" from a Gemini user means "the balanced one".
    "cheap": TIER_FAST,
    "haiku": TIER_FAST,
    "flash": TIER_FAST,
    "mini": TIER_FAST,
    "nano": TIER_FAST,
    "small": TIER_FAST,
    "sonnet": TIER_BALANCED,
    "medium": TIER_BALANCED,
    "opus": TIER_DEEP,
    "pro": TIER_DEEP,
    "gemini": TIER_DEEP,
    "best": TIER_DEEP,
    "large": TIER_DEEP,
}


def active_provider() -> str:
    """The provider this machine is configured for.

    Reads `[llm] provider`, falling back to the registry default. Kept
    here so callers need not each reimplement the lookup.
    """
    from core.config_loader import get

    return get("llm", "provider", env=providers.PROVIDER_ENV) or (
        providers.DEFAULT_PROVIDER
    )


def tier_for_stage(stage: str) -> str:
    """The tier `stage` runs on. Unknown stages get the balanced tier."""
    return TIER_FOR_STAGE.get(stage, TIER_BALANCED)


def resolve_model(name: str, *, provider: str = "") -> str:
    """Expand a short alias to a concrete model ID for the active provider.

    Unknown names pass through unchanged — `--model claude-opus-4-1` and
    `--model qwen3-30b` (against a local endpoint) are both legitimate,
    and this is what keeps every explicit ID working. Empty input
    returns `""` so callers can use the usual
    `args.model or config_value` precedence chain.

    An alias that names a tier is resolved from the shipped catalogue
    rather than by calling the provider: this runs per-invocation on the
    screening hot path, and a network round-trip there would be both
    slow and a new failure mode. Use `resolve_models.py` when you want
    a freshly discovered pin.
    """
    if not name:
        return ""
    cleaned = name.strip()
    tier = TIER_ALIASES.get(cleaned.lower())
    if tier is None:
        return cleaned
    from core import model_discovery

    pin = model_discovery.catalog_model(provider or active_provider(), tier)
    # No catalogue entry (OpenRouter, local providers) — hand the alias
    # back so the provider can reject it with its own message rather
    # than this layer inventing an ID.
    return pin or cleaned


def model_flag_help(default_source: str) -> str:
    """`--model` help text, listing the aliases so `--help` is enough."""
    aliases = ", ".join(sorted(set(TIER_ALIASES)))
    return (
        f"Override the model for this run. Accepts a full model ID or a "
        f"short alias ({aliases}) — aliases resolve against your "
        f"configured provider. Default: {default_source}. This does NOT "
        f"edit screening_config.py — the effective model is recorded in "
        f"the `model` column of the CSV log, which is what the "
        f"manuscript should cite."
    )


def effective_model(cli_model: str, config_model: str, *, stage: str) -> str:
    """Resolve `--model` against the project config, loudly.

    Precedence: `--model` → `screening_config.py` → the stage default
    that `_load_screening_config` already applied.

    Prints a banner when the flag overrides a differing config value.
    Without it, `screening_config.py` would silently describe a run it
    did not configure — a reproducibility problem, since the config file
    is what a reader checks first.
    """
    if not cli_model:
        return config_model
    resolved = resolve_model(cli_model)
    if resolved != config_model:
        print(
            f"NOTE: --model overrides {stage} for this run: "
            f"{config_model} -> {resolved} "
            f"(screening_config.py is unchanged; the CSV log records "
            f"{resolved}).",
            flush=True,
        )
    return resolved


def default_for_stage(stage: str, *, provider: str = "") -> str:
    """Fallback model for `stage` when the project pins none.

    Only reached by projects bootstrapped before pins were written, or
    where the user emptied the constant. Comes from the shipped
    catalogue, so it is data rather than a code constant — but it is
    still a *fallback*, and the right fix is to run `resolve_models.py`
    and get a discovered pin.
    """
    from core import model_discovery

    return model_discovery.catalog_model(
        provider or active_provider(), tier_for_stage(stage),
    )
