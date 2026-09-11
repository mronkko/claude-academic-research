"""Set and validate a review's Zotero tag prefix in `screening_config.py`.

Every SLR namespaces the Zotero tags it writes — `agentic-ai/abstract:include`
rather than a bare `abstract:include` — so that one review's tags can be
isolated in Zotero's tag selector and two reviews sharing a library never
write the same tag. `TAG_PREFIX` in the project's `screening_config.py` is
where that namespace is declared.

This script exists rather than a hand-edit for two reasons. It validates
before the value can reach Zotero (a prefix containing `/` or `:` would make
tag families ambiguous, and a bad prefix is only discovered after it has been
written onto hundreds of items), and it prints the exact tags the review will
produce, which is what the `systematic-review` skill shows the user when
asking them to confirm.

Usage:
    python3 set_tag_prefix.py --prefix agentic-ai
    python3 set_tag_prefix.py --prefix agentic-ai --dry-run
    python3 set_tag_prefix.py --show              # what is set right now

Stdlib-only, like everything in `scripts/setup/`: it runs before any
environment exists, and the wizard's `Bash(python3 ${CLAUDE_PLUGIN_ROOT}/
scripts/**)` allow rule is what keeps it from prompting.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

#: Mirrors `scripts/pipelines/tag_prefix.py`. Duplicated deliberately: this
#: script must stay stdlib-only and importable with nothing on `sys.path`,
#: and `scripts/setup/` does not import from `scripts/pipelines/`.
#: `tests/unit/test_tag_prefix.py` asserts the two stay equal.
MAX_PREFIX_LEN = 32
PREFIX_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
CONSTANT = "TAG_PREFIX"
DEFAULT_CONFIG = Path("screening_config.py")

RULES = (
    f"A tag prefix must be 1-{MAX_PREFIX_LEN} characters of lowercase letters, "
    "digits and interior hyphens (e.g. `agentic-ai`). No spaces, no `/`, no "
    "`:`, and it may not start or end with a hyphen."
)

#: What the review will write, as families. Rendered for the user so the
#: choice is made against the real thing rather than an abstraction.
SAMPLE_FAMILIES: tuple[tuple[str, str], ...] = (
    ("{ns}abstract:include", "passed title/abstract screening"),
    ("{ns}abstract:exclude", "excluded at title/abstract"),
    ("{ns}abstract:borderline", "kept for full text"),
    ("{ns}fulltext:include", "passed full-text screening"),
    ("{ns}fulltext:exclude", "excluded at full text"),
    ("{ns}qa-adjudicated-include", "you overrode the screener"),
    ("{ns}search:<query-label>", "which query found the item"),
)

#: Tags that stay global on purpose — facts about the paper, not this
#: review's opinion of it, so two reviews share rather than duplicate them.
GLOBAL_TAGS: tuple[str, ...] = (
    "predatory:flag",
    "retracted:flag",
    "pdf:tdm-recovered",
    "pdf:preprint-version",
    "pdf:repository-copy",
)


def validate(raw: str) -> str:
    """Return the canonical prefix, or exit with the rules."""
    prefix = (raw or "").strip()
    if not prefix:
        sys.exit(f"ERROR: tag prefix is empty. {RULES}")
    if len(prefix) > MAX_PREFIX_LEN:
        sys.exit(f"ERROR: tag prefix is {len(prefix)} characters. {RULES}")
    if not PREFIX_RE.match(prefix):
        sys.exit(f"ERROR: `{raw}` is not a well-formed tag prefix. {RULES}")
    return prefix


def rewrite(text: str, prefix: str) -> tuple[str, int]:
    """Replace the `TAG_PREFIX = …` assignment. Returns (text, n).

    n is 0 when the constant is absent, which the caller reports rather than
    appending — a `screening_config.py` without it is more likely the wrong
    file than a file missing one line. Same reasoning, and the same
    CRLF-preserving `[^\\r\\n]*` match, as `resolve_models.rewrite_pin`.
    """
    line = f'{CONSTANT} = "{prefix}"'
    pattern = re.compile(rf"^{re.escape(CONSTANT)}[ \t]*=[^\r\n]*", re.MULTILINE)
    return pattern.subn(lambda _m: line, text, count=1)


def _read(path: Path) -> str:
    # newline="" keeps CRLF files CRLF: this rewrites one line of a file the
    # user has in git, and flipping every line ending would bury the change.
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def _write(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def current(text: str) -> str:
    """The prefix currently declared, or "" if unset or absent."""
    match = re.search(
        rf"^{re.escape(CONSTANT)}[ \t]*=[ \t]*[\"']([^\"']*)[\"']",
        text,
        re.MULTILINE,
    )
    return match.group(1) if match else ""


def preview(prefix: str) -> str:
    """The tag list to show the user before they confirm."""
    ns = f"{prefix}/"
    width = max(len(t.format(ns=ns)) for t, _ in SAMPLE_FAMILIES)
    lines = [
        f"Tags this review will write (filter the Zotero tag selector on "
        f"`{ns}` to see exactly these):",
        "",
    ]
    lines += [
        f"  {tag.format(ns=ns):<{width}}  {meaning}"
        for tag, meaning in SAMPLE_FAMILIES
    ]
    lines += [
        "",
        "Coded categorical variables can add families of their own, e.g.",
        f"  {ns}research-design:panel",
        "",
        "Left global on purpose — facts about the paper, not this review's",
        "opinion of it, so reviews sharing a library share them:",
        "",
    ]
    lines += [f"  {tag}" for tag in GLOBAL_TAGS]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Set the Zotero tag prefix for this review.",
    )
    parser.add_argument("--prefix", default="", help=RULES)
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"Project screening config (default: {DEFAULT_CONFIG}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print the resulting tags without writing.",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Print the prefix currently declared, then exit.",
    )
    args = parser.parse_args()

    if not args.config.is_file():
        sys.exit(
            f"ERROR: {args.config} not found. Run this from the project root, "
            f"or pass --config."
        )
    text = _read(args.config)

    if args.show:
        existing = current(text)
        print(f"{CONSTANT} = {existing!r}" if existing else f"{CONSTANT} is not set")
        return 0 if existing else 1

    if not args.prefix:
        sys.exit("ERROR: --prefix is required (or pass --show).")
    prefix = validate(args.prefix)

    existing = current(text)
    if existing and existing != prefix:
        print(
            f"WARNING: {args.config} already declares {CONSTANT} = "
            f"{existing!r}.\n"
            f"         Changing it orphans every tag already written under "
            f"`{existing}/` —\n"
            f"         already-screened items will look unscreened to the "
            f"pipeline.",
            file=sys.stderr,
        )

    print(preview(prefix))
    print()

    if args.dry_run:
        print(f"--dry-run: {args.config} not modified.")
        return 0

    updated, count = rewrite(text, prefix)
    if count == 0:
        sys.exit(
            f"ERROR: {args.config} has no `{CONSTANT} = …` line to rewrite.\n"
            f"       Is this the right file? A current template declares it "
            f"near the top."
        )
    _write(args.config, updated)
    print(f"{args.config}: {CONSTANT} = \"{prefix}\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
