#!/usr/bin/env python3
"""Show what your provider serves, and pin the model you choose.

Two jobs, deliberately separated:

    python3 resolve_models.py                          # what can I use?
    python3 resolve_models.py --stage abstract_screening --model <id>

The first asks the configured provider for its model listing and prints
it. It picks nothing and writes nothing. The second writes one chosen ID
into the project's `screening_config.py`.

**Why there is no automatic pick.** There used to be one: tier hints
plus a version-number sort. It chose `anthropic/claude-haiku-4.5:batch`
on OpenRouter — the *asynchronous Batch API*, useless for a synchronous
screening run — because the suffix won a string tiebreak. On Google it
chose `deep-research-pro-preview-12-2025` for the deep tier, because
`12-2025` parses as version 12.2025 and outranks every real Gemini. Both
mistakes are obvious to a reader who knows what a Batch API is, and this
script only ever runs from a SKILL.md, with an agent reading its output
and a user available to confirm. Suppressing those two would have meant
a blocklist of provider-specific substrings — `:batch`, `-image`, `-tts`,
`customtools` — that goes stale exactly as fast as the pinned model IDs
this whole design exists to eliminate.

So: the script reports, the agent proposes, the user confirms.
`templates/model_catalog.toml` covers the one case with nobody in the
loop — a provider that cannot be reached.

Why write into `screening_config.py` rather than re-copying the
template: that file also holds the review's prompts and coding scheme,
which the user wrote and a reviewer will read. Only the one
`*_MODEL = "…"` line is rewritten, in place; everything else in the
file is untouched, byte for byte.

Each rewritten line carries its provenance —

    ABSTRACT_SCREENING_MODEL = "…"  # provider=… · tier=… · pinned …

— because the pin is a methods-section fact. A reader reconstructing the
review needs to know not just which model ran, but roughly what class of
model it was and when the choice was made. The tier label is inferred
from the ID and can be overridden with `--tier`.

Stdlib-only, like everything in `scripts/setup/` — this runs under a
bare `python3` with no venv (see CLAUDE.md).
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SCRIPTS_ROOT = _HERE.parent
if str(_SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_ROOT))

from core import (  # noqa: E402
    llm_provider,
    model_discovery,
    model_health,
    providers,
)
from core.config_loader import get  # noqa: E402
from core.models import TIER_FOR_STAGE  # noqa: E402
from core.providers import ProviderSpec  # noqa: E402


def _api_key(spec: ProviderSpec) -> str:
    # `local`, not `api_key_env`: a bring-your-own gateway declares no
    # variable but still keeps a key in `[gateway] api_key`.
    if spec.local:
        return ""
    return get(
        providers.config_section(spec), "api_key",
        env=llm_provider.credential_env(spec),
    )


def _base_url(spec: ProviderSpec) -> str:
    if not (spec.base_url_env or spec.byo_endpoint):
        return ""
    return get(
        providers.config_section(spec), "base_url",
        env=llm_provider.base_url_env(spec),
    ) or ""

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
    return _api_key(spec), _base_url(spec)


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def _released(created: int) -> str:
    """`created` as a date, or blank. Providers report it inconsistently."""
    if not created:
        return ""
    try:
        return datetime.fromtimestamp(created, tz=UTC).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def listing_lines(spec: ProviderSpec, models: list) -> list[str]:
    """The model menu, one row per model, sorted by ID.

    Sorted rather than ranked. Alphabetical order groups a vendor's
    models together — which is what makes a 400-row OpenRouter listing
    skimmable — without implying that anything at the top is preferred.

    The `tier?` column is `providers.tier_of`, a guess from the ID's
    wording, and is labelled as a guess. It narrows a long listing; it
    does not decide anything.
    """
    rows = [
        (providers.tier_label(spec, m.id), m.id, _released(m.created))
        for m in sorted(models, key=lambda m: m.id)
    ]
    tier_w = max([len(t) for t, _i, _r in rows] + [len("tier?")])
    id_w = max([len(i) for _t, i, _r in rows] + [len("model")])
    out = [f"  {'tier?':<{tier_w}}  {'model':<{id_w}}  released"]
    out += [f"  {t:<{tier_w}}  {i:<{id_w}}  {r}".rstrip() for t, i, r in rows]
    return out


def _print_listing(spec: ProviderSpec, models: list) -> None:
    print(f"provider: {spec.name} ({spec.label})")
    print(f"{len(models)} model(s) served. Nothing has been written.\n")
    for line in listing_lines(spec, models):
        print(line)
    print(
        "\n`tier?` is a guess from the model's name, not a recommendation — "
        "check it.\nVariants that are not ordinary synchronous chat models "
        "(`:batch` queues,\n`-image` / `-tts` / `deep-research` endpoints) "
        "appear here too and are rarely\nwhat a screening run wants.\n"
        "\nPin a choice with:\n"
        f"  {Path(__file__).name} --stage abstract_screening --model <id>\n"
        f"  {Path(__file__).name} --stage fulltext_coding    --model <id>",
    )


def _print_catalog_fallback(spec: ProviderSpec, reason: str) -> int:
    """Offer the shipped catalogue when the provider cannot be asked.

    A catalogue answer is a working answer, but it comes from a file
    that ages with the plugin release rather than with the provider.
    Silence here is how a project ends up pinned to a superseded model
    and nobody notices.
    """
    print(
        f"WARNING: could not ask {spec.name} for its model listing: {reason}.\n"
        f"         Check the credential in "
        f"{providers.credential_location(spec, llm_provider.credential_env(spec))}, "
        f"or pin a model by hand.",
        file=sys.stderr,
    )
    suggestions = model_discovery.catalog_suggestions(spec.name)
    if not suggestions:
        print(
            f"ERROR: the shipped catalogue has no entry for {spec.name} "
            f"either, so\n       there is nothing to suggest. Fix the "
            f"credential and re-run, or pass\n       --model with an ID "
            f"you know the provider serves.",
            file=sys.stderr,
        )
        return 1
    print(f"provider: {spec.name} ({spec.label})")
    print(
        "Falling back to the catalogue shipped with this plugin. These may "
        "name\nmodels that have since been superseded — say so to the user "
        "before pinning:\n",
    )
    for tier, model in suggestions:
        print(f"  {tier:<9} {model}")
    return 0


# ---------------------------------------------------------------------------
# Pinning
# ---------------------------------------------------------------------------


def pin_line(constant: str, model: str, provider: str, tier: str,
             today: str = "") -> str:
    """The replacement source line, with its provenance comment."""
    stamp = today or date.today().isoformat()
    return (
        f'{constant} = "{model}"'
        f"  # provider={provider} · tier={tier} · pinned {stamp}"
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


def tier_for_pin(spec: ProviderSpec, model: str, stage: str, override: str) -> str:
    """The tier label for the provenance comment.

    Classified from the model actually chosen, not from the stage's
    default tier: a user who deliberately pins a strong model to the
    screening stage must not get a comment claiming it is the fast one.
    Falls back to the stage default only when the ID says nothing —
    common on local providers, where names carry no tier vocabulary.
    """
    return override or providers.tier_of(spec, model) or TIER_FOR_STAGE.get(
        stage, providers.TIER_BALANCED,
    )


def _warn_if_unlisted(spec: ProviderSpec, model: str) -> None:
    """Flag a model the provider does not list — usually a typo.

    A warning rather than a refusal: LM Studio omits models it has not
    loaded, and a user may legitimately name something the listing
    endpoint does not return. Being unreachable is not a reason to
    block a pin either, so a failed lookup passes quietly.
    """
    api_key, base_url = _credentials(spec)
    try:
        served = {m.id for m in model_discovery.list_models(
            spec, api_key=api_key, base_url=base_url,
        )}
    except model_discovery.DiscoveryError:
        return
    if model not in served:
        print(
            f"WARNING: {spec.name} does not list {model!r}. Pinning it "
            f"anyway — check\n         for a typo, or run without --model "
            f"to see what is served.",
            file=sys.stderr,
        )


def _read(path: Path) -> str:
    # newline="" keeps CRLF files CRLF: this rewrites a line of a file
    # the user has in git, and flipping every line ending would bury it.
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def _write(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG,
        help=f"Project screening config to rewrite (default: {DEFAULT_CONFIG}).",
    )
    parser.add_argument(
        "--stage", choices=sorted(CONSTANT_FOR_STAGE),
        help="Which stage's pin to write. Required with --model.",
    )
    parser.add_argument(
        "--model",
        help="The model ID to pin. Without it, the script lists what the "
             "provider serves and writes nothing.",
    )
    parser.add_argument(
        "--tier", choices=list(providers.TIERS),
        help="Tier label for the provenance comment. Defaults to the tier "
             "the model ID implies. Only affects the comment, never which "
             "model is written.",
    )
    parser.add_argument(
        "--provider",
        help="Use this provider instead of the configured one. Does not "
             "change the configuration — use set_llm_provider.py.",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="List the served models and exit. This is also the default "
             "when --model is not given.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the line that would be written; write nothing.",
    )
    parser.add_argument(
        "--no-check", action="store_true",
        help="Do not probe the model after pinning it. The probe costs "
             "~4 tokens and catches a dead key, a spent quota, or a "
             "mistyped ID at the moment of pinning rather than mid-run.",
    )
    args = parser.parse_args(argv)

    if args.model and not args.stage:
        parser.error("--model pins one stage; pass --stage as well.")
    if args.tier and not args.model:
        parser.error("--tier labels a pin; it does nothing without --model.")

    provider_name = args.provider or get(
        "llm", "provider", env=providers.PROVIDER_ENV,
    ) or providers.DEFAULT_PROVIDER
    try:
        spec = providers.require(provider_name)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    if args.list or not args.model:
        api_key, base_url = _credentials(spec)
        try:
            models = model_discovery.list_models(
                spec, api_key=api_key, base_url=base_url,
            )
        except model_discovery.DiscoveryError as e:
            return _print_catalog_fallback(spec, str(e))
        _print_listing(spec, models)
        return 0

    constant = CONSTANT_FOR_STAGE[args.stage]
    tier = tier_for_pin(spec, args.model, args.stage, args.tier or "")
    line = pin_line(constant, args.model, spec.name, tier)

    if args.dry_run:
        print(f"would write to {args.config}:\n  {line}")
        return 0

    _warn_if_unlisted(spec, args.model)

    if not args.config.is_file():
        print(
            f"ERROR: {args.config} not found. Run this from the project "
            f"directory, or pass --config.",
            file=sys.stderr,
        )
        return 1

    text, n = rewrite_pin(_read(args.config), constant, line)
    if not n:
        print(
            f"ERROR: {constant} not found in {args.config}; nothing written. "
            f"Is that a screening config?",
            file=sys.stderr,
        )
        return 1

    _write(args.config, text)
    print(f"pinned {constant} in {args.config}:\n  {line}", flush=True)

    if args.no_check:
        return 0

    # Prove the pin before anyone runs on it. Pinning is the moment the
    # model ID, the provider, and the credential first have to agree, and
    # a mismatch here is silent until a batch run discovers it item by
    # item — which is how a spent quota once read as a 22-minute network
    # hang. One ~4-token request settles it now.
    print("", flush=True)
    result = model_health.check_connection(
        spec,
        args.model,
        api_key=_api_key(spec),
        base_url=_base_url(spec),
    )
    print(result.format(), flush=True)
    if not result.ok:
        print(
            "\nThe pin was written, but the model did not answer. Fix the "
            "above before running a screening or coding stage.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
