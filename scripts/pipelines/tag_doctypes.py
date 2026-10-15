#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyzotero>=1.15.1",
#     "tenacity>=8.0",
#     "httpx>=0.25",
# ]
# ///
"""Tag supplements, front matter and notices already in a Zotero library.

`import_to_zotero.py` tags these records as it creates them. This script
does the same for items imported before it did, or added some other way:

- ``doctype:supplement``   — APA supplemental material (DOI ends ``.supp``,
  title "Supplemental Material for …")
- ``doctype:front-matter`` — "Editorial Board", "Issue Information",
  "Featured Cover", "Acknowledgment of Ad Hoc Reviewers", author-less
  "Call for Papers: …"
- ``doctype:notice``       — SAGE "Publication Notice" (retraction /
  withdrawal notices)

The rules live in `doctype.py`. The tags are global — not under the
review's `TAG_PREFIX` — because they state a fact about the record, so
this script needs no screening config. Each supplement whose article is
in the library is also related to it (Zotero "Related", both directions).

Nothing is removed or deleted: items are only tagged and related. Screening
(`abstract_screen.py`, `fulltext_code.py`) then skips tagged items and
prints the count per class for the PRISMA flow.

Dry-run by default; nothing is written until `--apply`.

Usage:
    uv run tag_doctypes.py --group 6015547
    uv run tag_doctypes.py --group 6015547 --collection "AI SLR" --apply
    uv run tag_doctypes.py --group 6015547 --apply --no-link
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = SCRIPT_DIR.parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import doctype  # noqa: E402
import zotero_io  # noqa: E402


def plan(
    targets: list[dict], library: list[dict],
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """`(to_tag, links)` for a dry run or an apply.

    `to_tag` is `(item_key, doctype)` for every target that `classify`
    recognises and that carries no `doctype:*` tag yet — an item with one
    (including a hand-set value) is left alone. `links` pairs each
    supplement among the targets with its article anywhere in `library`.
    """
    to_tag: list[tuple[str, str]] = []
    supplement_dois: list[str] = []
    for item in targets:
        d = item.get("data", {})
        existing = doctype.from_tags(d.get("tags", []))
        dt = existing or doctype.classify_zotero_item(item)
        if dt == doctype.SUPPLEMENT and d.get("DOI"):
            supplement_dois.append(d["DOI"])
        if dt and not existing:
            to_tag.append((item["key"], dt))

    key_by_doi: dict[str, str] = {}
    for item in library:
        doi = doctype.norm_doi(item.get("data", {}).get("DOI"))
        if doi:
            key_by_doi.setdefault(doi, item["key"])
    return to_tag, doctype.supplement_links(supplement_dois, key_by_doi)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    zotero_io.add_library_args(parser)
    parser.add_argument(
        "--collection", default="",
        help="Zotero collection key or name to tag. Omit for the whole "
             "library. Parents of supplements are looked up library-wide "
             "either way.",
    )
    parser.add_argument(
        "--no-link", action="store_true",
        help="Tag only; do not relate supplements to their articles.",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually write. Without it the run is a dry run.",
    )
    args = parser.parse_args(argv)

    zot = zotero_io.ZoteroClient.from_args(args)
    scope = (f"collection={args.collection}" if args.collection
             else "whole library")
    print(f"Reading Zotero ({zot.describe_library()}, {scope})...", flush=True)
    library = zot.journal_articles()
    targets = (
        zot.collection_items(args.collection, item_type="journalArticle")
        if args.collection else library
    )
    print(f"  {len(targets)} item(s) to check", flush=True)

    to_tag, links = plan(targets, library)
    if args.no_link:
        links = []

    counts: dict[str, int] = {}
    for _key, dt in to_tag:
        counts[dt] = counts.get(dt, 0) + 1
    verb = "Tagging" if args.apply else "Would tag"
    if to_tag:
        print(f"{verb} {len(to_tag)} item(s): {doctype.format_counts(counts)}")
    else:
        print("No untagged supplements, front matter or notices found.")
    if links:
        print(f"{'Relating' if args.apply else 'Would relate'} {len(links)} "
              f"supplement(s) to their article.")

    if not args.apply:
        if to_tag or links:
            print("\nDry run — nothing written. Re-run with --apply.")
        return 0

    ok = True
    if to_tag:
        stats = zot.batch_update_tags(
            [(key, {"add": [doctype.tag_for(dt)]}) for key, dt in to_tag],
        )
        print(f"  Tags: applied={stats['applied']} "
              f"unchanged={stats['unchanged']} failed={stats['failed']}")
        ok = stats["failed"] == 0
    if links:
        _linked, failed = doctype.link_supplements(links, zot)
        ok = ok and failed == 0
    return 0 if ok else 1


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
