#!/usr/bin/env python3
"""Switch which LLM provider the screening pipelines call.

Usage:
    python3 set_llm_provider.py openai
    python3 set_llm_provider.py --list

Writes `[llm] provider` into `~/.config/academic-research/config.toml`
and reports the new state, including which credential is still missing.

It reports rather than prompts, deliberately. "Switch me to OpenAI" is a
request an assistant can carry out on its own: the provider name is not
a secret and the file it lands in is machine-local. Supplying the key is
not — an API key typed into a conversation is an API key in a
transcript, so a missing credential is reported here and collected by
`/setup`, whose prompts read the terminal directly.

Changing the provider does not re-pin the models. Follow with
`resolve_models.py` inside the project — it lists what the new provider
serves, and `--stage X --model Y` writes the choice — so
`screening_config.py` names models the new provider actually has.

Stdlib-only, like everything in `scripts/setup/`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SCRIPTS_ROOT = _HERE.parent
if str(_SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_ROOT))
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from check_llm_provider import status_lines  # noqa: E402
from core import config_writer, llm_provider, providers  # noqa: E402


def _print_choices() -> None:
    print("Available providers:")
    for spec in providers.PROVIDERS:
        if spec.local:
            need = f"no API key (local, {spec.default_base_url})"
        elif spec.byo_endpoint:
            # Both halves, because a key alone gets this provider
            # nowhere: the plugin ships no address for it. Neither has an
            # environment variable, so name the config section.
            need = (
                f"needs base_url and api_key in config.toml "
                f"[{providers.config_section(spec)}]"
            )
        else:
            need = f"needs {spec.api_key_env}"
        print(f"  {spec.name:<12} {spec.label} — {need}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "provider", nargs="?",
        help="Provider to switch to. Omit with --list to see the options.",
    )
    parser.add_argument(
        "--list", action="store_true", help="List the providers and exit.",
    )
    args = parser.parse_args(argv)

    if args.list or not args.provider:
        _print_choices()
        return 0 if args.list else 2

    try:
        spec = providers.require(args.provider)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    config_writer.set_value("llm", "provider", spec.name)

    for line in status_lines(spec, selected=True):
        print(line)

    ok, missing = llm_provider.credential_status(spec)
    print()
    if not ok:
        print(
            f"NEXT: {missing} is not set. Run /setup to add it — keys are "
            f"typed into the wizard's own terminal prompt, never into this "
            f"conversation.",
        )
    print(
        "NEXT: run resolve_models.py in the project directory to see what "
        "this provider\n      serves, then re-pin screening_config.py with "
        "--stage/--model. The old pins\n      name models this provider does "
        "not have.",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
