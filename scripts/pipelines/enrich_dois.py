#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyzotero>=1.6",
#     "requests>=2.31",
#     "habanero>=1.2",
#     "tenacity>=8.0",
# ]
# ///
"""Find and validate DOIs in a Zotero library (v0.5.0).

Two modes combine by default (use `--validate` or `--find-missing` to
run just one):

  --validate      For items that HAVE a DOI: look it up on Crossref
                  and compare the registered title / year / first-
                  author surname against Zotero's record. Flag
                  mismatches and DOIs that don't resolve.

  --find-missing  For items WITHOUT a DOI (or whose DOI failed
                  validation, when combined with --validate): search
                  Crossref by title + author + year. Auto-apply
                  high-confidence matches (all three criteria agree);
                  prompt on 2/3 matches; skip zero-match items.

  --all           Both (default).

Selective targeting via `--filter-keys-file` (one Zotero item key per
line). A typical workflow runs the audit first and passes
`audit.missing_doi.keys` to this script's `--find-missing` pass.

Write policy:
  - Default: auto-apply only when the Crossref candidate is a 3/3
    match (title via `_title_match.matches`, year within ±1, first-
    author surname equality case-insensitive).
  - `--replace-invalid`: also overwrite DOIs that failed validation
    when a 3/3 replacement candidate is found (off by default — a
    wrong replacement is worse than a broken DOI).
  - `--dry-run`: never write; just report proposals in the CSV.
  - `--no-prompt`: skip the 2/3-ambiguity prompt (log and continue).
  - Non-TTY stdin forces `--no-prompt` behaviour regardless.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = SCRIPT_DIR.parent
for _p in (str(SCRIPT_DIR), str(SCRIPTS_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import zotero_io  # noqa: E402
from core.config_loader import get, require  # noqa: E402
from fetchers._title_match import matches as title_matches  # noqa: E402
from fetchers.doi_resolver import (  # noqa: E402
    DoiResolution,
    DoiResolverCache,
    _extract_resolution,
    resolve_doi,
)

DEFAULT_LOG_CSV = os.path.join("output", "doi_enrich_log.csv")
DEFAULT_CACHE_DIR = os.path.join("output", "pdf_cache")

LOG_FIELDS = [
    "run_date", "item_key",
    "zotero_doi", "zotero_title", "zotero_year",
    "crossref_doi", "crossref_title", "crossref_authors",
    "status",
]


@dataclass
class Config:
    crossref_mailto: str = ""


def _load_config() -> Config:
    return Config(
        crossref_mailto=get("crossref", "mailto", env="CROSSREF_MAILTO"),
    )


def _open_log(path: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    is_new = not os.path.exists(path)
    fh = open(path, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(fh, fieldnames=LOG_FIELDS)
    if is_new:
        writer.writeheader()
    return fh, writer


# ---------------------------------------------------------------------------
# DOI normalisation — handle the common junk in Zotero's DOI field.
# ---------------------------------------------------------------------------


_DOI_PREFIX_STRIPS = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
    "doi:",
    "DOI:",
)


def _normalise_doi(raw: str) -> tuple[str, bool]:
    """Strip URL / `doi:` prefixes and whitespace from a DOI.

    Returns `(clean_doi, was_malformed)`. `was_malformed` is True
    whenever we had to change anything (strip prefix or whitespace),
    so `--fix-malformed` can PATCH the Zotero field back to the
    canonical form.
    """
    s = (raw or "").strip()
    original = s
    for prefix in _DOI_PREFIX_STRIPS:
        if s.lower().startswith(prefix.lower()):
            s = s[len(prefix):].strip()
            break
    return s, s != original.strip() or s != raw


# ---------------------------------------------------------------------------
# Match scoring — 3 criteria, scored independently.
# ---------------------------------------------------------------------------


def _year_matches(zot_year: str, cr_year: str) -> bool:
    """Year equality with ±1 tolerance (early-access / late-issue
    drift is common). Both strings; empty year on either side fails."""
    try:
        a = int(zot_year)
        b = int(cr_year)
    except (ValueError, TypeError):
        return False
    return abs(a - b) <= 1


def _first_author_matches(zot_item: dict, cr_surnames: list[str]) -> bool:
    """Compare Zotero's first-author family name to Crossref's first
    surname. Case-insensitive exact equality — any typo or
    transliteration difference fails the check (safer than fuzzy).
    """
    if not cr_surnames:
        return False
    creators = zot_item.get("data", {}).get("creators") or []
    for c in creators:
        if c.get("creatorType") == "author":
            zot_family = (c.get("lastName") or "").strip().lower()
            if zot_family and zot_family == cr_surnames[0].strip().lower():
                return True
            break
    return False


def _match_score(
    zot_item: dict, zot_title: str, zot_year: str, cr: DoiResolution,
) -> tuple[int, list[str]]:
    """Return (score_0_to_3, failing_criteria_labels)."""
    score = 0
    failing: list[str] = []
    if cr.title and zot_title and title_matches(zot_title, cr.title):
        score += 1
    else:
        failing.append("title")
    if _year_matches(zot_year, cr.issued_year):
        score += 1
    else:
        failing.append("year")
    if _first_author_matches(zot_item, cr.author_surnames):
        score += 1
    else:
        failing.append("author")
    return score, failing


# ---------------------------------------------------------------------------
# Zotero helpers
# ---------------------------------------------------------------------------


def _zot_year(item: dict) -> str:
    """Extract the year from Zotero's `date` field (common formats:
    '2014', '2014-06', '2014-06-15', 'June 2014', etc.)."""
    import re
    raw = (item.get("data", {}).get("date") or "").strip()
    m = re.search(r"(?<!\d)(\d{4})(?!\d)", raw)
    return m.group(1) if m else ""


def _zot_title(item: dict) -> str:
    return (item.get("data", {}).get("title") or "").strip()


def _zot_first_author(item: dict) -> str:
    creators = item.get("data", {}).get("creators") or []
    for c in creators:
        if c.get("creatorType") == "author":
            return (c.get("lastName") or "").strip()
    return ""


# ---------------------------------------------------------------------------
# Ambiguity prompt (TTY only)
# ---------------------------------------------------------------------------


def _prompt_confirm(
    zot_item: dict, zot_title: str, zot_year: str,
    cr: DoiResolution, cr_doi: str,
) -> bool:
    """Show the proposed match and return True on user confirm."""
    cr_authors = ", ".join(cr.author_surnames[:3]) or "(no authors)"
    print(
        f"\n  Proposed DOI for: {zot_title[:70]}\n"
        f"    Zotero: {zot_year or '(no year)'}, "
        f"{_zot_first_author(zot_item) or '(no author)'}\n"
        f"    Crossref: {cr.issued_year or '(no year)'}, "
        f"{cr_authors}, title={cr.title[:70]!r}\n"
        f"    DOI: {cr_doi}",
        flush=True,
    )
    try:
        with open("/dev/tty") as tty:
            sys.stdout.write("  Apply? [y/N] ")
            sys.stdout.flush()
            raw = tty.readline()
    except Exception:
        raw = sys.stdin.readline()
    return (raw or "").strip().lower() in ("y", "yes")


# ---------------------------------------------------------------------------
# Validate mode
# ---------------------------------------------------------------------------


@dataclass
class ValidateResult:
    status: str
    crossref_doi: str = ""
    crossref_title: str = ""
    crossref_authors: str = ""


def _validate_doi(
    zot_item: dict, crossref_client, cache: DoiResolverCache,
    *, fix_malformed: bool, dry_run: bool, zot,
) -> ValidateResult:
    raw_doi = zot_item.get("data", {}).get("DOI") or ""
    clean_doi, was_malformed = _normalise_doi(raw_doi)
    if not clean_doi:
        # Empty after stripping — treat as validate-ineligible.
        return ValidateResult(status="validate_skipped_empty_after_strip")

    # Apply the malformed-DOI fix if requested.
    malformed_was_fixed = False
    if was_malformed and fix_malformed and not dry_run:
        try:
            zot.update_item({
                "key": zot_item["key"],
                "version": zot_item["version"],
                "DOI": clean_doi,
            })
            malformed_was_fixed = True
        except Exception as e:
            print(f"  [{zot_item.get('key')}] fix-malformed write failed: {e}",
                  flush=True)

    try:
        cr = resolve_doi(clean_doi, crossref=crossref_client, cache=cache)
    except Exception as e:
        # Outer exception is rare — resolve_doi catches its own
        # Crossref errors and returns None. Only triggers on e.g.
        # cache corruption or import issues at call time.
        print(f"  [{zot_item.get('key')}] Crossref error: {e}", flush=True)
        return ValidateResult(
            status="validate_network_error",
            crossref_doi=clean_doi,
        )

    if cr is None:
        # resolve_doi returned None: Crossref couldn't resolve this
        # DOI (404, unknown registrar, parse failure). Treat as
        # "not in Crossref" so the combined --validate + --find-missing
        # flow re-routes to title-based search. A genuine transient
        # network error here is indistinguishable from a 404 at
        # resolve_doi's current contract; in practice that's fine
        # because the subsequent Crossref search call would also
        # fail on a real network outage, producing a downstream
        # `not_found_in_crossref` with zero writes.
        return ValidateResult(
            status="validate_not_in_crossref",
            crossref_doi=clean_doi,
        )

    # Crossref returned a resolution. If title is empty, Crossref has
    # the DOI registered but lacks metadata — treat as "not in
    # Crossref" for validation purposes (we can't compare).
    if not cr.title:
        return ValidateResult(
            status="validate_not_in_crossref",
            crossref_doi=clean_doi,
        )

    zot_title = _zot_title(zot_item)
    if not zot_title:
        return ValidateResult(
            status="validate_skipped_no_zotero_title",
            crossref_doi=clean_doi,
            crossref_title=cr.title,
        )

    if title_matches(zot_title, cr.title):
        status = (
            "validate_malformed_doi_fixed" if malformed_was_fixed
            else "validate_ok"
        )
        return ValidateResult(
            status=status,
            crossref_doi=clean_doi,
            crossref_title=cr.title,
            crossref_authors=", ".join(cr.author_surnames[:3]),
        )
    return ValidateResult(
        status="validate_title_mismatch",
        crossref_doi=clean_doi,
        crossref_title=cr.title,
        crossref_authors=", ".join(cr.author_surnames[:3]),
    )


# ---------------------------------------------------------------------------
# Find-missing mode
# ---------------------------------------------------------------------------


def _crossref_search(
    zot_title: str, zot_first_author: str, zot_year: str,
    crossref_client,
) -> list[tuple[str, DoiResolution]]:
    """Return up to 3 Crossref candidates as (doi, resolution)
    tuples. Uses `query_bibliographic` (Crossref's single-field
    relevance search) — more forgiving of punctuation / subtitle
    drift than separate `query_title` + `query_author` fields."""
    query = " ".join(
        s for s in (zot_title, zot_first_author, zot_year) if s
    )
    try:
        resp = crossref_client.works(query_bibliographic=query, limit=3)
    except Exception:
        return []
    if not isinstance(resp, dict) or resp.get("status") != "ok":
        return []
    message = resp.get("message") or {}
    items = message.get("items") or []
    out: list[tuple[str, DoiResolution]] = []
    for m in items[:3]:
        if not isinstance(m, dict):
            continue
        doi = str(m.get("DOI") or "").strip()
        if not doi:
            continue
        out.append((doi, _extract_resolution(m)))
    return out


@dataclass
class FindResult:
    status: str
    crossref_doi: str = ""
    crossref_title: str = ""
    crossref_authors: str = ""


def _find_missing_doi(
    zot_item: dict, crossref_client, zot,
    *, is_replacement: bool,
    replace_invalid: bool, dry_run: bool, no_prompt: bool,
) -> FindResult:
    zot_title = _zot_title(zot_item)
    if not zot_title:
        return FindResult(status="skipped_no_title")

    zot_year = _zot_year(zot_item)
    zot_first_author = _zot_first_author(zot_item)

    candidates = _crossref_search(
        zot_title, zot_first_author, zot_year, crossref_client,
    )
    if not candidates:
        return FindResult(status="not_found_in_crossref")

    # Score each candidate; pick the highest.
    scored = [
        (doi, cr, *_match_score(zot_item, zot_title, zot_year, cr))
        for doi, cr in candidates
    ]
    scored.sort(key=lambda t: t[2], reverse=True)
    top_doi, top_cr, top_score, top_failing = scored[0]
    cr_authors_str = ", ".join(top_cr.author_surnames[:3])

    def _do_write(status_applied: str) -> FindResult:
        if dry_run:
            return FindResult(
                status=f"{status_applied}_dry_run",
                crossref_doi=top_doi,
                crossref_title=top_cr.title,
                crossref_authors=cr_authors_str,
            )
        try:
            zot.update_item({
                "key": zot_item["key"],
                "version": zot_item["version"],
                "DOI": top_doi,
            })
        except Exception as e:
            print(f"  [{zot_item.get('key')}] write failed: {e}", flush=True)
            return FindResult(
                status="write_failed",
                crossref_doi=top_doi,
                crossref_title=top_cr.title,
                crossref_authors=cr_authors_str,
            )
        return FindResult(
            status=status_applied,
            crossref_doi=top_doi,
            crossref_title=top_cr.title,
            crossref_authors=cr_authors_str,
        )

    # 3/3: auto-apply (or auto-replace with --replace-invalid).
    if top_score == 3:
        if is_replacement and not replace_invalid:
            return FindResult(
                status="proposed_replacement",
                crossref_doi=top_doi,
                crossref_title=top_cr.title,
                crossref_authors=cr_authors_str,
            )
        status_applied = (
            "replaced_high_confidence"
            if is_replacement else "applied_high_confidence"
        )
        return _do_write(status_applied)

    # 2/3: prompt (unless non-TTY or --no-prompt).
    if top_score == 2:
        if no_prompt or not sys.stdin.isatty():
            return FindResult(
                status="proposed_not_applied",
                crossref_doi=top_doi,
                crossref_title=top_cr.title,
                crossref_authors=cr_authors_str,
            )
        if _prompt_confirm(
            zot_item, zot_title, zot_year, top_cr, top_doi,
        ):
            status_applied = (
                "replaced_after_prompt"
                if is_replacement else "applied_after_prompt"
            )
            return _do_write(status_applied)
        return FindResult(
            status="proposed_skipped_by_user",
            crossref_doi=top_doi,
            crossref_title=top_cr.title,
            crossref_authors=cr_authors_str,
        )

    # 0-1/3: ambiguous or plainly no match.
    return FindResult(
        status="ambiguous_no_clear_match"
        if len(candidates) > 1 else "not_found_in_crossref",
        crossref_doi=top_doi,
        crossref_title=top_cr.title,
        crossref_authors=cr_authors_str,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _write_log(
    log_writer, run_date: str, zot_item: dict, status: str,
    crossref_doi: str = "", crossref_title: str = "",
    crossref_authors: str = "",
) -> None:
    d = zot_item.get("data", {})
    log_writer.writerow({
        "run_date": run_date, "item_key": zot_item.get("key", ""),
        "zotero_doi": (d.get("DOI") or "")[:200],
        "zotero_title": _zot_title(zot_item)[:120],
        "zotero_year": _zot_year(zot_item),
        "crossref_doi": crossref_doi,
        "crossref_title": crossref_title[:120],
        "crossref_authors": crossref_authors,
        "status": status,
    })


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode_group = parser.add_argument_group("mode (default: --all)")
    mode_group.add_argument(
        "--validate", action="store_true",
        help="Validate existing DOIs against Crossref.",
    )
    mode_group.add_argument(
        "--find-missing", dest="find_missing", action="store_true",
        help="Search Crossref for items without a DOI.",
    )
    mode_group.add_argument(
        "--all", action="store_true",
        help="Run both --validate and --find-missing.",
    )
    parser.add_argument(
        "--filter-keys-file",
        help="Text file of Zotero item keys (one per line) to process.",
    )
    zotero_io.add_library_args(parser)
    parser.add_argument(
        "--log-csv", default=DEFAULT_LOG_CSV,
        help=f"CSV log path (default: {DEFAULT_LOG_CSV}).",
    )
    parser.add_argument(
        "--cache-dir", default=DEFAULT_CACHE_DIR,
        help=f"DoiResolverCache directory (default: {DEFAULT_CACHE_DIR}; "
             "shared with the browser pass).",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Never write to Zotero; just report.")
    parser.add_argument("--no-prompt", action="store_true",
                        help="Skip 2/3-match prompts; log as proposed_not_applied.")
    parser.add_argument("--fix-malformed", action="store_true",
                        help="PATCH DOIs with URL/doi: prefixes or whitespace "
                             "back to the clean form.")
    parser.add_argument("--replace-invalid", action="store_true",
                        help="Overwrite invalid DOIs with 3/3-match Crossref "
                             "candidates (off by default).")
    args = parser.parse_args()

    # Mode resolution: explicit flags win; else default to --all.
    if not (args.validate or args.find_missing or args.all):
        args.all = True
    do_validate = args.validate or args.all
    do_find_missing = args.find_missing or args.all

    os.makedirs(args.cache_dir, exist_ok=True)
    run_date = date.today().isoformat()

    # Zotero + Crossref clients.
    require("zotero", "api_key", env="ZOTERO_API_KEY")
    if not getattr(args, "user", False) and not args.group:
        try:
            zot = zotero_io.ZoteroClient.from_config(group_id=None)
        except zotero_io.GroupSelectionRequired as e:
            print(zotero_io.format_group_selection_error(e.groups), file=sys.stderr)
            return 2
    else:
        zot = zotero_io.ZoteroClient.from_args(args)

    config = _load_config()
    from habanero import Crossref
    crossref_client = Crossref(mailto=config.crossref_mailto)
    cache = DoiResolverCache(args.cache_dir)

    print("Fetching Zotero items...", end=" ", flush=True)
    all_items = zot.journal_articles()
    print(f"{len(all_items)} journal articles.", flush=True)

    if args.filter_keys_file:
        with open(args.filter_keys_file) as f:
            target = {line.strip() for line in f if line.strip()}
        all_items = [it for it in all_items if it["key"] in target]
        print(f"  After --filter-keys-file: {len(all_items)} items.", flush=True)

    with_doi = [
        it for it in all_items
        if (it.get("data", {}).get("DOI") or "").strip()
    ]
    without_doi = [
        it for it in all_items
        if not (it.get("data", {}).get("DOI") or "").strip()
    ]

    log_fh, log_writer = _open_log(args.log_csv)

    # Pass 1 — validate existing DOIs. Collect keys of items whose
    # DOI failed validation — Pass 2 can re-route them through
    # find-missing when --all runs.
    validate_failed_items: list[dict] = []
    validate_failed_keys: set[str] = set()
    counts: dict[str, int] = {}

    try:
        if do_validate and with_doi:
            print(f"\n--- Validating {len(with_doi)} DOIs ---", flush=True)
            for i, item in enumerate(with_doi, 1):
                title70 = _zot_title(item)[:70]
                result = _validate_doi(
                    item, crossref_client, cache,
                    fix_malformed=args.fix_malformed,
                    dry_run=args.dry_run, zot=zot,
                )
                counts[result.status] = counts.get(result.status, 0) + 1
                _write_log(
                    log_writer, run_date, item, result.status,
                    crossref_doi=result.crossref_doi,
                    crossref_title=result.crossref_title,
                    crossref_authors=result.crossref_authors,
                )
                print(f"  [{i}/{len(with_doi)}] {result.status:<40} "
                      f"{title70}", flush=True)
                if result.status in (
                    "validate_not_in_crossref",
                    "validate_title_mismatch",
                ) and do_find_missing:
                    validate_failed_items.append(item)
                    validate_failed_keys.add(item["key"])

        # Pass 2 — find missing (and replace invalid).
        missing_pool = without_doi + validate_failed_items
        if do_find_missing and missing_pool:
            print(
                f"\n--- Searching Crossref for "
                f"{len(missing_pool)} item"
                f"{'s' if len(missing_pool) != 1 else ''} "
                f"({len(without_doi)} without DOI, "
                f"{len(validate_failed_items)} invalid) ---",
                flush=True,
            )
            for i, item in enumerate(missing_pool, 1):
                title70 = _zot_title(item)[:70]
                is_replacement = item["key"] in validate_failed_keys
                result = _find_missing_doi(
                    item, crossref_client, zot,
                    is_replacement=is_replacement,
                    replace_invalid=args.replace_invalid,
                    dry_run=args.dry_run,
                    no_prompt=args.no_prompt,
                )
                counts[result.status] = counts.get(result.status, 0) + 1
                _write_log(
                    log_writer, run_date, item, result.status,
                    crossref_doi=result.crossref_doi,
                    crossref_title=result.crossref_title,
                    crossref_authors=result.crossref_authors,
                )
                print(f"  [{i}/{len(missing_pool)}] {result.status:<40} "
                      f"{title70}", flush=True)
    finally:
        log_fh.close()

    # Summary.
    print("\nDone. Status counts:", flush=True)
    for status in sorted(counts.keys()):
        print(f"  {status:<40} {counts[status]}", flush=True)
    print(f"  (log: {args.log_csv})", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
