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

# Journal-aliases lookup tables, populated on first call. The CSV ships
# with the plugin at scripts/pipelines/data/journal_aliases.csv and grows
# over time as users encounter dedup misses across search databases.
# Two indices: name → canonical (lowercase variant lookup) and ISSN →
# canonical (catches cases where the variant isn't yet in the table but
# the ISSN matches a known canonical entry).
_DATA_DIR = SCRIPT_DIR / "data"
_JOURNAL_ALIAS_BY_NAME: dict[str, str] = {}
_JOURNAL_ALIAS_BY_ISSN: dict[str, str] = {}
_JOURNAL_ALIASES_LOADED = False


def _canonicalize_issn(issn: str) -> str:
    """Normalize an ISSN to canonical L-form: ``NNNN-NNNN`` (or NNNN-NNNX
    for check-digit X).

    Scopus emits ISSNs without hyphens (``00401625``) while WoS, Crossref,
    and OpenAlex keep the hyphen (``0040-1625``). Returning a single
    canonical shape makes downstream dedup work; without it, two rows
    pointing at the same journal can survive as duplicates only because
    their ISSN strings don't compare equal.

    Returns ``""`` if the input doesn't normalize to 8 digits + optional
    check-digit X — never a partial canonical form.
    """
    if not issn:
        return ""
    cleaned = re.sub(r"[^0-9Xx]", "", issn)
    if len(cleaned) != 8:
        return ""
    return f"{cleaned[:4]}-{cleaned[4:].upper()}"


def _load_journal_aliases() -> None:
    """Populate the lookup tables from data/journal_aliases.csv on first call."""
    global _JOURNAL_ALIASES_LOADED
    if _JOURNAL_ALIASES_LOADED:
        return
    _JOURNAL_ALIASES_LOADED = True
    path = _DATA_DIR / "journal_aliases.csv"
    if not path.is_file():
        return
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            variant = (row.get("variant") or "").strip().lower()
            canonical = (row.get("canonical") or "").strip()
            issn = _canonicalize_issn(row.get("issn") or "")
            if variant and canonical:
                _JOURNAL_ALIAS_BY_NAME[variant] = canonical
            if issn and canonical:
                _JOURNAL_ALIAS_BY_ISSN.setdefault(issn, canonical)


def _canonicalize_journal_name(name: str, issn: str = "") -> str:
    """Map a journal-name variant to its canonical form using the
    plugin-shipped alias table.

    Lookup order:
      1. exact case-insensitive match of the trimmed name in the
         variant→canonical table;
      2. fall back to canonical ISSN match (catches cases where the
         variant string isn't yet in the table but the ISSN identifies
         the journal);
      3. otherwise return the input name (trimmed).

    Pure function; safe to call from `_row_to_zotero_item` once per row.
    """
    _load_journal_aliases()
    if not name:
        return ""
    key = name.strip().lower()
    if key in _JOURNAL_ALIAS_BY_NAME:
        return _JOURNAL_ALIAS_BY_NAME[key]
    canonical_issn = _canonicalize_issn(issn)
    if canonical_issn and canonical_issn in _JOURNAL_ALIAS_BY_ISSN:
        return _JOURNAL_ALIAS_BY_ISSN[canonical_issn]
    return name.strip()


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
    # Canonicalize at ingest so dedup downstream works across databases:
    # Scopus strips ISSN hyphens (`00401625`) while WoS keeps them
    # (`0040-1625`); journal names abbreviate inconsistently
    # (`Strat Manag J` vs `Strategic Management Journal`). Both fixes
    # are pure, table-driven, and skip-safe (returning the input on miss).
    canonical_issn = _canonicalize_issn(row.get("issn", ""))
    canonical_source = _canonicalize_journal_name(
        row.get("source", ""), canonical_issn,
    )
    item: dict = {
        "itemType": "journalArticle",
        "title": row.get("title", ""),
        "creators": _parse_authors(row.get("authors", "")),
        "publicationTitle": canonical_source,
        "date": row.get("year", ""),
        "DOI": row.get("doi", ""),
        "ISSN": canonical_issn,
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
    # during full-text review. Use the canonical name + ISSN here so a
    # Scopus-abbreviated entry like "J Bus Venturing" matches the same
    # predatory-list entries as the WoS-form "Journal of Business
    # Venturing" — without canonicalization they would each be checked
    # against different keys and only one might hit.
    try:
        from sources.predatory import check_predatory
    except ImportError:
        check_predatory = None  # type: ignore[assignment]
    if check_predatory is not None:
        result = check_predatory(
            journal=canonical_source or None,
            issn=canonical_issn or None,
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
