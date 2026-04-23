#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyzotero>=1.6",
#     "requests>=2.31",
#     "tenacity>=8.0",
#     "httpx>=0.25",
# ]
# ///
"""Import a deduplicated search-results CSV into a Zotero group library.

Reads a CSV with at least `doi`, `title`, `authors`, `year`, `source`,
`issn`, `abstract`, and optional `query` columns. For each row:

- If the DOI already exists in the target library: add to the target
  collection (if given) and backfill a missing abstract.
- If the title+first-author matches an existing item without a DOI:
  same.
- Otherwise: create a new `journalArticle` item in the collection.

Also deduplicates **within** the import batch, so two input rows for
the same paper (e.g. Scopus + WoS where only one has a DOI) merge
into one new item rather than creating duplicates.

After import: **run a duplicate check via
`mcp__zotero__zotero_find_duplicates`** or Zotero's Tools menu.
Pre-existing items with incomplete metadata can still slip through
the DOI + title-author matching.

Usage:
    uv run import_to_zotero.py --group 6015547 --input search.csv
    uv run import_to_zotero.py --group 6015547 --collection BSEJHPJN \\
        --input search.csv --dry-run
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = SCRIPT_DIR.parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from core.config_loader import require  # noqa: E402

try:
    import requests
except ImportError:
    sys.exit(
        "ERROR: dependencies not available. Run via `uv run`; the PEP 723 "
        "block at the top declares pyzotero + requests."
    )

import zotero_io  # noqa: E402

BATCH_SIZE = 50  # Zotero write API max


def _parse_authors(author_str: str) -> list[dict]:
    """Parse 'Last, First; Last, First' into Zotero creator dicts."""
    creators: list[dict] = []
    if not author_str:
        return creators
    for part in author_str.split(";"):
        part = part.strip()
        if not part:
            continue
        if "," in part:
            last, _, first = part.partition(",")
            creators.append({
                "creatorType": "author",
                "firstName": first.strip(),
                "lastName": last.strip(),
            })
        else:
            creators.append({"creatorType": "author", "name": part})
    return creators


def _row_to_zotero_item(row: dict, collection_key: str | None) -> dict:
    item: dict = {
        "itemType": "journalArticle",
        "title": row.get("title", ""),
        "creators": _parse_authors(row.get("authors", "")),
        "publicationTitle": row.get("source", ""),
        "date": row.get("year", ""),
        "DOI": row.get("doi", ""),
        "ISSN": row.get("issn", ""),
        "abstractNote": row.get("abstract", ""),
        "extra": "",
    }
    if collection_key:
        item["collections"] = [collection_key]
    tags: list[dict] = []
    if row.get("query"):
        tags.append({"tag": f"search:{row['query']}", "type": 1})

    # Predatory-journal preflight: check the title / ISSN against the
    # Beall's-list snapshot in `sources/predatory.py`. Flag (don't
    # exclude) per the social-sciences convention in the
    # systematic-review skill. The screener sees the flag and decides
    # during full-text review.
    try:
        from sources.predatory import check_predatory
    except ImportError:
        check_predatory = None  # type: ignore[assignment]
    if check_predatory is not None:
        result = check_predatory(
            journal=row.get("source") or None,
            issn=row.get("issn") or None,
        )
        if result.is_predatory:
            tags.append({"tag": "predatory:flag", "type": 1})

    if tags:
        item["tags"] = tags
    return item


def _title_author_key(title: str, authors) -> str:
    """Normalised 'title|first_author_lastname' for fuzzy dedup."""
    t = re.sub(r"\W+", " ", (title or "").lower()).strip()
    first_last = ""
    if isinstance(authors, list) and authors:
        first_last = (
            authors[0].get("lastName") or authors[0].get("name") or ""
        ).lower()
    elif isinstance(authors, str) and authors:
        first_last = authors.split(";")[0].split(",")[0].strip().lower()
    return f"{t}|{first_last}"


def _fetch_existing_items(
    zot: zotero_io.ZoteroClient, dry_run: bool,
) -> tuple[dict[str, str], dict[str, str]]:
    """Return (doi_map, title_map) for existing items in the library."""
    if dry_run:
        return {}, {}
    print("Fetching existing library items via local Zotero client...", flush=True)
    items = zot.journal_articles()

    doi_map: dict[str, str] = {}
    title_map: dict[str, str] = {}
    for item in items:
        d = item.get("data", {})
        key = d.get("key", item.get("key", ""))
        doi = (d.get("DOI") or "").strip().lower()
        if doi:
            doi_map[doi] = key
        tk = _title_author_key(d.get("title", ""), d.get("creators", []))
        if tk and tk not in title_map:
            title_map[tk] = key

    print(f"  {len(items)} items: {len(doi_map)} with DOI, "
          f"{len(title_map)} indexed by title+author.", flush=True)
    return doi_map, title_map


def _patch_existing_items(
    to_add: list[tuple[str, str]],
    zot: zotero_io.ZoteroClient,
    collection_key: str | None,
) -> None:
    """Patch existing items to add missing abstracts and/or collection
    membership. Uses ZoteroClient.update_item (pyzotero) — the custom
    If-Unmodified-Since-Version requests.patch() that used to live here
    is gone.
    """
    if not to_add:
        return
    print(f"\nReading {len(to_add)} existing items from local Zotero...", flush=True)
    all_items = zot.journal_articles()
    item_by_key = {it["key"]: it for it in all_items}

    need_patch: list[tuple[str, int, dict]] = []
    abstract_patched = 0
    for item_key, abstract in to_add:
        item = item_by_key.get(item_key)
        if not item:
            continue
        d = item.get("data", {})
        patch: dict = {}
        if collection_key:
            colls = d.get("collections", []) or []
            if collection_key not in colls:
                patch["collections"] = colls + [collection_key]
        if not (d.get("abstractNote") or "").strip() and abstract:
            patch["abstractNote"] = abstract
            abstract_patched += 1
        if patch:
            need_patch.append((item_key, item["version"], patch))

    print(f"  Items needing patch: {len(need_patch)} "
          f"(abstracts to backfill: {abstract_patched}).", flush=True)

    for i, (item_key, version, patch) in enumerate(need_patch, 1):
        if i % 50 == 0 or i == len(need_patch):
            print(f"  [{i}/{len(need_patch)}] patching...", flush=True)
        zot.update_item({"key": item_key, "version": version, **patch})
        time.sleep(0.15)


def _create_new_items(
    to_create: list[dict],
    zot: zotero_io.ZoteroClient,
) -> tuple[int, int]:
    base_url = zot.api_base_url()
    headers = {
        "Zotero-API-Key": zot.api_key,
        "Zotero-API-Version": "3",
        "Content-Type": "application/json",
    }
    created = failed = 0
    n_batches = (len(to_create) + BATCH_SIZE - 1) // BATCH_SIZE
    for batch_num, i in enumerate(range(0, len(to_create), BATCH_SIZE), 1):
        batch = to_create[i:i + BATCH_SIZE]
        print(f"  batch {batch_num}/{n_batches} ({len(batch)} items)...", flush=True)
        resp = requests.post(
            f"{base_url}/items", headers=headers, json=batch, timeout=60,
        )
        resp.raise_for_status()
        result = resp.json()
        created += len(result.get("success", {}))
        failed += len(result.get("failed", {}))
        if result.get("failed"):
            for idx, err in result["failed"].items():
                print(f"  FAILED item {idx}: {err}", flush=True)
        time.sleep(0.5)
    return created, failed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    zotero_io.add_library_args(parser)
    parser.add_argument("--collection",
                        default=os.environ.get("ZOTERO_SLR_COLL", ""),
                        help="Collection key to add items into "
                             "(default: $ZOTERO_SLR_COLL, optional).")
    parser.add_argument("--input", required=True,
                        help="Path to deduplicated search-results CSV.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and report without writing to Zotero.")
    args = parser.parse_args()

    api_key = "" if args.dry_run else require("zotero", "api_key",
                                              env="ZOTERO_API_KEY")
    zot = zotero_io.ZoteroClient.from_args(args, api_key=api_key or "dummy")

    csv_path = Path(args.input)
    if not csv_path.exists():
        sys.exit(f"ERROR: --input path not found: {csv_path}")

    with csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    print(f"Records to import into {zot.describe_library()}: {len(rows)}",
          flush=True)

    doi_map, title_map = _fetch_existing_items(zot, args.dry_run)

    to_add: list[tuple[str, str]] = []
    to_create: list[dict] = []
    batch_doi_seen: dict[str, int] = {}
    batch_title_seen: dict[str, int] = {}
    dropped_within_batch = 0

    for row in rows:
        doi = (row.get("doi") or "").strip().lower()
        abstract = (row.get("abstract") or "").strip()

        if doi and doi in doi_map:
            to_add.append((doi_map[doi], abstract))
            continue

        tk = _title_author_key(row.get("title", ""), row.get("authors", ""))
        if tk and tk in title_map:
            to_add.append((title_map[tk], abstract))
            continue

        # Within-batch dedup — merge rather than duplicate
        if doi and doi in batch_doi_seen:
            idx = batch_doi_seen[doi]
            if not to_create[idx].get("abstractNote") and abstract:
                to_create[idx]["abstractNote"] = abstract
            dropped_within_batch += 1
            continue
        if tk and tk in batch_title_seen:
            idx = batch_title_seen[tk]
            if doi and not to_create[idx].get("DOI"):
                to_create[idx]["DOI"] = doi
            if not to_create[idx].get("abstractNote") and abstract:
                to_create[idx]["abstractNote"] = abstract
            dropped_within_batch += 1
            continue

        item = _row_to_zotero_item(row, args.collection or None)
        idx = len(to_create)
        to_create.append(item)
        if doi:
            batch_doi_seen[doi] = idx
        if tk:
            batch_title_seen[tk] = idx

    print(f"  Already in library (patch only): {len(to_add)}", flush=True)
    print(f"  New items to create:             {len(to_create)}", flush=True)
    if dropped_within_batch:
        print(f"  Within-batch duplicates merged:  {dropped_within_batch}",
              flush=True)

    if args.dry_run:
        print("\n[DRY RUN] No changes written.", flush=True)
        return 0

    _patch_existing_items(to_add, zot, args.collection or None)

    created = 0
    if to_create:
        print(f"\nCreating {len(to_create)} new items...", flush=True)
        created, failed = _create_new_items(to_create, zot)
        print(f"  Created: {created}  Failed: {failed}", flush=True)

    total = len(to_add) + created
    print(f"\nDone. {total} items now in target collection/library.", flush=True)
    print(
        "\nNEXT STEP — run a duplicate check. Use the Zotero MCP tool "
        "`zotero_find_duplicates` (or Zotero → Tools → Duplicate Items) "
        "and merge anything it surfaces before moving on to abstract "
        "screening. Within-batch duplicates are caught automatically, but "
        "pre-existing items with incomplete metadata can still slip "
        "through DOI + title-author matching.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
