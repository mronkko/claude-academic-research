"""Model defaults and short aliases for the screening pipelines.

Two jobs:

1. **One definition of each stage default.** `ABSTRACT_SCREENING_MODEL` and
   `FULLTEXT_CODING_MODEL` used to be written twice each — once as the
   `getattr` fallback in the orchestrator, once in
   `templates/screening_config.py`. The template cannot import this module
   (it is copied into user projects and must stand alone), so it keeps its
   literals and `tests/unit/test_model_defaults.py` asserts the two agree.

2. **Short aliases, so a model can be chosen in conversation.** A user
   saying "screen these with Haiku" should map to
   `abstract_screen.py --model haiku` rather than an edit to the project's
   `screening_config.py`. `resolve_model` handles the lookup.

`resolve_model` deliberately does *not* validate against a closed list.
Anything it does not recognise passes through untouched, which is what lets
explicit model IDs and locally-served model names (see `ANTHROPIC_BASE_URL`
in `llm_provider`) keep working.
"""

from __future__ import annotations

#: Stage-1 default: title + abstract screening, high volume, cheap model.
DEFAULT_ABSTRACT_SCREENING_MODEL = "claude-haiku-4-5-20251001"

#: Stage-2 default: full-text coding, low volume, stronger model.
DEFAULT_FULLTEXT_CODING_MODEL = "claude-sonnet-4-6"

#: Short names accepted by `--model`. Keep the keys lowercase.
ALIASES: dict[str, str] = {
    # Anthropic
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-5",
    # Google (Antigravity)
    "flash": "gemini-2.5-flash",
    "pro": "gemini-2.5-pro",
    "gemini": "gemini-2.5-pro",
}


def resolve_model(name: str) -> str:
    """Expand a short alias to a full model ID.

    Unknown names pass through unchanged — `--model claude-opus-4-1` and
    `--model qwen3-30b` (against a local Anthropic-compatible endpoint) are
    both legitimate. Empty input returns `""` so callers can use the usual
    `args.model or config_value` precedence chain.
    """
    if not name:
        return ""
    return ALIASES.get(name.strip().lower(), name.strip())


def model_flag_help(default_source: str) -> str:
    """`--model` help text, listing the aliases so `--help` is enough."""
    aliases = ", ".join(sorted(ALIASES))
    return (
        f"Override the model for this run. Accepts a full model ID or a "
        f"short alias ({aliases}). Default: {default_source}. This does NOT "
        f"edit screening_config.py — the effective model is recorded in the "
        f"`model` column of the CSV log, which is what the manuscript should "
        f"cite."
    )


def effective_model(cli_model: str, config_model: str, *, stage: str) -> str:
    """Resolve `--model` against the project config, loudly.

    Precedence: `--model` → `screening_config.py` → the stage default that
    `_load_screening_config` already applied.

    Prints a banner when the flag overrides a differing config value.
    Without it, `screening_config.py` would silently describe a run it did
    not configure — a reproducibility problem, since the config file is
    what a reader checks first.
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
