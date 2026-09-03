#!/usr/bin/env python3
"""Prove the pinned model answers, before a run that costs real money.

    python3 check_model_connection.py                 # every pinned stage
    python3 check_model_connection.py --model <id>     # one specific ID
    python3 check_model_connection.py --quiet          # exit code only

Runs one ~4-token request per distinct model and prints the verdict.
Exit codes are the contract for callers:

    0  every model answered
    1  a failure the user must resolve (dead key, spent quota, bad ID)
    2  the project is not configured far enough to check

Why this exists, in one paragraph: a live classification run over 244
items sat silent for ~22 minutes and was diagnosed as a network hang.
It was an exhausted Gemini quota, reported plainly in every response
body, but the per-item retry ladder spent 131 seconds per item before
surfacing anything. One second of checking up front would have replaced
the whole evening. So the systematic-review and setup skills call this
after any model is pinned and before any screening or coding run.

Stdlib-only — `scripts/setup/` runs under a bare `python3` with no venv
(CLAUDE.md). The probe deliberately speaks HTTP directly rather than
through the provider SDKs, which also means it works before the
pipeline dependencies are installed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SCRIPTS_ROOT = _HERE.parent
if str(_SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_ROOT))

from core import llm_provider, model_health, providers  # noqa: E402
from core.config_loader import get  # noqa: E402
from core.models import TIER_FOR_STAGE  # noqa: E402

#: `screening_config.py` constant per stage — the same mapping
#: `resolve_models.py` writes into. Kept here rather than imported from
#: it because this script must work with no project scaffold at all.
STAGE_CONSTANTS: dict[str, str] = {
    "abstract_screening": "ABSTRACT_SCREENING_MODEL",
    "fulltext_coding": "FULLTEXT_CODING_MODEL",
}


def _configured_provider() -> str:
    return get("llm", "provider", env=providers.PROVIDER_ENV) or (
        providers.DEFAULT_PROVIDER
    )


def _api_key(spec) -> str:
    # `local`, not `api_key_env`: a bring-your-own gateway declares no
    # variable but still keeps a key in `[gateway] api_key`.
    if spec.local:
        return ""
    return get(
        providers.config_section(spec), "api_key",
        env=llm_provider.credential_env(spec),
    )


def _base_url(spec) -> str:
    if not (spec.base_url_env or spec.byo_endpoint):
        return ""
    return get(
        providers.config_section(spec), "base_url",
        env=llm_provider.base_url_env(spec),
    ) or ""


def _pinned_models(config_path: Path) -> dict[str, str]:
    """Stage → pinned model ID, read out of a project `screening_config.py`.

    Parsed with `ast` rather than imported: the file lives in the user's
    project and importing it would execute whatever else it contains,
    and pull in dependencies this stdlib-only script does not have.
    """
    import ast

    if not config_path.is_file():
        return {}
    try:
        tree = ast.parse(config_path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return {}
    wanted = {const: stage for stage, const in STAGE_CONSTANTS.items()}
    found: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            name = getattr(target, "id", "")
            if name in wanted and isinstance(node.value, ast.Constant):
                value = str(node.value.value or "").strip()
                if value:
                    found[wanted[name]] = value
    return found


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check that the configured provider answers for the "
                    "pinned model(s).",
    )
    parser.add_argument(
        "--model",
        default="",
        help="Check this model ID instead of the project's pinned ones.",
    )
    parser.add_argument(
        "--stage",
        choices=sorted(TIER_FOR_STAGE),
        help="Check only this stage's pinned model.",
    )
    parser.add_argument(
        "--config",
        default="screening_config.py",
        help="Path to the project's screening_config.py "
             "(default: ./screening_config.py).",
    )
    parser.add_argument(
        "--provider",
        default="",
        help="Override the configured provider for this check.",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Print nothing; communicate via exit code only.",
    )
    args = parser.parse_args()

    try:
        spec = providers.require(args.provider or _configured_provider())
    except ValueError as e:
        if not args.quiet:
            print(f"ERROR: {e}", file=sys.stderr)
        return 2

    key = _api_key(spec)
    base = _base_url(spec)
    # Checked before the pinned-model test below, not after: the advice
    # that test gives is "run resolve_models.py --list", and that cannot
    # work without an endpoint to list from. Reporting the missing model
    # first would send the user down a path that dead-ends.
    if spec.byo_endpoint and not base:
        if not args.quiet:
            print(
                f"NOT CONFIGURED: {spec.label} needs its endpoint set "
                f"in {providers.base_url_location(spec, llm_provider.base_url_env(spec))}.\n"
                f"  The plugin ships no default address for it — only you "
                f"know your institution's.\n  Run `/setup` to add it.",
                file=sys.stderr,
            )
        return 2

    if args.model:
        targets = {"(explicit)": args.model}
    else:
        pinned = _pinned_models(Path(args.config))
        if args.stage:
            pinned = {k: v for k, v in pinned.items() if k == args.stage}
        if not pinned:
            if not args.quiet:
                print(
                    f"NOT CONFIGURED: no pinned model found in "
                    f"{args.config}.\n"
                    f"  Run `resolve_models.py --list` to see what "
                    f"{spec.label} serves, then pin one with\n"
                    f"  `resolve_models.py --stage <stage> --model <id>`.",
                    file=sys.stderr,
                )
            return 2
        targets = pinned

    if not spec.local and not key and not base:
        if not args.quiet:
            print(
                f"NOT CONFIGURED: {spec.label} needs a key in "
                f"{providers.credential_location(spec, llm_provider.credential_env(spec))}, "
                f"which is not set.\n  Run `/setup` to add it — keys are "
                f"entered there so they never enter the conversation.",
                file=sys.stderr,
            )
        return 2

    # One probe per distinct model: the two stages often share an ID, and
    # a second identical request tells us nothing new.
    checked: dict[str, model_health.ConnectionResult] = {}
    failed = False
    for stage, model in sorted(targets.items()):
        result = checked.get(model)
        if result is None:
            result = model_health.check_connection(
                spec, model, api_key=key, base_url=base,
            )
            checked[model] = result
        if not args.quiet:
            print(f"[{stage}] {result.format()}")
        if not result.ok:
            failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    # Windows takes stdout's encoding from the locale when output is
    # redirected — normally cp1252, which cannot encode the arrows, em
    # dashes and rules printed below. See scripts/core/console.py.
    from core.console import enable_utf8_output
    enable_utf8_output()
    raise SystemExit(main())
