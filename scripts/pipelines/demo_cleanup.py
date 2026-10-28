#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyzotero>=1.16",
#     "tenacity>=8.0",
#     "urllib3>=2.0",
#     "httpx>=0.25",
# ]
# ///
"""Remove what a `demo-slr` run created in Zotero, and nothing else.

Deletes exactly the items listed in `--keys-file` (the file
`import_to_zotero.py --created-keys-out` wrote: items the demo CREATED,
never ones it merely touched) and then the demo collection. Deleting an
item removes its attachment, note and highlights with it.

Two guards keep this from ever reaching a real library's contents:
the tag namespace must start with `demo-`, and an item is deleted only if
it still carries a tag in that namespace. The collection is deleted only
if its name starts with `demo-`.

Usage:
    uv run demo_cleanup.py --user --collection ABCD1234 \\
        --tag-prefix demo-20261028-1430 --keys-file output/demo/created.keys
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import zotero_io  # noqa: E402

DEMO_PREFIX = "demo-"


def cleanup(zot, *, collection: str, prefix: str, keys: list[str],
            dry_run: bool) -> dict:
    """Delete the demo's items and collection; returns the counts."""
    if not prefix.startswith(DEMO_PREFIX):
        raise SystemExit(
            f"ERROR: refusing to clean up: tag prefix {prefix!r} does not "
            f"start with {DEMO_PREFIX!r}.")
    ns = prefix.rstrip("/") + "/"
    result = {"deleted_items": 0, "skipped_items": 0,
              "deleted_collection": False}
    for key in keys:
        try:
            item = zot.get_item(key)
        except Exception:
            result["skipped_items"] += 1       # already gone
            continue
        tags = [t.get("tag", "") for t in item.get("data", {}).get("tags", [])]
        if not any(t.startswith(ns) for t in tags):
            result["skipped_items"] += 1       # not carrying the demo's tags
            continue
        if dry_run or zot.delete_item(key):
            result["deleted_items"] += 1
        else:
            result["skipped_items"] += 1
    if collection:
        try:
            name = zot._client().collection(collection)["data"]["name"]
        except Exception:
            name = ""
        if name.startswith(DEMO_PREFIX):
            result["deleted_collection"] = dry_run or bool(
                zot.delete_collection(collection))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    zotero_io.add_library_args(parser)
    parser.add_argument("--collection", default="", help="Demo collection key.")
    parser.add_argument("--tag-prefix", required=True,
                        help="The demo's tag namespace (must start with demo-).")
    parser.add_argument("--keys-file", required=True,
                        help="Created-item keys, one per line.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    path = Path(args.keys_file)
    keys = [k.strip() for k in path.read_text(encoding="utf-8").splitlines()
            if k.strip()] if path.is_file() else []
    zot = zotero_io.ZoteroClient.from_args(args)
    res = cleanup(zot, collection=args.collection, prefix=args.tag_prefix,
                  keys=keys, dry_run=args.dry_run)
    verb = "Would delete" if args.dry_run else "Deleted"
    print(f"{verb} {res['deleted_items']} item(s); "
          f"skipped {res['skipped_items']}; "
          f"collection {'removed' if res['deleted_collection'] else 'kept'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
