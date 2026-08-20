#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyzotero>=1.6",
#     "requests>=2.31",
#     "tenacity>=8.0",
#     "httpx>=0.25",
#     "zotero-mcp-server>=0.9,<0.10",
# ]
# ///
"""Import a deduplicated search-results CSV into a Zotero group library.

Reads a CSV with at least `doi`, `title`, `authors`, `year`, `source`,
`issn`, `abstract`, and optional `query`, `volume`, `issue`, `pages`,
`type` columns. For each row:

- If the DOI already exists in the target library: add to the target
  collection (if given) and backfill a missing abstract.
- If the title+first-author matches an existing item without a DOI:
  same.
- Otherwise: create a new item.

**Where a new item's metadata comes from** — two paths, and an item is
built wholly by one or the other, so its provenance is never a mixture:

1. *Source-built.* The search databases return volume, issue, pages and
   a document type, and Scopus and WoS return creators already split at
   the comma. A row carrying all of that is mapped straight to a Zotero
   item with **no network call beyond Zotero itself**. This is the
   normal path and the fast one.
2. *Authority-filled.* A row whose creators are in display order
   ("Jane Doe" — all OpenAlex and Semantic Scholar rows) or whose type
   is missing cannot produce a correct item on its own. Those rows get
   one Crossref CSL-JSON fetch per DOI, converted by
   `zotero_mcp.citation_import.csl_json_to_zotero`, which is the
   converter the Zotero MCP server itself uses.

Either way the plugin's own concerns are layered on top: ISSN and
journal-name canonicalization, `search:` provenance tags, the predatory
flag, collection membership, and the search database's abstract (which
beats Crossref's — Crossref abstracts are sparsely deposited).

Nothing here composes citation metadata by hand: every field traces to
a database response. See the IRON RULE in
`skills/zotero-operations/SKILL.md`.

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
    uv run import_to_zotero.py --group 6015547 --collection "AI SLR" \\
        --input search.csv      # by name; created if it doesn't exist
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = SCRIPT_DIR.parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from core.config_loader import require  # noqa: E402

try:
    import requests  # noqa: F401  # imported for the availability check below
except ImportError:
    sys.exit(
        "ERROR: dependencies not available. Run via `uv run`; the PEP 723 "
        "block at the top declares pyzotero + requests."
    )

import doi_utils  # noqa: E402
import http_client  # noqa: E402
import zotero_io  # noqa: E402
from zotero_mcp.citation_import import csl_json_to_zotero  # noqa: E402
from zotero_mcp.schema import valid_fields  # noqa: E402

try:  # stdlib-only module; absent only if the script is run oddly
    from sources import predatory
except ImportError:  # pragma: no cover — defensive
    predatory = None  # type: ignore[assignment]

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


def _normalize_doi_key(raw: str) -> str:
    """Canonical dedup key for a DOI string.

    The *strict* `doi_utils` helper: strips ``doi:``/URL prefixes and
    trailing punctuation, validates the DOI shape, and lowercases for
    case-insensitive comparison, so a library item stored as
    ``https://doi.org/10.1234/ABC`` and a CSV row of bare ``10.1234/abc``
    match instead of creating a duplicate. Returns ``""`` (falsy) for
    empty/malformed input, matching the `if doi and doi in doi_map`
    pattern callers already use — a malformed DOI must not dedup against
    another malformed DOI.
    """
    return doi_utils.doi_key(raw)


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


def _has_split_creators(author_str: str) -> bool:
    """True when every creator in the column is in `Last, First` form.

    This is the routing question for a whole record. Scopus and WoS
    return names split at the comma; OpenAlex and Semantic Scholar
    return display order, and *no* given/family split exists anywhere in
    their responses — so for those rows the split has to come from an
    authority (Crossref), not from a rule applied to the string.

    Empty is False: a record with no creators at all is exactly one an
    authority can improve.
    """
    parts = [p.strip() for p in (author_str or "").split(";") if p.strip()]
    return bool(parts) and all("," in p for p in parts)


def _parse_authors(author_str: str) -> list[dict]:
    """Parse the `authors` column into Zotero creator dicts.

    `Last, First` is the shape this pipeline's own searchers promise for
    splittable names, and it is unambiguous. The two fallbacks are a
    safety net for rows this pipeline did not produce — a hand-made CSV,
    or a source whose fill path failed:

    - `First Last` (display order): last whitespace-separated token is
      taken as the family name. Wrong for "Ludwig van Beethoven" and for
      Spanish double surnames, which is why display-order rows are
      routed to the authority path *first* and only land here when that
      path could not run. It is still far better than what it replaced —
      a single-field `name` creator, which is what put "Vanacker Tom"
      into a live library as an unsplit blob.
    - a single token: kept as a one-field `name` creator, which is what
      Zotero uses for corporate authors ("OECD") and is correct there.
    """
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
        elif " " in part:
            first, _, last = part.rpartition(" ")
            creators.append({
                "creatorType": "author",
                "firstName": first.strip(),
                "lastName": last.strip(),
            })
        else:
            creators.append({"creatorType": "author", "name": part})
    return creators


#: Crossref `type` → Zotero `itemType`. Only what a literature search
#: actually returns; anything absent falls back to the default.
_CROSSREF_TYPE_TO_ZOTERO = {
    "journal-article": "journalArticle",
    "proceedings-article": "conferencePaper",
    "book-chapter": "bookSection",
    "book-part": "bookSection",
    "book-section": "bookSection",
    "reference-entry": "dictionaryEntry",
    "book": "book",
    "monograph": "book",
    "edited-book": "book",
    "reference-book": "book",
    "report": "report",
    "report-component": "report",
    "dissertation": "thesis",
    "posted-content": "preprint",
}

#: Where a row's `source` column belongs, per item type. A `book`'s
#: source *is* the book — already in `title` — so it has no container
#: field here and the value is dropped rather than duplicated. Getting
#: this wrong is silent: `_filter_valid_fields` would drop
#: `publicationTitle` from a `bookSection` and the book title would
#: vanish with only a per-row warning.
_CONTAINER_FIELD = {
    "journalArticle": "publicationTitle",
    "conferencePaper": "proceedingsTitle",
    "bookSection": "bookTitle",
    "dictionaryEntry": "dictionaryTitle",
    "preprint": "repository",
}

_DEFAULT_ITEM_TYPE = "journalArticle"

#: Crossref's CSL-JSON transform. Returns the record as CSL JSON
#: directly — no `message` envelope — which is what
#: `csl_json_to_zotero` consumes.
_CROSSREF_CSL_URL = (
    "https://api.crossref.org/works/{doi}/transform/"
    "application/vnd.citationstyles.csl+json"
)

#: How an item's metadata was obtained, for the per-run summary.
BUILD_SOURCE = "source"        # from the search row alone, no network
BUILD_AUTHORITY = "authority"  # from Crossref CSL via zotero-mcp
BUILD_FALLBACK = "fallback"    # wanted the authority, couldn't reach it


def row_item_type(row: dict) -> str:
    """Zotero itemType from the row's own `type` column, or `""`.

    The four searchers normalise their native document types into
    Crossref's vocabulary precisely so this lookup is a table read
    rather than an HTTP request. `""` means "this row cannot say" — an
    older CSV without the column, or a type the source did not
    recognise — and the caller reads that as a reason to ask the
    authority, never as a reason to assume `journalArticle`.

    Getting the type wrong is not cosmetic: a mis-typed book chapter
    passes `journal_articles()`, is routed to article-only PDF handlers
    and cannot succeed there. Five such items in one live corpus each
    burned a browser slot to produce an unexplained stall.
    """
    raw = str(row.get("type") or "").strip().lower()
    return _CROSSREF_TYPE_TO_ZOTERO.get(raw, "")


def _container_field_of(item: dict) -> str | None:
    """Which field holds this item's container title.

    `_CONTAINER_FIELD` covers the types a literature search returns.
    The authority path can produce others (a `magazineArticle`, say), so
    fall back to whichever standard container field the built item
    already has.
    """
    field = _CONTAINER_FIELD.get(item.get("itemType", ""))
    if field:
        return field
    for candidate in ("publicationTitle", "bookTitle", "proceedingsTitle"):
        if candidate in item:
            return candidate
    return None


def _apply_plugin_layers(
    item: dict, row: dict, collection_key: str | None,
) -> dict:
    """Layer this plugin's concerns onto an item, whoever built it.

    Everything here is knowledge the converter cannot have: which
    collection the run targets, which query found the paper, what this
    project considers a canonical journal name, and that a search
    database's abstract beats Crossref's (Crossref abstracts are
    sparsely deposited — a fill would otherwise *lose* the abstract the
    search already returned).

    Applied identically on both build paths, so a source-built and an
    authority-filled item differ only in where their bibliographic
    fields came from.
    """
    # Canonicalize at ingest so dedup downstream works across databases:
    # Scopus strips ISSN hyphens (`00401625`) while WoS keeps them
    # (`0040-1625`); journal names abbreviate inconsistently
    # (`Strat Manag J` vs `Strategic Management Journal`). Both fixes
    # are pure, table-driven, and skip-safe (returning the input on miss).
    canonical_issn = _canonicalize_issn(
        row.get("issn", "") or item.get("ISSN", ""),
    )
    if canonical_issn:
        item["ISSN"] = canonical_issn

    container = _container_field_of(item)
    raw_container = (item.get(container) or "") if container else ""
    # The ISSN→name fallback only makes sense for a periodical. A book
    # chapter's container is the book, and a record can carry an ISSN
    # that belongs to a series or was copied across by the search
    # database — letting that rewrite `bookTitle` swaps a real book
    # title for a journal name with nothing to notice it.
    issn_hint = canonical_issn if container == "publicationTitle" else ""
    canonical_source = _canonicalize_journal_name(
        raw_container or row.get("source", ""), issn_hint,
    )
    if container and canonical_source:
        item[container] = canonical_source

    if not (item.get("DOI") or "").strip() and row.get("doi"):
        item["DOI"] = row["doi"]

    # The search database's abstract wins outright.
    if (row.get("abstract") or "").strip():
        item["abstractNote"] = row["abstract"]

    if collection_key:
        item["collections"] = [collection_key]

    tags: list[dict] = [
        t for t in (item.get("tags") or [])
        if isinstance(t, dict) and t.get("tag")
    ]
    if row.get("query"):
        tags.append({"tag": f"search:{row['query']}", "type": 1})

    # Predatory-journal preflight: check the name / ISSN against the
    # Beall's-list snapshot in `sources/predatory.py`. Flag (don't
    # exclude) per the social-sciences convention in the
    # systematic-review skill. The screener sees the flag and decides
    # during full-text review. Use the canonical name + ISSN here so a
    # Scopus-abbreviated entry like "J Bus Venturing" matches the same
    # predatory-list entries as the WoS-form "Journal of Business
    # Venturing" — without canonicalization they would each be checked
    # against different keys and only one might hit.
    # Called through the module rather than a bound name so the lookup
    # stays late — the table is loaded lazily and tests substitute it.
    if predatory is not None:
        result = predatory.check_predatory(
            journal=canonical_source or None,
            issn=canonical_issn or None,
        )
        if result.is_predatory:
            tags.append({"tag": "predatory:flag", "type": 1})

    if tags:
        item["tags"] = tags
    return item


def _row_to_zotero_item(
    row: dict, collection_key: str | None,
    item_type: str = _DEFAULT_ITEM_TYPE,
) -> dict:
    """Build an item from the search row alone — no network."""
    item: dict = {
        "itemType": item_type,
        "title": row.get("title", ""),
        "creators": _parse_authors(row.get("authors", "")),
        "date": row.get("year", ""),
        "DOI": row.get("doi", ""),
        "ISSN": _canonicalize_issn(row.get("issn", "")),
        "abstractNote": row.get("abstract", ""),
        "volume": row.get("volume", "") or "",
        "issue": row.get("issue", "") or "",
        "pages": row.get("pages", "") or "",
        "extra": "",
    }
    container = _CONTAINER_FIELD.get(item_type)
    if container:
        item[container] = row.get("source", "")
    return _apply_plugin_layers(item, row, collection_key)


def _item_template(item_type: str) -> dict:
    """A blank Zotero item of `item_type`, for the CSL converter.

    `csl_json_to_zotero` writes a field only when the template already
    has that key, which is how it avoids emitting fields the type does
    not accept. pyzotero's `item_template()` would fetch this from
    api.zotero.org; `zotero_mcp.schema.valid_fields` answers the same
    question from an on-disk cache with no network at all, which keeps
    `--dry-run` honest offline and costs nothing per row.

    Raises KeyError for a type the schema does not know, so the caller
    can fall back rather than build an item with no fields in it.
    """
    fields = valid_fields(item_type)
    if not fields:
        raise KeyError(item_type)
    template: dict = {"itemType": item_type}
    template.update({f: "" for f in sorted(fields)})
    template["creators"] = []
    template["tags"] = []
    return template


#: Crossref `type` → CSL `type`, so `csl_json_to_zotero`'s own
#: `CSL_TYPE_MAP` lands on the same Zotero itemType that
#: `_CROSSREF_TYPE_TO_ZOTERO` would.
#:
#: Crossref's "CSL JSON" transform is not quite CSL: it keeps Crossref's
#: type names (`journal-article`, `book-chapter`) where CSL says
#: `article-journal` and `chapter`. The converter looks up the CSL
#: spelling, so handing it Crossref's unchanged makes *every* record a
#: `document` — a silent, total mistyping. `tests/unit/
#: test_import_metadata_provenance.py` pins the two tables together.
_CROSSREF_TYPE_TO_CSL = {
    "journal-article": "article-journal",
    "proceedings-article": "paper-conference",
    "book-chapter": "chapter",
    "book-part": "chapter",
    "book-section": "chapter",
    "reference-entry": "entry-dictionary",
    "book": "book",
    "monograph": "book",
    "edited-book": "book",
    "reference-book": "book",
    "report": "report",
    "report-component": "report",
    "dissertation": "thesis",
    "posted-content": "article",
}

#: CSL fields the converter knows how to map, and the only ones this
#: pipeline passes it. An allowlist rather than a denylist because the
#: converter *preserves* whatever it cannot map by dumping it into the
#: item's `extra` — sensible for hand-written CSL, ruinous for a
#: Crossref API payload, which carries `indexed`, `license`,
#: `content-domain`, `member`, `deposited`, `score`, `assertion`, and a
#: `reference` list that on one ordinary article held 108 entries. All
#: of it would land in `extra` as JSON.
#:
#: `id` and `abstract` are mapped by the converter but deliberately not
#: forwarded: Crossref sets `id` to the DOI, and letting it through
#: writes `Citation Key: 10.1016/…` into `extra`, where Better BibTeX
#: expects a real key; Crossref abstracts arrive as JATS XML and belong
#: to `enrich_abstracts.py`, which strips the markup and has three
#: better sources to try first.
_CSL_FIELDS_KEPT = frozenset({
    "type", "title", "title-short",
    "author", "editor", "translator",
    "issued", "container-title", "collection-title", "collection-number",
    "volume", "issue", "page", "publisher", "publisher-place",
    "edition", "ISBN", "ISSN", "DOI", "URL", "language",
    "number-of-pages", "number", "genre",
})

#: Of those, the ones the converter calls `.strip()` on directly.
#: Crossref returns several as arrays.
_CSL_SCALAR_FIELDS = (
    "title", "title-short", "container-title", "collection-title",
    "publisher", "publisher-place", "ISBN", "ISSN", "DOI", "URL",
    "language",
)


def _csl_for_converter(csl: dict) -> dict:
    """Make one Crossref CSL record safe for `csl_json_to_zotero`.

    Three adjustments, all about Crossref rather than about Zotero:

    - **Only mappable fields are forwarded** (`_CSL_FIELDS_KEPT`).
    - **Arrays where the converter expects a string.** Crossref emits
      `"ISSN": ["0883-9026"]` and the converter calls `.strip()` on it,
      which raises `AttributeError` — that alone would fail the fill for
      every journal article that has an ISSN, i.e. almost all of them.
      The first entry is taken; the rest are the same journal's other
      media forms.
    - **Crossref type names** translated to CSL's, per
      `_CROSSREF_TYPE_TO_CSL`.
    """
    out = {k: v for k, v in csl.items() if k in _CSL_FIELDS_KEPT}
    for field in _CSL_SCALAR_FIELDS:
        value = out.get(field)
        if isinstance(value, list):
            out[field] = next(
                (v for v in value if isinstance(v, str) and v.strip()), "",
            )
        elif value is not None and not isinstance(value, (str, dict, list)):
            out[field] = str(value)
    crossref_type = str(out.get("type") or "").strip().lower()
    if crossref_type in _CROSSREF_TYPE_TO_CSL:
        out["type"] = _CROSSREF_TYPE_TO_CSL[crossref_type]
    return out


def _fetch_csl(
    doi: str, *, session, cache: dict | None = None,
) -> dict | None:
    """Crossref's CSL JSON for one DOI, or None.

    One request per *distinct* DOI: a batch that merges Scopus and WoS
    hits sees most DOIs twice, and a failure is cached as a failure so a
    dead DOI is not retried per row.
    """
    key = doi_utils.doi_cache_key(doi)
    if not key:
        return None
    if cache is not None and key in cache:
        return cache[key]
    csl: dict | None = None
    try:
        data = http_client.get_json(
            session, _CROSSREF_CSL_URL.format(doi=quote(key, safe="/")),
        )
        if isinstance(data, dict) and data.get("type"):
            csl = data
    except Exception:  # noqa: BLE001 — a metadata fetch must not fail an import
        csl = None
    if cache is not None:
        cache[key] = csl
    return csl


def build_item(
    row: dict,
    collection_key: str | None,
    *,
    session=None,
    csl_cache: dict | None = None,
) -> tuple[dict, str]:
    """Turn one CSV row into a Zotero item. Returns `(item, how)`.

    `how` is one of BUILD_SOURCE / BUILD_AUTHORITY / BUILD_FALLBACK —
    see the module docstring for what each path means and why the
    default is the offline one.
    """
    item_type = row_item_type(row)
    if item_type and _has_split_creators(row.get("authors", "")):
        return _row_to_zotero_item(row, collection_key, item_type), BUILD_SOURCE

    csl = (
        _fetch_csl(row.get("doi", ""), session=session, cache=csl_cache)
        if session is not None else None
    )
    if csl is not None:
        try:
            item = csl_json_to_zotero(_csl_for_converter(csl), _item_template)
        except Exception as exc:  # noqa: BLE001 — never fail the whole import
            print(
                f"  WARNING: could not convert Crossref metadata for "
                f"{row.get('doi', '?')} ({exc}); using the search row's own "
                f"fields instead.",
                flush=True,
            )
        else:
            return (
                _apply_plugin_layers(item, row, collection_key),
                BUILD_AUTHORITY,
            )

    return (
        _row_to_zotero_item(
            row, collection_key, item_type or _DEFAULT_ITEM_TYPE,
        ),
        BUILD_FALLBACK,
    )


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
    """Return (doi_map, title_map) for existing items in the library.

    Reads the **cloud** library unconditionally, whatever the run's
    `--remote` setting, because this is a read-your-writes check: this
    script writes through the Zotero Web API, and Zotero Desktop's local
    database does not know about those items until it next syncs. A
    second import run therefore asked the local client, was told the
    library was empty, and created all 31 items again — after which
    PDF enrichment downloaded 62 files and the PRISMA count guard
    failed downstream.

    Local stays the default everywhere else; it is faster and those
    callers are not reading back their own writes.
    """
    if dry_run:
        return {}, {}
    print(
        "Fetching existing library items via the Zotero Web API "
        "(api.zotero.org — this script writes there, so it must read "
        "there too)...",
        flush=True,
    )
    items = zot.cloud_journal_articles()

    doi_map: dict[str, str] = {}
    title_map: dict[str, str] = {}
    for item in items:
        d = item.get("data", {})
        key = d.get("key", item.get("key", ""))
        doi = _normalize_doi_key(d.get("DOI") or "")
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
    # Same backend as `_fetch_existing_items`, for the same reason —
    # plus these patches carry an item `version`, and a stale local one
    # is rejected by the API's If-Unmodified-Since-Version check.
    print(f"\nReading {len(to_add)} existing items from the Zotero Web API...",
          flush=True)
    all_items = zot.cloud_journal_articles()
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


# Universal item-object keys that aren't part of a type's field schema
# (zotero_mcp.schema.valid_fields() only covers scalar metadata fields like
# title/DOI/date) — always kept regardless of itemType.
_ITEM_STRUCTURAL_KEYS = frozenset({"itemType", "creators", "tags", "collections", "relations"})


def _filter_valid_fields(item: dict) -> tuple[dict, list[str]]:
    """Drop fields the item's itemType schema doesn't recognize.

    The Zotero write API fails the WHOLE batch on one invalid field key
    with an opaque error, and `_create_new_items` posts up to 50 items per
    call. `zotero_mcp.schema.valid_fields` is stdlib-only (on-disk cache or
    a vendored floor, no network), so filtering per item here is cheap and
    turns a batch-wide 400 into a per-row warning instead.

    Returns ``(filtered_item, rejected_field_names)``.
    """
    valid = valid_fields(item.get("itemType", ""))
    filtered: dict = {}
    rejected: list[str] = []
    for key, value in item.items():
        if key in _ITEM_STRUCTURAL_KEYS or key in valid:
            filtered[key] = value
        else:
            rejected.append(key)
    return filtered, rejected


def _create_new_items(
    to_create: list[dict],
    zot: zotero_io.ZoteroClient,
) -> tuple[int, int, list[str]]:
    filtered_to_create = []
    for item in to_create:
        filtered, rejected = _filter_valid_fields(item)
        if rejected:
            label = item.get("title") or "(no title)"
            print(f"  WARNING: dropping unrecognized field(s) {rejected} "
                  f"from '{label}' (itemType={item.get('itemType', '?')})",
                  flush=True)
        filtered_to_create.append(filtered)
    to_create = filtered_to_create

    base_url = zot.api_base_url()
    headers = {
        "Zotero-API-Key": zot.api_key,
        "Zotero-API-Version": "3",
        "Content-Type": "application/json",
    }
    created = failed = 0
    created_keys: list[str] = []
    n_batches = (len(to_create) + BATCH_SIZE - 1) // BATCH_SIZE
    # Zotero asks clients to honour `Backoff` / `Retry-After` and returns
    # 429 under load. A bare `requests.post` honours neither, so a single
    # throttled batch used to abort the whole import; the shared session
    # retries 429/5xx with exponential backoff instead. Retrying this POST
    # is safe: Zotero rejects a throttled write outright rather than
    # applying it, and the per-item `success`/`failed` maps below are what
    # decide the outcome either way.
    session = http_client.build_session()
    for batch_num, i in enumerate(range(0, len(to_create), BATCH_SIZE), 1):
        batch = to_create[i:i + BATCH_SIZE]
        print(f"  batch {batch_num}/{n_batches} ({len(batch)} items)...", flush=True)
        resp = session.post(
            f"{base_url}/items", headers=headers, json=batch, timeout=60,
        )
        resp.raise_for_status()
        result = resp.json()
        success = result.get("success", {})
        created += len(success)
        created_keys.extend(str(v) for v in success.values())
        failed += len(result.get("failed", {}))
        if result.get("failed"):
            for idx, err in result["failed"].items():
                print(f"  FAILED item {idx}: {err}", flush=True)
        time.sleep(0.5)
    return created, failed, created_keys


def _format_item_preview(item: dict) -> list[str]:
    """A few lines describing one item, for `--dry-run`.

    Counts alone cannot answer the question a dry run is asked: does
    this import produce *correct records*? Creators are where it goes
    wrong invisibly — a single-field `name` blob and a properly split
    creator both look like "1 item to create" in a summary.
    """
    creators = item.get("creators") or []
    shown = []
    for c in creators[:3]:
        if c.get("name"):
            shown.append(f"{c['name']} (single field)")
        else:
            shown.append(f"{c.get('lastName', '')}, {c.get('firstName', '')}")
    if len(creators) > 3:
        shown.append(f"… +{len(creators) - 3} more")
    container = _container_field_of(item)
    lines = [
        f"    title    : {(item.get('title') or '')[:70]}",
        f"    itemType : {item.get('itemType', '?')}",
        f"    creators : {'; '.join(shown) or '(none)'}",
        f"    volume/issue/pages: {item.get('volume', '') or '—'} / "
        f"{item.get('issue', '') or '—'} / {item.get('pages', '') or '—'}",
    ]
    if container:
        lines.append(f"    {container}: {item.get(container, '') or '—'}")
    tags = [t.get("tag", "") for t in (item.get("tags") or [])]
    if tags:
        lines.append(f"    tags     : {', '.join(tags)}")
    return lines


def _print_dry_run_preview(samples: dict[str, dict]) -> None:
    """One example per build path, so both can be eyeballed."""
    labels = {
        BUILD_SOURCE: "source-built (from the search row, no network)",
        BUILD_AUTHORITY: "authority-filled (from Crossref via zotero-mcp)",
        BUILD_FALLBACK: "fallback (row as-is; the authority was unavailable)",
    }
    for kind, label in labels.items():
        item = samples.get(kind)
        if not item:
            continue
        print(f"\n  Example {label}:", flush=True)
        for line in _format_item_preview(item):
            print(line, flush=True)


def _print_provenance_summary(counts: dict[str, int], no_doi: int) -> None:
    """Say where the new items' metadata came from.

    Worth a line each run: a corpus that is mostly authority-filled is
    telling you the search stage is running without Scopus or WoS, which
    costs one HTTP request per record and is usually not what was
    intended.
    """
    if not any(counts.values()):
        return
    print("  Metadata provenance:", flush=True)
    print(f"    from the search databases: {counts[BUILD_SOURCE]}", flush=True)
    print(f"    filled from Crossref:      {counts[BUILD_AUTHORITY]}",
          flush=True)
    if counts[BUILD_FALLBACK]:
        why = f" ({no_doi} of them with no DOI to look up)" if no_doi else ""
        print(
            f"    incomplete, row as-is:     {counts[BUILD_FALLBACK]}{why}",
            flush=True,
        )


def _resolve_collection(zot: zotero_io.ZoteroClient, args) -> str:
    """`--collection` as a key, resolving a name and creating on miss.

    Prints which path was taken. A name silently treated as a key is
    what produced a 400 on every item of a 31-row batch, so this is
    loud on purpose.
    """
    wanted = (args.collection or "").strip()
    if not wanted:
        return ""
    if args.dry_run:
        # No credential to list collections with, and nothing will be
        # written anyway. Say what would happen rather than pretending.
        print(f"  Collection: {wanted!r} (resolved at write time; "
              f"--dry-run does not create anything)", flush=True)
        return wanted
    try:
        key, how = zot.find_collection(
            wanted, create=not args.no_create_collection,
        )
    except ValueError as exc:
        sys.exit(f"ERROR: {exc}")
    label = {
        "key": f"  Collection: {key} (matched by key)",
        "name": f"  Collection: {key} (matched by name {wanted!r})",
        "created": f"  Collection: {key} — CREATED, named {wanted!r}",
    }[how]
    print(label, flush=True)
    return key


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    zotero_io.add_library_args(parser)
    parser.add_argument("--collection",
                        default=os.environ.get("ZOTERO_SLR_COLL", ""),
                        help="Collection to add items into — an 8-character "
                             "collection KEY or a display NAME. A name that "
                             "does not exist yet is created (pass "
                             "--no-create-collection to make that an error "
                             "instead). Default: $ZOTERO_SLR_COLL, optional.")
    parser.add_argument("--no-create-collection", action="store_true",
                        help="Fail if --collection names a collection that "
                             "does not exist, instead of creating it.")
    parser.add_argument("--input", required=True,
                        help="Path to deduplicated search-results CSV.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and report without writing to Zotero.")
    parser.add_argument(
        "--created-keys-out",
        help="Write the Zotero keys of newly CREATED items (one per line) "
             "to this path — the create-batch success map, not patched "
             "pre-existing items. Lets a caller (e.g. a test harness) "
             "delete exactly what this run created, never anything it "
             "merely touched.",
    )
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

    collection_key = _resolve_collection(zot, args)

    doi_map, title_map = _fetch_existing_items(zot, args.dry_run)

    to_add: list[tuple[str, str]] = []
    to_create: list[dict] = []
    batch_doi_seen: dict[str, int] = {}
    batch_title_seen: dict[str, int] = {}
    dropped_within_batch = 0
    #: One Crossref fetch per distinct DOI, not per row — a batch that
    #: merges Scopus and WoS hits sees most DOIs twice. Populated under
    #: `--dry-run` too, so the preview shows the items that would
    #: actually be written rather than an optimistic sketch of them.
    csl_cache: dict[str, dict | None] = {}
    csl_session = http_client.build_session()
    build_counts: dict[str, int] = {
        BUILD_SOURCE: 0, BUILD_AUTHORITY: 0, BUILD_FALLBACK: 0,
    }
    preview_samples: dict[str, dict] = {}
    fallback_no_doi = 0

    for row in rows:
        doi = _normalize_doi_key(row.get("doi") or "")
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

        item, how = build_item(
            row, collection_key or None,
            session=csl_session, csl_cache=csl_cache,
        )
        build_counts[how] += 1
        preview_samples.setdefault(how, item)
        if how == BUILD_FALLBACK and not doi:
            fallback_no_doi += 1
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
    _print_provenance_summary(build_counts, fallback_no_doi)

    if args.dry_run:
        _print_dry_run_preview(preview_samples)
        print("\n[DRY RUN] No changes written.", flush=True)
        return 0

    _patch_existing_items(to_add, zot, collection_key or None)

    created = 0
    created_keys: list[str] = []
    if to_create:
        print(f"\nCreating {len(to_create)} new items...", flush=True)
        created, failed, created_keys = _create_new_items(to_create, zot)
        print(f"  Created: {created}  Failed: {failed}", flush=True)

    if args.created_keys_out:
        Path(args.created_keys_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.created_keys_out).write_text(
            "\n".join(created_keys) + ("\n" if created_keys else ""),
            encoding="utf-8",
        )

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
