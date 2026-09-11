#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyzotero>=1.15.1",
#     "tenacity>=8.0",
#     "httpx>=0.25",
# ]
# ///
"""Inspect and prune the Zotero tags one review has written.

Every tag this review writes is namespaced under `TAG_PREFIX` from
`screening_config.py` — `agentic-ai/abstract:include`,
`agentic-ai/research-design:panel`. That namespace is what makes this
script safe: it can enumerate and delete exactly this review's tags without
touching another review's, or the user's own.

Two jobs:

  --list   Every tag in this review's namespace, grouped by family, with
           the number of items carrying each. This is the input to the
           prune decision — a coding field that looked categorical at
           design time turns out to have forty singleton values, and the
           listing is where that becomes visible.

  --prune  Remove a whole family. Named by the coding field
           (`--prune research_design`) or by the family as it appears in
           the tag (`--prune research-design`); both resolve to the same
           `<prefix>/research-design:` sweep.

Dry-run by default. Nothing is written until `--apply`, and no Zotero item
is ever deleted — only tags are removed.

Usage:
    uv run manage_tags.py --group 6015547 --collection ABCDE1234 --list
    uv run manage_tags.py --group 6015547 --collection ABCDE1234 \\
        --prune research_design
    uv run manage_tags.py --group 6015547 --collection ABCDE1234 \\
        --prune research_design --apply

Omit `--collection` to work across the whole library. Stage families
(`abstract:`, `fulltext:`) are refused unless `--force` is passed: pruning
those throws away screening decisions, which is a different act from
tidying up a coding dimension.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = SCRIPT_DIR.parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import screening_common  # noqa: E402
import tag_prefix  # noqa: E402
import zotero_io  # noqa: E402

#: Families that record screening decisions rather than coded attributes.
#: Pruning one discards the review's own verdicts — recoverable only by
#: re-screening — so it takes `--force` on top of `--apply`.
DECISION_FAMILIES = ("abstract", "fulltext", "qa")


def tags_in_namespace(items: list[dict], ns: str) -> dict[str, int]:
    """`{tag: item count}` for every tag under this review's namespace."""
    counts: dict[str, int] = defaultdict(int)
    for item in items:
        for t in item.get("data", {}).get("tags", []):
            tag = t.get("tag", "")
            if tag.startswith(ns):
                counts[tag] += 1
    return dict(counts)


def group_by_family(counts: dict[str, int], ns: str) -> dict[str, list[tuple[str, int]]]:
    """Bucket `{tag: n}` by family, preserving a stable sort inside each.

    The family is everything up to the first `:` after the namespace.
    A namespaced tag with no `:` at all (the `qa-*` family spells itself
    with a hyphen) is bucketed under its whole remainder.
    """
    families: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for tag, n in counts.items():
        rest = tag[len(ns):]
        family = rest.split(":", 1)[0] if ":" in rest else rest
        families[family].append((tag, n))
    return {f: sorted(v) for f, v in sorted(families.items())}


def is_decision_family(family: str) -> bool:
    """Whether pruning this family would discard screening verdicts."""
    return any(
        family == d or family.startswith(f"{d}-") for d in DECISION_FAMILIES
    )


def resolve_family(raw: str, ns: str) -> str:
    """`research_design` or `research-design` -> `<ns>research-design:`.

    Accepts the coding field's own `name` because that is what the user
    reads in `screening_config.py`; accepts the slugged form because that
    is what they read in Zotero.
    """
    return tag_prefix.coding_family(ns, raw)


def print_listing(counts: dict[str, int], ns: str) -> None:
    if not counts:
        print(f"No tags found under `{ns}`.")
        print(
            "  Either this review has written none yet, or the prefix does "
            "not match\n  what was written. Check TAG_PREFIX in "
            "screening_config.py.",
        )
        return
    families = group_by_family(counts, ns)
    width = max(len(t) for t in counts)
    total_items = sum(counts.values())
    print(f"{len(counts)} tag(s) under `{ns}`, {total_items} item-tag(s):\n")
    for family, tags in families.items():
        note = "  (screening decisions — --prune needs --force)" if is_decision_family(family) else ""
        print(f"  {family}{note}")
        for tag, n in sorted(tags, key=lambda kv: (-kv[1], kv[0])):
            print(f"    {tag:<{width}}  {n:>5} item(s)")
        if len(tags) > 20:
            print(
                f"    -- {len(tags)} distinct values. That is a lot for a tag "
                f"selector; consider --prune {family}.",
            )
        print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    zotero_io.add_library_args(parser)
    tag_prefix.add_argument(parser)
    parser.add_argument(
        "--config", default="./screening_config.py",
        help="Project screening config, read for TAG_PREFIX "
             "(default: ./screening_config.py).",
    )
    parser.add_argument(
        "--collection", default="",
        help="Zotero collection key or name. Omit to scan the whole library.",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="List every tag in this review's namespace with item counts.",
    )
    parser.add_argument(
        "--prune", nargs="+", default=[],
        help="Remove whole tag families, named by coding field "
             "(`research_design`) or by the family in the tag "
             "(`research-design`).",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually write. Without it the run is a dry run.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Permit pruning a decision family (abstract / fulltext / qa). "
             "Those hold screening verdicts, recoverable only by re-screening.",
    )
    args = parser.parse_args()

    if not args.list and not args.prune:
        parser.error("nothing to do: pass --list or --prune.")

    ns = screening_common.load_tag_namespace(args.tag_prefix, args.config)
    zot = zotero_io.ZoteroClient.from_args(args)

    scope = f"collection={args.collection}" if args.collection else "whole library"
    print(f"Reading Zotero ({zot.describe_library()}, {scope})...", flush=True)
    items = (
        zot.collection_items(args.collection, item_type="journalArticle")
        if args.collection else zot.journal_articles()
    )
    print(f"  {len(items)} item(s)\n", flush=True)

    counts = tags_in_namespace(items, ns)

    if args.list:
        print_listing(counts, ns)
        if not args.prune:
            return 0

    families = []
    for raw in args.prune:
        family_prefix = resolve_family(raw, ns)
        family_name = family_prefix[len(ns):-1]
        if is_decision_family(family_name) and not args.force:
            sys.exit(
                f"ERROR: `{family_name}` holds screening decisions, not coded "
                f"attributes.\n"
                f"       Pruning it discards this review's verdicts, and the "
                f"only way back is to\n"
                f"       re-screen. Pass --force as well if that is really "
                f"what you want."
            )
        families.append(family_prefix)

    targets = [
        (item["key"], [t for t in _tags_of(item) if _in_any(t, families)])
        for item in items
    ]
    targets = [(k, tags) for k, tags in targets if tags]

    if not targets:
        print(f"Nothing to prune: no items carry {', '.join(families)}*.")
        return 0

    affected_tags = sorted({t for _, tags in targets for t in tags})
    print(
        f"{'Would remove' if not args.apply else 'Removing'} "
        f"{len(affected_tags)} tag(s) from {len(targets)} item(s):",
    )
    for tag in affected_tags:
        n = sum(1 for _, tags in targets if tag in tags)
        print(f"  {tag}  ({n} item(s))")

    if not args.apply:
        print("\nDry run — nothing written. Re-run with --apply to remove.")
        return 0

    updates = [(key, {"remove_prefixed": families}) for key, _ in targets]
    stats = zot.batch_update_tags(updates)
    print(
        f"\nDone. applied={stats['applied']} unchanged={stats['unchanged']} "
        f"failed={stats['failed']}",
    )
    return 0 if stats["failed"] == 0 else 1


def _tags_of(item: dict) -> list[str]:
    return [t.get("tag", "") for t in item.get("data", {}).get("tags", [])]


def _in_any(tag: str, families: list[str]) -> bool:
    return any(tag.startswith(f) for f in families)


if __name__ == "__main__":
    raise SystemExit(main())
