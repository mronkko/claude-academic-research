#!/usr/bin/env python3
"""Report which models the `demo-slr` skill will screen and code with.

Usage:
    python3 check_demo_models.py

Prints a short status block and exits 0:

    provider: anthropic
    abstract screening: claude-haiku-4-5 (tier fast, --model fast)
    full-text coding: claude-sonnet-4-5 (tier balanced, --model balanced)

The demo runs abstract screening with `--model fast` and full-text coding
with `--model balanced`, and ships `screening_config.py` with no model
pins, so what runs is whatever those two tiers resolve to for the
configured provider. This resolves them exactly as the screening scripts
do (`core.models.resolve_model`, from the shipped catalogue, no network),
so the line shown to the presenter is the line the run will use. A
provider with no catalogue entry (OpenRouter, local endpoints) leaves the
alias unresolved; that is said plainly rather than guessed at.

Like `check_llm_provider.py`, this exists so a skill never reads
`~/.config/academic-research/config.toml`: it prints the provider and the
model IDs, never a key.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_ROOT = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_ROOT))

from core import models, providers  # noqa: E402

#: `(label, stage, --model alias)` for each LLM step the demo runs.
DEMO_STEPS = (
    ("abstract screening", "abstract_screening", "fast"),
    ("full-text coding", "fulltext_coding", "balanced"),
)


def status_lines(provider: str) -> list[str]:
    lines = [f"provider: {provider}"]
    for label, stage, alias in DEMO_STEPS:
        tier = models.tier_for_stage(stage)
        model = models.resolve_model(alias, provider=provider)
        if model and model != alias:
            lines.append(f"{label}: {model} (tier {tier}, --model {alias})")
        else:
            lines.append(
                f"{label}: not resolved — no catalogue model for tier "
                f"{tier} on {provider}; --model {alias} is passed to the "
                f"provider as written and may be rejected. Pin a model "
                f"with resolve_models.py first."
            )
    return lines


def main() -> int:
    provider = models.active_provider()
    if providers.get(provider) is None:
        provider = providers.DEFAULT_PROVIDER
    for line in status_lines(provider):
        print(line)
    return 0


if __name__ == "__main__":
    from core.console import enable_utf8_output
    enable_utf8_output()
    raise SystemExit(main())
