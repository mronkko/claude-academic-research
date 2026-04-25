#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyzotero>=1.6",
#     "tenacity>=8.0",
#     "httpx>=0.25",
# ]
# ///
"""Audit a Zotero library for items missing abstracts and / or PDF attachments.

Default mode (no subcommand) — the legacy macro audit:
    audit_zotero_library.py --group 6015547
    audit_zotero_library.py --group 6015547 --output analysis/raw/audit.json
    audit_zotero_library.py --user  # audit the personal library instead

Prints a one-line summary to stdout and writes a JSON file listing the
actionable item keys. Intended to drive subsequent pipeline stages
(enrich_abstracts.py, enrich_pdfs.py) via their --filter-keys-file arg.

Row-level subcommands (T1-2) — operate on a screening CSV, no Zotero:
    audit_zotero_library.py find <substring> [--csv path] [--in-field title]
    audit_zotero_library.py show <item-key> [--csv path]
    audit_zotero_library.py diff <csv-a> <csv-b> [--key item_key]
    audit_zotero_library.py by-decision <include|exclude|borderline> [--csv path]

These replace the 22 inline `python3 -c "import csv; ..."` lookups
visible in the SLR session log — the user's standing rule against
improvised pipeline code applies here, and the operations recur often
enough to deserve named subcommands.

The script reads the Zotero API key from the plugin config
(~/.config/academic-research/config.toml) via core.config_loader for
the macro-audit path only. Subcommand row-queries do not touch
Zotero or the API key.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = SCRIPT_DIR.parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from core.config_loader import require  # noqa: E402


def _classify(items: list[dict], attachments_by_parent: dict[str, list[dict]]) -> dict:
    missing_abstract: list[dict] = []
    missing_pdf: list[dict] = []
    missing_doi: list[dict] = []
    empty_stubs: list[dict] = []
    have_pdf = 0

    for it in items:
        d = it.get("data", {})
        item_type = d.get("itemType", "")
        if item_type in ("attachment", "note", "annotation"):
            continue

        key = d.get("key") or it.get("key")
        if not key:
            continue

        title = (d.get("title") or "")[:80]
        doi = d.get("DOI") or ""
        identifier = {"key": key, "title": title, "doi": doi}

        if not (d.get("abstractNote") or "").strip():
            missing_abstract.append(identifier)

        # Missing DOI check — cheap (Zotero data only, no Crossref).
        # Feeds enrich_dois.py --find-missing via audit.missing_doi.keys.
        # Only applies to journalArticle: books / reports / other item
        # types often legitimately lack DOIs.
        if item_type == "journalArticle" and not doi.strip():
            missing_doi.append(identifier)

        atts = attachments_by_parent.get(key, [])
        pdfs = [a for a in atts if a.get("data", {}).get("contentType") == "application/pdf"]
        real = [a for a in pdfs if a.get("data", {}).get("md5")]
        stubs = [a for a in pdfs if not a.get("data", {}).get("md5")]

        if real:
            have_pdf += 1
        elif stubs:
            stub_keys = [s.get("data", {}).get("key") for s in stubs]
            empty_stubs.append({**identifier, "stub_keys": stub_keys})
        else:
            missing_pdf.append(identifier)

    return {
        "total_items": sum(
            1 for it in items
            if it.get("data", {}).get("itemType") not in ("attachment", "note", "annotation")
        ),
        "have_pdf": have_pdf,
        "missing_pdf_count": len(missing_pdf),
        "empty_stub_count": len(empty_stubs),
        "missing_abstract_count": len(missing_abstract),
        "missing_doi_count": len(missing_doi),
        "missing_abstract": missing_abstract,
        "missing_pdf": missing_pdf,
        "missing_doi": missing_doi,
        "empty_stubs": empty_stubs,
    }


_ROW_QUERY_SUBCOMMANDS = {"find", "show", "diff", "by-decision"}

DEFAULT_SCREENING_CSV = "screening/fulltext_screening.csv"


def _row_query_main(argv: list[str]) -> int:
    """Handle the four CSV-only subcommands without touching Zotero.

    Each subcommand is a thin wrapper around the pure helpers in
    `csv_summary.py` (Package 1). Lives in audit_zotero_library so the
    user has one entry point for "look at my screening data" rather
    than juggling separate scripts.
    """
    import csv_summary

    parser = argparse.ArgumentParser(
        prog="audit_zotero_library.py",
        description=(
            "Row-level lookup against a screening CSV. Operates on the "
            "CSV directly — does not connect to Zotero."
        ),
    )
    sub = parser.add_subparsers(dest="op", required=True)

    p_find = sub.add_parser("find", help="Rows whose values contain a substring.")
    p_find.add_argument("substring", help="Text to search for.")
    p_find.add_argument(
        "--csv", default=DEFAULT_SCREENING_CSV,
        help=f"Screening CSV path (default: {DEFAULT_SCREENING_CSV}).",
    )
    p_find.add_argument(
        "--in-field", default="",
        help="Limit search to this column (default: search all columns).",
    )
    p_find.add_argument(
        "--case-sensitive", action="store_true",
        help="Match substring case-sensitively (default: insensitive).",
    )

    p_show = sub.add_parser("show", help="Print one row by item_key.")
    p_show.add_argument("item_key", help="Zotero item key to look up.")
    p_show.add_argument(
        "--csv", default=DEFAULT_SCREENING_CSV,
        help=f"Screening CSV path (default: {DEFAULT_SCREENING_CSV}).",
    )

    p_diff = sub.add_parser("diff", help="Three-way diff between two screening CSVs.")
    p_diff.add_argument("csv_a", help="First CSV (typically older / before).")
    p_diff.add_argument("csv_b", help="Second CSV (typically newer / after).")
    p_diff.add_argument(
        "--key", default="item_key",
        help="Column to join on (default: item_key).",
    )

    p_by = sub.add_parser(
        "by-decision",
        help="Show row counts by decision (include / exclude / borderline / etc.).",
    )
    p_by.add_argument(
        "--csv", default=DEFAULT_SCREENING_CSV,
        help=f"Screening CSV path (default: {DEFAULT_SCREENING_CSV}).",
    )
    p_by.add_argument(
        "--filter", default="",
        help=(
            "Optional decision value to list (`include` / `exclude` / "
            "`borderline`). When set, prints item_keys + titles for "
            "rows with that decision rather than just the summary count."
        ),
    )

    args = parser.parse_args(argv)

    if args.op == "find":
        rows = csv_summary.read_csv(args.csv)
        hits = csv_summary.find_rows(
            rows, args.substring,
            in_field=args.in_field or None,
            case_sensitive=args.case_sensitive,
        )
        if not hits:
            print(
                f"No rows match {args.substring!r} in {args.csv}"
                + (f" (field={args.in_field!r})" if args.in_field else ""),
                flush=True,
            )
            return 0
        print(f"Found {len(hits)} matching row(s) in {args.csv}:", flush=True)
        for r in hits:
            key = r.get("item_key", "?")
            title = (r.get("title") or "")[:80]
            decision = r.get("decision", "")
            print(f"  {key}  [{decision}]  {title}", flush=True)
        return 0

    if args.op == "show":
        rows = csv_summary.read_csv(args.csv)
        match = next(
            (r for r in rows if r.get("item_key") == args.item_key),
            None,
        )
        if match is None:
            print(f"No row with item_key={args.item_key!r} in {args.csv}", flush=True)
            return 1
        max_field = max(len(k) for k in match.keys())
        for k, v in match.items():
            print(f"  {k:<{max_field}}  {v}", flush=True)
        return 0

    if args.op == "diff":
        result = csv_summary.diff_csvs(args.csv_a, args.csv_b, key=args.key)
        print(
            f"Diff {args.csv_a} vs {args.csv_b} (joining on {args.key!r}):",
            flush=True,
        )
        print(f"  only in A: {len(result['only_in_a'])}", flush=True)
        print(f"  only in B: {len(result['only_in_b'])}", flush=True)
        print(f"  changed:   {len(result['changed'])}", flush=True)
        for entry in result["only_in_a"][:10]:
            print(f"    only-A: {entry[args.key]}", flush=True)
        for entry in result["only_in_b"][:10]:
            print(f"    only-B: {entry[args.key]}", flush=True)
        for entry in result["changed"][:10]:
            print(f"    changed: {entry[args.key]}", flush=True)
        return 0

    if args.op == "by-decision":
        rows = csv_summary.read_csv(args.csv)
        if args.filter:
            wanted = args.filter.lower()
            matches = [r for r in rows if (r.get("decision") or "").lower() == wanted]
            print(
                f"{len(matches)} row(s) with decision={args.filter!r} in {args.csv}",
                flush=True,
            )
            for r in matches:
                key = r.get("item_key", "?")
                title = (r.get("title") or "")[:80]
                code = r.get("exclusion_code") or ""
                trailer = f" [{code}]" if code else ""
                print(f"  {key}{trailer}  {title}", flush=True)
            return 0
        counts = csv_summary.summarize_by(rows, "decision")
        if not counts:
            print(f"  (no rows in {args.csv})", flush=True)
            return 0
        print(f"Decision counts in {args.csv} ({len(rows)} rows total):", flush=True)
        for decision, count in counts.most_common():
            label = decision or "(blank)"
            print(f"  {label:<14}  {count}", flush=True)
        return 0

    return 0


def main() -> int:
    # Subcommand fast-path: when the first positional arg is one of the
    # row-level operations, skip Zotero and dispatch to the CSV helpers.
    # Falls through to the legacy macro audit otherwise so existing
    # `--group X` invocations keep working.
    argv = sys.argv[1:]
    if argv and argv[0] in _ROW_QUERY_SUBCOMMANDS:
        return _row_query_main(argv)

    try:
        import zotero_io
    except ImportError:
        sys.exit(
            "ERROR: pyzotero not installed. Run via `uv run` — the PEP 723 "
            "block at the top of this script declares the dependency and "
            "uv will install it into an ephemeral venv automatically."
        )

    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--group", help="Zotero group library ID (numeric)")
    target.add_argument(
        "--user", action="store_true",
        help="Audit the personal (user) library instead of a group.",
    )
    parser.add_argument(
        "--output", default=".claude/audit/audit.json",
        help="Path to write JSON report (default: .claude/audit/audit.json, "
             "project-local).",
    )
    parser.add_argument(
        "--local", action="store_true", default=True,
        help="Use the local Zotero client (requires Zotero running). Default.",
    )
    parser.add_argument(
        "--remote", action="store_true",
        help="Use the remote api.zotero.org instead of the local client.",
    )
    parser.add_argument(
        "--pdf-fetch-log", default="output/pdf_fetch_log.csv",
        help=(
            "Path to the structured PDF-fetch failure log written by "
            "enrich_pdfs.py (default: output/pdf_fetch_log.csv). When "
            "present, the audit groups failures by cause (out-of-scope, "
            "access-blocked, unavailable, network-error) and suggests "
            "FE codes per group. Pass an empty string to skip."
        ),
    )
    args = parser.parse_args()

    api_key = require("zotero", "api_key", env="ZOTERO_API_KEY")
    if args.user:
        user_id = require("zotero", "user_id", env="ZOTERO_USER_ID")
        library_id, library_type = user_id, "user"
    else:
        library_id, library_type = args.group, "group"

    use_local = not args.remote
    print(f"Connecting to Zotero ({'local' if use_local else 'remote'}, "
          f"{library_type}={library_id})...", flush=True)
    zot = zotero_io.ZoteroClient(
        api_key=api_key,
        group_id=library_id,
        library_type=library_type,
        prefer_local=use_local,
    )

    print("Fetching top-level items...", end=" ", flush=True)
    items = zot.top_items()
    print(f"{len(items)} fetched.", flush=True)

    print("Fetching attachments...", end=" ", flush=True)
    attachments = zot.all_attachments()
    print(f"{len(attachments)} fetched.", flush=True)

    by_parent: dict[str, list[dict]] = {}
    for a in attachments:
        parent = a.get("data", {}).get("parentItem")
        if parent:
            by_parent.setdefault(parent, []).append(a)

    report = _classify(items, by_parent)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    # Write one-key-per-line files next to the JSON so downstream pipeline
    # stages can consume them via --filter-keys-file without any jq step.
    stem = out_path.with_suffix("")  # strip .json
    keys_files: dict[str, Path] = {}
    for category in ("missing_abstract", "missing_pdf", "missing_doi", "empty_stubs"):
        keys_path = Path(f"{stem}.{category}.keys")
        keys_path.write_text(
            "\n".join(entry["key"] for entry in report.get(category, [])) + "\n"
            if report.get(category) else "",
            encoding="utf-8",
        )
        keys_files[category] = keys_path

    print()
    print(f"Library audit ({library_type} {library_id})")
    print(f"  Total items:                {report['total_items']}")
    print(f"  Have PDF:                   {report['have_pdf']}")
    print(f"  Missing PDF:                {report['missing_pdf_count']}")
    print(f"  Empty PDF stubs:            {report['empty_stub_count']}")
    print(f"  Missing abstract:           {report['missing_abstract_count']}")
    print(f"  Missing DOI:                {report['missing_doi_count']}")
    print(f"  Details written to:         {out_path}")
    print(f"  Keys files written to:      "
          f"{stem}.{{missing_abstract,missing_pdf,missing_doi,empty_stubs}}.keys")
    print()
    print("Next steps — feed the .keys files directly into pipeline stages:")
    if report["missing_doi_count"]:
        print(f"  uv run {SCRIPT_DIR}/enrich_dois.py "
              f"--find-missing --filter-keys-file {keys_files['missing_doi']}")
    if report["missing_abstract_count"]:
        print(f"  uv run {SCRIPT_DIR}/enrich_abstracts.py "
              f"--filter-keys-file {keys_files['missing_abstract']}")
    if report["missing_pdf_count"]:
        print(f"  uv run {SCRIPT_DIR}/enrich_pdfs.py "
              f"--filter-keys-file {keys_files['missing_pdf']}")
    if report["empty_stub_count"]:
        print(f"  # {report['empty_stub_count']} empty stubs to delete; see "
              f"{keys_files['empty_stubs']}")

    # PDF-fetch failure-cause grouping (T4-3). Reads the structured log
    # written by enrich_pdfs and groups items by why the cascade gave
    # up. Each group gets a suggested FE-code label so the user can
    # adjudicate in bulk rather than retyping per item.
    if args.pdf_fetch_log:
        try:
            import pdf_fetch_log
        except ImportError:
            pdf_fetch_log = None  # type: ignore[assignment]
        if pdf_fetch_log is not None:
            failures = pdf_fetch_log.read_failures(args.pdf_fetch_log)
            if failures:
                groups = pdf_fetch_log.group_by_cause(failures)
                print()
                print(
                    f"PDF-fetch failures grouped by cause "
                    f"({len(failures)} total, from {args.pdf_fetch_log})"
                )
                # Stable ordering: most actionable causes first.
                ordered = [
                    pdf_fetch_log.FailureCause.ACCESS_BLOCKED.value,
                    pdf_fetch_log.FailureCause.OUT_OF_SCOPE.value,
                    pdf_fetch_log.FailureCause.UNAVAILABLE.value,
                    pdf_fetch_log.FailureCause.NETWORK_ERROR.value,
                ]
                for cause in ordered:
                    rows = groups.get(cause, [])
                    if not rows:
                        continue
                    suggestion = pdf_fetch_log.SUGGESTED_FE_CODE.get(cause, "")
                    print(f"  {cause} ({len(rows)} items)")
                    print(f"    suggested action: {suggestion}")
                    sample_keys = [r.get("item_key", "") for r in rows[:5]]
                    print(f"    sample keys: {', '.join(k for k in sample_keys if k)}")
                    if len(rows) > 5:
                        print(f"    ...and {len(rows) - 5} more")
    return 0


if __name__ == "__main__":
    sys.exit(main())
