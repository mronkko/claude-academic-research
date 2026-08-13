#!/usr/bin/env python3
"""Pin the models this project screens with, by asking the provider.

This is the bootstrap step that turns a tier ("fast", "balanced") into a
concrete model ID. It asks your configured provider which models it
currently serves, picks the newest one in each stage's tier, and writes
the answer into the project's `screening_config.py`.

Usage:
    python3 resolve_models.py                      # both stages, default tiers
    python3 resolve_models.py --dry-run            # show the choice, write nothing
    python3 resolve_models.py --stage fulltext_coding --tier deep
    python3 resolve_models.py --config path/to/screening_config.py

Why write into `screening_config.py` rather than re-copying the
template: that file also holds the review's prompts and coding scheme,
which the user wrote and a reviewer will read. Only the two
`*_MODEL = "…"` lines are rewritten, in place; everything else in the
file is untouched, byte for byte.

Each rewritten line carries its provenance —

    ABSTRACT_SCREENING_MODEL = "…"  # provider=… · tier=… · resolved …

— because the pin is a methods-section fact. A reader reconstructing the
review needs to know not just which model ran, but which tier it was
chosen to satisfy and when the choice was made.

`--stage X --tier Y` is also the permanent-change path: to code full
texts on the deep tier from now on, run it with `--tier deep` rather
than hand-editing the constant, so the provenance comment stays true.

Stdlib-only, like everything in `scripts/setup/` — this runs under a
bare `python3` with no venv (see CLAUDE.md).
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SCRIPTS_ROOT = _HERE.parent
if str(_SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_ROOT))

from core import model_discovery, providers  # noqa: E402
from core.config_loader import get  # noqa: E402
from core.models import TIER_FOR_STAGE  # noqa: E402
from core.providers import ProviderSpec  # noqa: E402

#: Which constant in `screening_config.py` each stage pins. Explicit
#: rather than derived from the stage name: the constants are part of a
#: file users have already copied into their projects, so they cannot be
#: renamed by a refactor here without breaking those projects.
CONSTANT_FOR_STAGE: dict[str, str] = {
    "abstract_screening": "ABSTRACT_SCREENING_MODEL",
    "fulltext_coding": "FULLTEXT_CODING_MODEL",
}

DEFAULT_CONFIG = Path("screening_config.py")


def _credentials(spec: ProviderSpec) -> tuple[str, str]:
    """`(api_key, base_url)` for `spec`, from env or config.toml.

    Neither is ever printed. The wizard writes them; this script reads
    them only to make the model-listing call.
    """
    section = providers.config_section(spec)
    api_key = get(section, "api_key", env=spec.api_key_env) if spec.api_key_env else ""
    base_url = (
        get(section, "base_url", env=spec.base_url_env) if spec.base_url_env else ""
    )
    return api_key, base_url


def pin_line(constant: str, model: str, provider: str, tier: str,
             today: str = "") -> str:
    """The replacement source line, with its provenance comment."""
    stamp = today or date.today().isoformat()
    return (
        f'{constant} = "{model}"'
        f"  # provider={provider} · tier={tier} · resolved {stamp}"
    )


def rewrite_pin(text: str, constant: str, line: str) -> tuple[str, int]:
    """Replace the `constant = …` assignment in `text`. Returns (text, n).

    n is 0 when the constant is absent — the caller reports that rather
    than appending, since a `screening_config.py` without it is more
    likely the wrong file than a file missing one line.

    The replacement goes through a lambda so a model ID containing a
    backslash sequence cannot be read as a regex group reference.

    The match deliberately ends at `[^\\r\\n]*` rather than `$`: Python's
    `$` treats only `\\n` as a line terminator, so on a CRLF file `.*$`
    swallows the `\\r` and the rewritten line silently loses it — two
    pinned lines would end up with different endings from the rest of a
    file the user has in git.
    """
    pattern = re.compile(rf"^{re.escape(constant)}[ \t]*=[^\r\n]*", re.MULTILINE)
    return pattern.subn(lambda _m: line, text, count=1)


def _read(path: Path) -> str:
    # newline="" keeps CRLF files CRLF: this rewrites two lines of a file
    # the user has in git, and flipping every line ending would bury them.
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def _write(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _resolve(
    spec: ProviderSpec, stages: list[str], tier_override: str, catalog: dict | None,
) -> dict[str, tuple[str, model_discovery.Resolution]]:
    """Resolve every stage, returning `{stage: (tier, Resolution)}`."""
    api_key, base_url = _credentials(spec)
    out: dict[str, tuple[str, model_discovery.Resolution]] = {}
    for stage in stages:
        tier = tier_override or TIER_FOR_STAGE.get(stage, providers.TIER_BALANCED)
        out[stage] = (tier, model_discovery.resolve_tier(
            spec, tier, api_key=api_key, base_url=base_url, catalog=catalog,
        ))
    return out


def _report(spec: ProviderSpec, resolved: dict) -> None:
    print(f"provider: {spec.name} ({spec.label})")
    stage_w = max((len(s) for s in resolved), default=0)
    model_w = max((len(r.model) or 12 for _t, r in resolved.values()), default=0)
    for stage, (tier, res) in resolved.items():
        model = res.model or "(none found)"
        print(f"  {stage:<{stage_w}}  tier={tier:<9} {model:<{model_w}}  [{res.source}]")


def _warn_stale(spec: ProviderSpec, resolved: dict) -> None:
    """Say out loud when a pin came from the shipped file, not the API.

    A catalogue fallback is a working answer, but it is an answer from a
    file that ages with the plugin release rather than with the
    provider. Silence here is how a project ends up pinned to a
    superseded model and nobody notices.
    """
    for stage, (_tier, res) in resolved.items():
        if not res.is_stale_risk:
            continue
        print(
            f"\nWARNING: {stage} fell back to the model catalogue shipped "
            f"with this plugin.\n"
            f"         Could not ask {spec.name}: {res.detail}.\n"
            f"         The pin below may name a model that has since been "
            f"superseded —\n"
            f"         fix the credential or endpoint and re-run to get a "
            f"current one.",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG,
        help=f"Project screening config to rewrite (default: {DEFAULT_CONFIG}).",
    )
    parser.add_argument(
        "--stage", choices=sorted(CONSTANT_FOR_STAGE),
        help="Pin only this stage. Default: every stage.",
    )
    parser.add_argument(
        "--tier", choices=list(providers.TIERS),
        help="Override the stage's default tier. Requires --stage, since a "
             "tier names one capability level and the stages want different "
             "ones. This is the supported way to make the change permanent.",
    )
    parser.add_argument(
        "--provider",
        help="Resolve against this provider instead of the configured one. "
             "Does not change the configuration — use set_llm_provider.py.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be pinned; write nothing.",
    )
    args = parser.parse_args(argv)

    if args.tier and not args.stage:
        parser.error("--tier applies to one stage; pass --stage as well.")

    provider_name = args.provider or get(
        "llm", "provider", env=providers.PROVIDER_ENV,
    ) or providers.DEFAULT_PROVIDER
    try:
        spec = providers.require(provider_name)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    stages = [args.stage] if args.stage else list(CONSTANT_FOR_STAGE)
    resolved = _resolve(spec, stages, args.tier or "", None)

    _report(spec, resolved)
    _warn_stale(spec, resolved)

    unresolved = [s for s, (_t, r) in resolved.items() if not r.model]
    if unresolved:
        print(
            f"\nERROR: no model found for: {', '.join(unresolved)}.\n"
            f"       {spec.name} could not be asked and the shipped "
            f"catalogue has no entry for that tier.\n"
            f"       Check the credential for {spec.api_key_env or spec.base_url_env}, "
            f"or pin a model by hand in {args.config}.",
            file=sys.stderr,
        )
        return 1

    if args.dry_run:
        print(f"\ndry run: {args.config} not modified.")
        return 0

    if not args.config.is_file():
        print(
            f"\nERROR: {args.config} not found. Run this from the project "
            f"directory, or pass --config.",
            file=sys.stderr,
        )
        return 1

    text = _read(args.config)
    written = []
    for stage, (tier, res) in resolved.items():
        constant = CONSTANT_FOR_STAGE[stage]
        text, n = rewrite_pin(
            text, constant, pin_line(constant, res.model, spec.name, tier),
        )
        if n:
            written.append(constant)
        else:
            print(
                f"WARNING: {constant} not found in {args.config}; left alone. "
                f"Is that a screening config?",
                file=sys.stderr,
            )
    if not written:
        return 1

    _write(args.config, text)
    print(f"\nwrote {len(written)} pin(s) to {args.config}: {', '.join(written)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
