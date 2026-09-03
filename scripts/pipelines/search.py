#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pybliometrics>=3.6",
#     "requests>=2.31",
#     "urllib3>=2.0",
#     "tenacity>=8.0",
# ]
# ///
"""Formal systematic search across Scopus / WoS / OpenAlex / Semantic Scholar.

Reads a per-project `search_config.py` (see
`${CLAUDE_PLUGIN_ROOT}/templates/search_config.py`) for year window,
journal ISSN list, Scopus/WoS `QUERY_DEFS`, and OpenAlex/Semantic
Scholar `BLOCK_A_TERMS` / `BLOCK_B_TERMS`. Dispatches each source
via the `searchers/` registry, deduplicates across databases by DOI
with a title+first-author fallback, and writes:

    <output-dir>/search_results_raw.csv   — pre-dedup, all hits
    <output-dir>/search_results.csv       — deduplicated union
    <metadata-dir>/search_metadata.json   — parameters, timestamps, counts
    <metadata-dir>/search_run.json        — DOI-set hash (integrity gatekeeper)

Two streams feed those files. The **keyword** stream is the journal- and
term-restricted database search. The **citation** stream — forward
snowballing — lists everything citing each DOI in the config's
`CITATION_SEEDS`, with no journal restriction, because a paper that
applies a method cites the paper that introduced it while often using
none of the review's topic vocabulary. Both land in one corpus under one
DOI hash; the `discovery_source` column keeps them separable, which
PRISMA requires (a citation search is reported under "other sources",
not in the database counts).

The DOI-set hash in `search_run.json` is the single load-bearing
invariant: downstream test suites compare each manuscript render
against this hash to catch silent scope changes.

Usage:
    uv run search.py --config ./search_config.py
    uv run search.py --config ./search_config.py --databases scopus,wos
    uv run search.py --config ./search_config.py --databases openalex
    uv run search.py --config ./search_config.py --streams citation
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import screening_common  # noqa: E402
from plugin_version import plugin_version  # noqa: E402
from searchers import (  # noqa: E402
    DISCOVERY_CITATION,
    DISCOVERY_KEYWORD,
    SEARCH_ROW_FIELDS,
    SearchContext,
    searchers_by_name,
)


def _load_config(path: str):
    return screening_common.load_config_module(
        path, "search_config", required=("FROM_YEAR", "TO_YEAR", "JOURNALS"),
    )


def _title_author_key(title: str, authors: str) -> str:
    t = re.sub(r"\W+", " ", (title or "").lower()).strip()
    first_last = ""
    if authors:
        first_last = authors.split(";")[0].split(",")[0].strip().lower()
    return f"{t}|{first_last}" if t else ""


#: Fields a duplicate row may contribute to the row that is kept:
#: first non-empty value wins, so whichever database happened to be
#: searched first stops deciding what metadata survives. Databases
#: differ in what they populate — Semantic Scholar has no issue number,
#: OpenAlex has no page range for some records — and dedup used to
#: throw the complement away with the duplicate.
_MERGEABLE_FIELDS = ("abstract", "volume", "issue", "pages", "type")


def _has_comma_authors(authors: str) -> bool:
    """True when every creator in the string is in `Last, First` form.

    The distinction is worth a column: Scopus and WoS return names
    already split at the comma, while OpenAlex and Semantic Scholar
    return display order ("Jane Doe") and no split exists in their
    responses. An import can build a correct Zotero creator from the
    first shape offline; the second one has to be reconstructed from
    Crossref, one HTTP request per record.
    """
    parts = [p.strip() for p in (authors or "").split(";") if p.strip()]
    return bool(parts) and all("," in p for p in parts)


def _merge_row(keeper: dict, other: dict) -> None:
    """Fold what `other` knows into `keeper`, in place.

    Never overwrites a value the keeper already has, with one exception:
    comma-format authors replace display-order authors, because that is
    a strictly better rendering of the same fact. A run that searched
    OpenAlex and WoS therefore imports WoS's splittable names even when
    OpenAlex's row arrived first — repairing the creators of every
    overlapping record with no network calls at all.
    """
    for field in _MERGEABLE_FIELDS:
        if not (keeper.get(field) or "") and other.get(field):
            keeper[field] = other[field]
    if not _has_comma_authors(keeper.get("authors", "")) and \
            _has_comma_authors(other.get("authors", "")):
        keeper["authors"] = other["authors"]
    # A record both streams found is reported as a database hit. PRISMA
    # counts a citation search for what it *adds*, so attributing an
    # overlap to it would inflate "other sources" and understate the
    # databases. Stated as a rule rather than left to arrival order,
    # which changes with --databases and --streams.
    if DISCOVERY_KEYWORD in (keeper.get("discovery_source"),
                             other.get("discovery_source")):
        keeper["discovery_source"] = DISCOVERY_KEYWORD


def _dedup(rows: list[dict]) -> tuple[list[dict], int]:
    by_doi: dict[str, dict] = {}
    no_doi: list[dict] = []
    for r in rows:
        doi = r["doi"].strip()
        if not doi:
            no_doi.append(r)
        elif doi not in by_doi:
            by_doi[doi] = r
        else:
            _merge_row(by_doi[doi], r)

    title_to_doi: dict[str, str] = {}
    for doi, r in by_doi.items():
        tk = _title_author_key(r.get("title", ""), r.get("authors", ""))
        if tk:
            title_to_doi[tk] = doi

    unresolved: list[dict] = []
    merged = 0
    for r in no_doi:
        tk = _title_author_key(r.get("title", ""), r.get("authors", ""))
        if tk and tk in title_to_doi:
            _merge_row(by_doi[title_to_doi[tk]], r)
            merged += 1
        else:
            unresolved.append(r)
    return list(by_doi.values()) + unresolved, merged


def _validate_search_fields(value: str) -> str:
    """Check `--search-fields`, which decides what "searched" means.

    Worth validating rather than defaulting on a typo: the two settings
    retrieve materially different populations. OpenAlex's default
    `search=` covers full text, and across six management journals
    "three-way interaction" appears in 17 titles or abstracts against 113
    full texts — so a silent fallback could change a review's recall by
    an order of magnitude without changing a visible parameter.
    """
    if value not in ("all", "title_abstract"):
        sys.exit(f"ERROR: --search-fields must be `all` or "
                 f"`title_abstract` (got {value!r}).")
    return value


def _resolve_citation_scope(choice: str, *, issns: list[str]) -> bool:
    """Whether the citation stream restricts to the config's journal list.

    `auto` (the default) scopes whenever `JOURNALS` names anything. That
    default was chosen against the alternative of opting in, and the
    tradeoff is worth being explicit about: an open citation stream can
    be almost entirely out of scope — on one review a seed returned 1839
    citing works of which 107 were in the 22 target journals, and the
    other 1732 spanned 760 unrelated venues that were fetched, imported
    and then trashed by hand — but a changed default also means the same
    config returns a different corpus across releases with no flag
    change. `search_metadata.json` records the resolved value and the
    plugin version, and the banner prints it, so the change is at least
    never silent.

    With no journals named there is nothing to scope to, so `auto` stays
    open: scoping against an empty list would empty the stream.
    """
    if choice not in ("auto", "on", "off"):
        sys.exit(f"ERROR: --citation-journal-scope must be auto, on or off "
                 f"(got {choice!r}).")
    if choice == "off":
        return False
    if choice == "on":
        return True
    return bool([i for i in issns if str(i).strip()])


def _resolve_stream_databases(
    *,
    default: list[str],
    keyword: str,
    citation: str,
    available: list[str],
) -> tuple[list[str], list[str]]:
    """Which databases each stream runs against.

    `--databases` is the default for both; `--keyword-databases` and
    `--citation-databases` override it per stream. A flat list cannot
    express a database that belongs in one stream and not the other, and
    that case is real rather than hypothetical: Semantic Scholar returns
    no ISSN, so it cannot be scoped to a journal list at all, while for a
    citation search it returned about 50% more citing works than OpenAlex
    on a live seed.

    An override may name a database outside `--databases`: that flag is a
    default, not a ceiling, and needing a source in one stream should not
    force it into the other.

    The literal `none` empties a stream — distinct from omitting the
    flag, and the way to run one stream's databases without disabling the
    other stream wholesale.
    """
    def parse(raw: str, flag: str) -> list[str] | None:
        if not raw.strip():
            return None
        if raw.strip().lower() == "none":
            return []
        names = [n.strip() for n in raw.split(",") if n.strip()]
        unknown = [n for n in names if n not in available]
        if unknown:
            sys.exit(f"ERROR: unknown database(s) in {flag}: {unknown}. "
                     f"Available: {available}")
        return names

    parsed_keyword = parse(keyword, "--keyword-databases")
    parsed_citation = parse(citation, "--citation-databases")
    return (
        list(default) if parsed_keyword is None else parsed_keyword,
        list(default) if parsed_citation is None else parsed_citation,
    )


def _run_keyword_stream(source, cfg, ctx: SearchContext) -> list[dict]:
    """The journal- and term-restricted database search for one source.

    A source whose query shape is absent from the config is skipped with
    a message rather than failed: a config that defines only block terms
    is a valid OpenAlex/S2 run, and one that defines only QUERY_DEFS is a
    valid Scopus/WoS run.
    """
    name = source.name
    if name in ("scopus", "wos") and not hasattr(cfg, "QUERY_DEFS"):
        print(f"  ({name} needs QUERY_DEFS in the config — skipping)",
              flush=True)
        return []
    if (name in ("openalex", "semantic_scholar")
        and not (getattr(cfg, "BLOCK_A_TERMS", None)
                 or getattr(cfg, "BLOCK_B_TERMS", None))):
        print(f"  ({name} needs BLOCK_A_TERMS / BLOCK_B_TERMS — skipping)",
              flush=True)
        return []
    return source.run(cfg, ctx)


def _run_citation_stream(
    source, seeds: list[str], ctx: SearchContext,
) -> list[dict]:
    """Forward snowballing: everything citing each seed DOI.

    Skipped with a message for a source that cannot list citing works.
    Scopus needs an EID rather than a DOI for `REFEID(...)`, and the WoS
    Starter tier exposes no cited-reference endpoint at all; neither is a
    reason to fail a run whose other databases can do it.
    """
    if not source.supports_citation_search:
        print(
            f"  ({source.name} cannot list citing works — citation stream "
            f"skipped for this database)",
            flush=True,
        )
        return []
    return source.run_citations(seeds, ctx)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="./search_config.py",
                        help="Path to the project's search_config.py.")
    parser.add_argument("--output-dir", default="analysis/raw",
                        help="Where to write CSV outputs (default: analysis/raw).")
    parser.add_argument("--metadata-dir", default=".",
                        help="Where to write search_metadata.json / "
                             "search_run.json (default: current directory).")
    parser.add_argument("--databases", default="",
                        help="Comma-separated source names (scopus, wos, "
                             "openalex, semantic_scholar). Default: every "
                             "source with usable credentials.")
    parser.add_argument("--keyword-databases", default="",
                        help="Override --databases for the keyword stream "
                             "only. `none` runs no keyword search. Use this "
                             "when a database is unsuitable for a "
                             "journal-restricted query but wanted for "
                             "citations — Semantic Scholar returns no ISSN, "
                             "so it cannot be scoped to a journal list.")
    parser.add_argument("--citation-databases", default="",
                        help="Override --databases for the citation stream "
                             "only. `none` runs no citation search. May name "
                             "a database absent from --databases; that flag "
                             "is a default, not a ceiling.")
    parser.add_argument("--search-fields", default="all",
                        choices=("all", "title_abstract"),
                        help="Which fields the keyword stream searches "
                             "where the database lets us choose. `all` "
                             "(default) uses OpenAlex's full-text search; "
                             "`title_abstract` restricts it to title and "
                             "abstract, matching what Scopus "
                             "TITLE-ABS-KEY and WoS TS= can reach. Use "
                             "title_abstract for comparability across "
                             "databases; leave it on `all` for recall. "
                             "Recorded in search_metadata.json — PRISMA "
                             "requires reporting the fields searched.")
    parser.add_argument("--citation-journal-scope", default="auto",
                        choices=("auto", "on", "off"),
                        help="Restrict the citation stream to the config's "
                             "JOURNALS list. `auto` (default) scopes "
                             "whenever JOURNALS is non-empty; `off` keeps "
                             "the stream open to any venue, which is what "
                             "it was originally built for. OpenAlex applies "
                             "this server-side so out-of-scope citing works "
                             "are never fetched; Semantic Scholar's "
                             "/citations takes no venue filter, so there it "
                             "is applied after the fact and saves import "
                             "rather than API calls.")
    parser.add_argument("--streams", default="keyword,citation",
                        help="Which search streams to run: `keyword` (the "
                             "journal- and term-restricted database search) "
                             "and/or `citation` (forward snowballing from "
                             "CITATION_SEEDS). Default: both. Use "
                             "`--streams citation` to pilot a seed before "
                             "committing to a full run.")
    args = parser.parse_args()

    streams = [s.strip() for s in args.streams.split(",") if s.strip()]
    unknown_streams = [s for s in streams if s not in ("keyword", "citation")]
    if unknown_streams or not streams:
        sys.exit(f"ERROR: unknown stream(s): {unknown_streams or ['(none)']}. "
                 f"Available: keyword, citation")

    cfg = _load_config(args.config)
    search_fields = _validate_search_fields(args.search_fields)
    citation_scope = _resolve_citation_scope(
        args.citation_journal_scope, issns=list(cfg.JOURNALS.keys()),
    )
    seeds = [
        str(d).strip() for d in getattr(cfg, "CITATION_SEEDS", []) or []
        if str(d).strip()
    ]
    ctx = SearchContext(
        from_year=cfg.FROM_YEAR,
        to_year=cfg.TO_YEAR,
        issns=list(cfg.JOURNALS.keys()),
        # Semantic Scholar returns no ISSN, so an ISSN-only scope check
        # rejected every paper it produced. `JOURNALS` maps
        # ISSN -> (rating, full_title); index 1 is the title.
        journal_titles=[
            entry[1] for entry in cfg.JOURNALS.values()
            if isinstance(entry, (list, tuple)) and len(entry) > 1
        ],
        citation_journal_scope=citation_scope,
        search_fields=search_fields,
        mailto=os.environ.get("CROSSREF_MAILTO", ""),
    )

    registry = searchers_by_name()
    selected: list[str]
    if args.databases:
        selected = [n.strip() for n in args.databases.split(",") if n.strip()]
        unknown = [n for n in selected if n not in registry]
        if unknown:
            sys.exit(f"ERROR: unknown database(s): {unknown}. "
                     f"Available: {list(registry)}")
    else:
        # Default: every source where credentials_error() returns None
        selected = [name for name, src in registry.items()
                    if src.credentials_error(ctx) is None]
        if not selected:
            sys.exit("ERROR: no database has usable credentials. Check the "
                     "wizard set-up or pass --databases explicitly.")

    keyword_dbs, citation_dbs = _resolve_stream_databases(
        default=selected,
        keyword=args.keyword_databases,
        citation=args.citation_databases,
        available=list(registry),
    )
    # A source is visited if either stream wants it. Registry order keeps
    # the run deterministic regardless of flag order.
    selected = [
        name for name in registry
        if (name in keyword_dbs and "keyword" in streams)
        or (name in citation_dbs and "citation" in streams)
    ]
    if not selected:
        sys.exit(
            "ERROR: no database is selected for any requested stream. Check "
            "--databases / --keyword-databases / --citation-databases "
            "against --streams."
        )

    if streams == ["citation"] and not seeds:
        sys.exit(
            "ERROR: --streams citation was requested but the config defines "
            "no CITATION_SEEDS. Add the DOI(s) whose citing works you want, "
            "e.g. CITATION_SEEDS = [\"10.1037/0021-9010.91.4.917\"]."
        )

    output_dir = Path(args.output_dir)
    metadata_dir = Path(args.metadata_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    metadata_dir.mkdir(parents=True, exist_ok=True)
    csv_raw = output_dir / "search_results_raw.csv"
    csv_dedup = output_dir / "search_results.csv"
    metadata_path = metadata_dir / "search_metadata.json"
    run_marker = metadata_dir / "search_run.json"

    run_start = datetime.now(UTC).isoformat()
    print(f"[{run_start}] Starting search")
    print(f"  Databases: {', '.join(selected)}")
    print(f"  Journals:  {len(cfg.JOURNALS)}")
    print(f"  Years:     {ctx.from_year}–{ctx.to_year}")
    if hasattr(cfg, "QUERY_DEFS"):
        print(f"  Queries:   {len(cfg.QUERY_DEFS)} (Scopus/WoS)")
    if getattr(cfg, "BLOCK_A_TERMS", None) or getattr(cfg, "BLOCK_B_TERMS", None):
        a = len(getattr(cfg, "BLOCK_A_TERMS", []) or [])
        b = len(getattr(cfg, "BLOCK_B_TERMS", []) or [])
        print(f"  Blocks:    A={a} terms, B={b} terms (OpenAlex/S2)")
    print(f"  Fields:    {'title+abstract only' if search_fields == 'title_abstract' else 'full text where available (OpenAlex); title/abstract/keywords on Scopus + WoS'}")
    print(f"  Streams:   {', '.join(streams)}")
    if keyword_dbs != citation_dbs:
        # Only worth the lines when the two differ; otherwise the
        # Databases line above already said it.
        print(f"    keyword:  {', '.join(keyword_dbs) or '(none)'}")
        print(f"    citation: {', '.join(citation_dbs) or '(none)'}")
    if "citation" in streams:
        print(
            "  Cite scope: "
            + ("journal-scoped to JOURNALS" if citation_scope
               else "open (any venue)")
        )
        if seeds:
            print(f"  Seeds:     {len(seeds)} cited work(s) — "
                  f"{', '.join(seeds[:3])}"
                  + (" …" if len(seeds) > 3 else ""))
        else:
            print("  Seeds:     none (CITATION_SEEDS is empty or absent "
                  "in the config — citation stream will find nothing)")
    print()

    all_rows: list[dict] = []
    counts: dict = {}
    citation_counts: dict = {}
    failed: dict[str, str] = {}
    for name in selected:
        source = registry[name]
        err = source.credentials_error(ctx)
        if err:
            print(f"SKIP {name}: {err}", flush=True)
            continue
        print(f"── {name} ──", flush=True)
        try:
            rows: list[dict] = []
            if "keyword" in streams and name in keyword_dbs:
                rows.extend(_run_keyword_stream(source, cfg, ctx))
            if "citation" in streams and name in citation_dbs and seeds:
                rows.extend(_run_citation_stream(source, seeds, ctx))
        except Exception as e:  # noqa: BLE001
            # One throttled database used to abort the process and throw
            # away every other database's results with it — a Semantic
            # Scholar 429 discarding completed Scopus, WoS and OpenAlex
            # queries, and the API quota they cost. Collect the failure and
            # keep going, so the run below can report all of them at once
            # and preserve the rows that were paid for.
            failed[name] = f"{type(e).__name__}: {e}"
            print(f"  FAILED: {e}", flush=True)
            print()
            continue
        counts[name] = len(rows)
        citation_counts[name] = sum(
            1 for r in rows if r.get("discovery_source") == DISCOVERY_CITATION
        )
        all_rows.extend(rows)
        print()

    if failed:
        # Deliberately still a hard failure. A corpus assembled from a
        # subset of its declared databases is not the corpus the protocol
        # describes, and writing the dedup CSV + search_run.json would
        # present it as one — the DOI hash downstream stages treat as the
        # integrity gatekeeper would certify an incomplete search.
        partial = output_dir / "search_results_raw.partial.csv"
        with partial.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=SEARCH_ROW_FIELDS,
                               extrasaction="ignore")
            w.writeheader()
            w.writerows(all_rows)
        detail = "\n".join(f"  {n}: {msg}" for n, msg in failed.items())
        ok = ", ".join(f"{n}={c}" for n, c in counts.items()) or "none"
        sys.exit(
            f"ERROR: {len(failed)} of {len(failed) + len(counts)} database(s) "
            f"failed:\n{detail}\n"
            f"Succeeded: {ok}.\n"
            f"Their {len(all_rows)} row(s) were kept at {partial} so a re-run "
            f"can be judged against them, but no dedup CSV and no "
            f"search_run.json were written: those certify a complete search, "
            f"and this one was not. Re-run once the failure above clears."
        )

    print(f"Total rows before dedup: {len(all_rows)}")

    with csv_raw.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SEARCH_ROW_FIELDS,
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(all_rows)
    print(f"Raw CSV:   {csv_raw}")

    deduped, merged = _dedup(all_rows)
    no_doi_count = sum(1 for r in deduped if not r["doi"])
    print(f"After DOI dedup:        {len(deduped) + merged}")
    print(f"  Merged no-DOI → DOI:  {merged}")
    print(f"After full dedup:       {len(deduped)} ({no_doi_count} without DOI)")

    with csv_dedup.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SEARCH_ROW_FIELDS,
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(deduped)
    print(f"Dedup CSV: {csv_dedup}")

    run_end = datetime.now(UTC).isoformat()
    sorted_dois = sorted(r["doi"] for r in deduped if r["doi"])
    doi_hash = hashlib.sha256("\n".join(sorted_dois).encode()).hexdigest()

    metadata: dict[str, object] = {
        # A search config does not determine a corpus on its own: 0.16.0
        # and 0.17.0 return different keyword corpora from identical
        # configs, and the date and database list could not explain it.
        "plugin_version": plugin_version(),
        "search_date_start": run_start,
        "search_date_end": run_end,
        "databases": selected,
        "from_year": ctx.from_year,
        "to_year": ctx.to_year,
        "journals": {issn: cfg.JOURNALS[issn][1] for issn in ctx.issns},
        "journal_count": len(cfg.JOURNALS),
        "per_database_counts": counts,
        "total_raw_rows": len(all_rows),
        "total_unique_records": len(deduped),
        "records_without_doi": no_doi_count,
        # PRISMA reports a citation search separately from the database
        # counts, so the split has to survive into the metadata rather
        # than being recoverable only by re-reading the CSV.
        "streams": streams,
        "keyword_databases": keyword_dbs,
        "citation_databases": citation_dbs,
        # PRISMA requires reporting the fields searched, and the four
        # databases do not search the same ones.
        "search_fields": search_fields,
        "search_field_note": (
            "OpenAlex `search=` covers full text; Scopus TITLE-ABS-KEY "
            "and WoS TS= cover title, abstract and keywords only. "
            "`title_abstract` restricts OpenAlex to match them."
        ),
        "citation_journal_scope": citation_scope,
        "citation_seeds": seeds,
        "per_database_citation_counts": citation_counts,
        "unique_records_by_discovery_source": {
            value: sum(1 for r in deduped
                       if r.get("discovery_source") == value)
            for value in (DISCOVERY_KEYWORD, DISCOVERY_CITATION)
        },
    }
    if hasattr(cfg, "QUERY_DEFS"):
        metadata["query_defs"] = [
            {"label": lbl, "scopus": sc, "wos": wc}
            for lbl, sc, wc in cfg.QUERY_DEFS
        ]
    if getattr(cfg, "BLOCK_A_TERMS", None):
        metadata["block_a_terms"] = list(cfg.BLOCK_A_TERMS)
    if getattr(cfg, "BLOCK_B_TERMS", None):
        metadata["block_b_terms"] = list(cfg.BLOCK_B_TERMS)

    metadata_path.write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    run_marker.write_text(
        json.dumps({
            "run_timestamp": run_start,
            "unique_records": len(deduped),
            "unique_dois": len(sorted_dois),
            "doi_sha256": doi_hash,
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"\nDone. {len(deduped)} unique records.")
    print(f"  Metadata:   {metadata_path}")
    print(f"  Run marker: {run_marker}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
