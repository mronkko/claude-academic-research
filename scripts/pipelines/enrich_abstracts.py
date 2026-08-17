#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyzotero>=1.6",
#     "requests>=2.31",
#     "urllib3>=2.0",
#     "tenacity>=8.0",
#     "habanero>=1.2",
#     "pyalex>=0.15",
#     "pybliometrics>=3.6",
# ]
# ///
"""Enrich Zotero items by fetching missing abstracts.

For each journal article in the library that does not have an
`abstractNote`, run the abstract-source cascade
(see `fetchers.abstract_sources`) until one source returns text, then
patch the Zotero item via `ZoteroClient.update_abstract` (pyzotero's
`update_item`).

The fetcher priority matches `fetchers.abstract_sources`:
    Crossref → Semantic Scholar → Scopus → WoS → ScienceDirect
    → OpenAlex GROBID

--sources filters to a subset, same as enrich_pdfs.py.

Log statuses (`output/abstract_fetch_log.csv`):
    updated        abstract fetched and written to Zotero
    dry_run        abstract fetched, Zotero not touched (--dry-run)
    update_failed  abstract fetched, the Zotero write raised
    not_found      every source answered and none had an abstract —
                   the article genuinely has none
    lookup_failed  at least one source raised, so absence was never
                   established; the abstract is unknown, not absent
    no_doi         no DOI on the item, so the cascade never ran

`not_found` and `lookup_failed` used to share the `not_found` label,
which made "this corpus has N abstract-less records" impossible to
compute from the log — a real requirement for callers that report
missingness as a finding. `detail` carries the per-source reason.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = SCRIPT_DIR.parent
for _p in (str(SCRIPT_DIR), str(SCRIPTS_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fetchers  # noqa: E402
import http_client  # noqa: E402
import shared_orchestrators  # noqa: E402
import zotero_io  # noqa: E402
from core.config_loader import get, require  # noqa: E402
from log_schemas import ABSTRACT_FETCH_FIELDS  # noqa: E402

DEFAULT_LOG_CSV = os.path.join("output", "abstract_fetch_log.csv")
DEFAULT_CACHE_DIR = os.path.join("output", "fulltext_cache")

LOG_FIELDS = ABSTRACT_FETCH_FIELDS


@dataclass
class Config:
    elsevier_api_key: str = ""
    openalex_api_key: str = ""
    semantic_scholar_api_key: str = ""
    wos_api_key_extended: str = ""
    wos_api_key: str = ""
    crossref_mailto: str = ""
    #: Opt-in for the paid OpenAlex Content API. Carried here as well as
    #: in enrich_pdfs.py's Config because OpenAlex's abstract route also
    #: goes through the paid Content API (GROBID TEI XML) — a user who
    #: turns the paid tier off means it everywhere, not just for PDFs.
    openalex_use_paid_content_api: bool = True


def _load_config() -> Config:
    return Config(
        elsevier_api_key=get("elsevier", "api_key", env="ELSEVIER_API_KEY"),
        openalex_api_key=get("openalex", "api_key", env="OPENALEX_API_KEY"),
        openalex_use_paid_content_api=fetchers.openalex.coerce_paid_opt_in(
            get(
                "openalex", "use_paid_content_api",
                env="OPENALEX_USE_PAID_CONTENT_API",
            ),
            default=True,
        ),
        semantic_scholar_api_key=get(
            "semantic_scholar", "api_key", env="SEMANTIC_SCHOLAR_API_KEY",
        ),
        wos_api_key_extended=get("wos", "expanded_key", env="WOS_API_KEY_EXTENDED"),
        wos_api_key=get("wos", "starter_key", env="WOS_API_KEY"),
        crossref_mailto=get("crossref", "mailto", env="CROSSREF_MAILTO"),
    )


def _open_log(path: str):
    return shared_orchestrators.open_log(path, LOG_FIELDS)


def _already_done(log_path: str) -> set[str]:
    """Zotero item keys this log records as already enriched.

    **Keyed on the item, not the DOI.** It used to key on `doi`, which
    made a successful update permanently suppress every *other* copy of
    the same article. Libraries built by a systematic-review import
    routinely hold several: one real library had 229 duplicate-DOI
    groups, ~298 extra items. Fill one copy, and the other two could
    never be filled again — the abstract was in the library and
    structurally invisible to any consumer that picks one item per DOI.

    Nothing was gained by keying on the DOI in the first place. An item
    whose update succeeded now *has* an `abstractNote`, so it already
    fails the emptiness test in `main()` on the next run; the DOI key
    could only ever exclude siblings. Keying on `item_key` keeps the
    resume guard (which matters when a Zotero read is served from a
    desktop copy that has not synced the write yet) and drops the
    collateral damage.
    """
    return shared_orchestrators.load_done_keys(
        log_path, statuses="updated", key_field="item_key",
    )


@dataclass
class CascadeResult:
    """Outcome of one item's trip through the abstract cascade.

    `abstract`/`source` are set only on a hit. When there is no hit,
    `confirmed_absent` distinguishes the two cases that matter
    downstream: every source answered cleanly and none had an abstract
    (True — the article genuinely has none), versus at least one source
    raised so the question was never actually answered (False — missing
    data). `errors` holds `(source_name, message)` for each failure and
    `asked` names the sources that answered cleanly.
    """

    abstract: str = ""
    source: str = ""
    asked: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return bool(self.abstract)

    @property
    def confirmed_absent(self) -> bool:
        return not self.found and not self.errors and bool(self.asked)

    def detail(self) -> str:
        """One-line human-readable summary for the log's `detail` column."""
        if self.errors:
            return "; ".join(f"{name}: {msg}" for name, msg in self.errors)
        if self.asked:
            return "no abstract at: " + ",".join(self.asked)
        return ""


def _try_cascade(
    item: dict,
    sources: list,
    cache_dir: str,
) -> CascadeResult:
    """Try each abstract fetcher in priority order.

    Returns a `CascadeResult`; `.found` is True on the first source that
    returns text. A source that raises is recorded in `.errors` rather
    than being silently skipped, so a run that failed to answer is
    distinguishable from one that answered "no abstract exists".
    Sources raising `NotImplementedError` are not counted either way —
    that means the fetcher does not offer abstracts at all.
    """
    result = CascadeResult()
    data = item.get("data", {})
    doi = (data.get("DOI") or "").strip()
    if not doi:
        return result
    title = (data.get("title") or "").strip()
    for src in sources:
        try:
            text = src.fetch_abstract(doi, title=title or None, cache_dir=cache_dir)
        except NotImplementedError:
            continue
        except Exception as e:
            print(f"    {src.name}: {e}", flush=True)
            result.errors.append((src.name, f"{type(e).__name__}: {e}"))
            continue
        result.asked.append(src.name)
        if text:
            result.abstract = text
            result.source = src.name
            return result
    return result


def group_by_doi(items: list[dict]) -> dict[str, list[dict]]:
    """Group items by normalised DOI, preserving encounter order.

    One lookup per DOI, applied to every copy that carries it. The
    cascade is the expensive part of a run and its answer depends only
    on the DOI, so without this the duplicate records that a systematic-
    review import leaves behind pay for the same abstract N times over.

    Items with no DOI are dropped — they have nothing to look up, and
    `main()`'s `missing` filter has already excluded them, which is what
    makes the grouping total there. The normalisation matches
    `load_done_keys`: strip and lower-case, no prefix handling, so the
    grouping key and the resume key cannot disagree about identity.
    """
    groups: dict[str, list[dict]] = {}
    for it in items:
        doi = (it.get("data", {}).get("DOI") or "").strip().lower()
        if not doi:
            continue
        groups.setdefault(doi, []).append(it)
    return groups


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sources", default="",
        help="Comma-separated fetcher names. Default: full cascade "
             "(crossref,semantic_scholar,scopus,wos,sciencedirect,"
             "openalex).",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch abstracts, do not patch Zotero.")
    parser.add_argument("--log-csv", default=DEFAULT_LOG_CSV,
                        help=f"Path to log CSV (default: {DEFAULT_LOG_CSV}).")
    parser.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR,
                        help=f"GROBID XML cache dir (default: {DEFAULT_CACHE_DIR}).")
    parser.add_argument("--workers", type=int, default=4,
                        help="Parallel fetch threads (default: 4).")
    parser.add_argument(
        "--item-types", default="",
        help="Comma-separated Zotero item types to enrich. Default: every "
             "type that can carry an abstract (journalArticle, bookSection, "
             "book, report, conferencePaper, preprint, thesis, manuscript, "
             "document). Pass journalArticle to restore the old behaviour.",
    )
    parser.add_argument("--filter-keys-file",
                        help="Text file with Zotero item keys, one per line.")
    zotero_io.add_library_args(parser)
    args = parser.parse_args()

    source_names = [s.strip() for s in args.sources.split(",") if s.strip()]
    require("zotero", "api_key", env="ZOTERO_API_KEY")

    os.makedirs(args.cache_dir, exist_ok=True)
    run_date = date.today().isoformat()
    done_items = _already_done(args.log_csv)

    config = _load_config()
    session = http_client.build_session(mailto=config.crossref_mailto)
    if not getattr(args, "user", False) and not args.group:
        # No explicit library chosen — fall back to from_config's
        # group auto-selection (single-group accounts) or error
        # with the accessible-groups list.
        try:
            zot = zotero_io.ZoteroClient.from_config(
                group_id=None, prefer_local=not args.remote,
            )
        except zotero_io.GroupSelectionRequired as e:
            print(zotero_io.format_group_selection_error(e.groups), file=sys.stderr)
            return 2
    else:
        zot = zotero_io.ZoteroClient.from_args(args)

    print("Fetching Zotero items...", end=" ", flush=True)
    item_types = [t.strip() for t in args.item_types.split(",") if t.strip()]
    all_items = zot.abstractable_items(item_types or None)
    print(f"{len(all_items)} items.", flush=True)

    if args.filter_keys_file:
        with open(args.filter_keys_file) as f:
            target = {line.strip() for line in f if line.strip()}
        all_items = [it for it in all_items if it["key"] in target]
        print(f"  After --filter-keys-file: {len(all_items)} items.",
              flush=True)

    missing = [
        it for it in all_items
        if not (it.get("data", {}).get("abstractNote") or "").strip()
        and (it.get("data", {}).get("DOI") or "").strip()
        and it["key"].strip().lower() not in done_items
    ]
    print(f"Missing abstracts (with DOI): {len(missing)}", flush=True)
    if not missing:
        return 0

    by_doi = group_by_doi(missing)
    duplicates = len(missing) - len(by_doi)
    if duplicates:
        print(
            f"  {len(by_doi)} distinct DOIs — {duplicates} of those items "
            f"are duplicate copies and will share one lookup.",
            flush=True,
        )

    sources = fetchers.abstract_sources(session, config)
    if source_names:
        sources = [s for s in sources if s.name in source_names]
    if not sources:
        print(f"ERROR: no abstract fetchers matched --sources={args.sources!r}",
              file=sys.stderr)
        return 2
    print(f"Active fetchers: {[s.name for s in sources]}", flush=True)

    log_fh, log_writer = _open_log(args.log_csv)
    log_lock = threading.Lock()
    counters = {"updated": 0, "skipped": 0, "failed": 0, "done": 0}
    total = len(missing)

    def _process(item: dict, result: CascadeResult) -> None:
        data = item.get("data", {})
        key = item["key"]
        doi = (data.get("DOI") or "").strip()
        title = (data.get("title") or "")[:70]

        with log_lock:
            counters["done"] += 1
            prefix = f"[{counters['done']}/{total}]"

        if not result.found:
            if not doi:
                status, note = "no_doi", "no abstract looked up (item has no DOI)"
            elif result.confirmed_absent:
                status, note = "not_found", "no abstract found"
            else:
                status, note = "lookup_failed", "lookup failed, absence unconfirmed"
            with log_lock:
                counters[
                    "skipped" if status == "not_found" else "failed"
                ] += 1
                log_writer.writerow({
                    "run_date": run_date, "item_key": key, "doi": doi,
                    "title": title, "source": "none", "status": status,
                    "detail": result.detail(),
                })
            print(f"{prefix} {title:<70} {note}", flush=True)
            return

        abstract, source = result.abstract, result.source

        if args.dry_run:
            with log_lock:
                counters["updated"] += 1
                log_writer.writerow({
                    "run_date": run_date, "item_key": key, "doi": doi,
                    "title": title, "source": source, "status": "dry_run",
                    "detail": "",
                })
            print(f"{prefix} {title:<70} found ({source}) [dry-run]",
                  flush=True)
            return

        detail = ""
        try:
            zot.update_abstract(key, abstract)
            status = "updated"
            ok = True
        except Exception as e:
            status = "update_failed"
            detail = f"{type(e).__name__}: {e}"
            ok = False
            print(f"{prefix} {title:<70} ({source}) update failed: {e}",
                  flush=True)

        with log_lock:
            if ok:
                counters["updated"] += 1
            else:
                counters["failed"] += 1
            log_writer.writerow({
                "run_date": run_date, "item_key": key, "doi": doi,
                "title": title, "source": source, "status": status,
                "detail": detail,
            })
        if ok:
            print(f"{prefix} {title:<70} ({source}) → updated", flush=True)

    def _process_group(group: list[dict]) -> None:
        """Look the DOI up once, then record the outcome for every copy.

        Each copy still gets its own Zotero write and its own log row —
        they are separate items and a consumer that resolves this DOI
        may land on any of them. Only the lookup is shared.
        """
        result = _try_cascade(group[0], sources, args.cache_dir)
        for item in group:
            _process(item, result)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(_process_group, g) for g in by_doi.values()]
        for fut in as_completed(futures):
            fut.result()          # re-raise unexpected exceptions

    log_fh.close()
    print(
        f"\nDone. updated={counters['updated']}, "
        f"confirmed-absent={counters['skipped']}, "
        f"failed={counters['failed']}",
        flush=True,
    )
    if counters["failed"]:
        print(
            "  Note: `failed` includes items whose lookups errored "
            "(status=lookup_failed). Their abstracts are unknown, not "
            "absent — re-run to retry them.",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
