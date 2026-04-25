#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Summarize a pilot search-results CSV across the dimensions an SLR
designer needs to decide year cutoff, journal coverage, and search-DB
choice.

Replaces the user's downstream `pilot_year_cutoffs.py` (edited 11 times
in the SLR session log — the most-iterated ad-hoc script) and
`pilot_breakdown.py`. The PRISMA pilot stage is a real recurring SLR
step; centralising its standard summaries here means a fresh user
doesn't re-derive the same `python3 -c "import csv; ..."` patterns
that surfaced in the triage as Cat 1 friction.

Subcommands:

  year-cutoff       Distribution of hits across publication years.
                    Helps pick a sensible from-year. Output: counts
                    table + cumulative-from-the-top column.
  db-overlap        How much each search database (scopus / wos /
                    openalex / semantic_scholar) found, and how much
                    overlap by DOI. Helps decide whether a single
                    DB is sufficient or two are worth the cost.
  journal-coverage  Per-journal hit counts (top N). Surfaces journals
                    that dominate the corpus and journals you might
                    expect but didn't see.
  field-breakdown   Cross-reference hits against a journals.json scope
                    file (from build_journal_list_from_abs.py) to
                    show hit counts by field code. Requires
                    --journals JSON.

Reads the standard search CSV produced by `search.py` — `SEARCH_ROW_FIELDS`
in `searchers/base.py`. Pure stdlib; matplotlib is optional and only
needed if you pass `--plot OUT.png`.

Usage:
    uv run pilot_analyze.py year-cutoff --csv pilot/search_results.csv
    uv run pilot_analyze.py db-overlap --csv pilot/search_results.csv
    uv run pilot_analyze.py journal-coverage --csv pilot/search_results.csv --top 25
    uv run pilot_analyze.py field-breakdown --csv pilot/search_results.csv \\
        --journals journals.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

# Local sibling — pure stdlib helper module from Package 1.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from csv_summary import read_csv, summarize_by  # noqa: E402

# ---------------------------------------------------------------------------
# Output helpers (used by every subcommand)
# ---------------------------------------------------------------------------


def _print_count_table(title: str, counts: Counter, *, top: int | None = None) -> None:
    """Pretty-print a Counter as `key  count  bar` lines, sorted by count desc."""
    if not counts:
        print(f"{title}: (no rows)", flush=True)
        return
    items = counts.most_common(top)
    width = max(len(str(k)) for k, _ in items) if items else 0
    max_count = max(c for _, c in items)
    print(title, flush=True)
    for key, count in items:
        bar = "█" * max(1, int(40 * count / max_count))
        print(f"  {str(key):<{width}}  {count:>5}  {bar}", flush=True)
    print(flush=True)


def _print_cumulative_year_table(year_counts: Counter) -> None:
    """Print year, count, and cumulative-from-most-recent — the table
    you read to pick a from-year cutoff. e.g. choose the cutoff so the
    cumulative count covers ~80% of the corpus, or to start at a
    natural break in publication volume."""
    if not year_counts:
        print("Year cutoff: (no rows with parseable year)", flush=True)
        return
    years_sorted = sorted(year_counts.keys(), reverse=True)
    total = sum(year_counts.values())
    cumulative = 0
    print(f"Year cutoff analysis ({total} rows total)", flush=True)
    print(f"  {'year':<6}  {'count':>5}  {'cumulative':>10}  {'pct':>5}", flush=True)
    for year in years_sorted:
        c = year_counts[year]
        cumulative += c
        pct = 100 * cumulative / total
        print(f"  {str(year):<6}  {c:>5}  {cumulative:>10}  {pct:>4.1f}%", flush=True)
    print(flush=True)


def _maybe_save_plot(counts: Counter, out_path: str, *, title: str) -> None:
    """Emit a bar chart PNG via matplotlib, if available and requested.

    matplotlib is intentionally NOT in the script's PEP 723 deps —
    pilot analysis is useful without plots, and the dep is heavy. Users
    who want plots install `matplotlib` themselves and pass --plot.
    """
    if not out_path:
        return
    try:
        import matplotlib  # type: ignore[import-not-found]
        matplotlib.use("Agg")  # headless
        import matplotlib.pyplot as plt  # type: ignore[import-not-found]
    except ImportError:
        print(
            f"  [info] --plot {out_path} skipped — matplotlib not installed. "
            f"`pip install matplotlib` to enable.",
            flush=True,
        )
        return
    keys, vals = zip(*counts.most_common(), strict=False)
    fig, ax = plt.subplots(figsize=(10, max(3, len(keys) * 0.3)))
    ax.barh(list(keys)[::-1], list(vals)[::-1])
    ax.set_title(title)
    fig.tight_layout()
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path)
    plt.close(fig)
    print(f"  Plot written to {out_path}", flush=True)


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------


def cmd_year_cutoff(rows: list[dict[str, str]], plot_path: str = "") -> Counter:
    """Distribution of hits across publication years (from `year` column)."""
    # Coerce to int when possible; non-numeric years bucket as "?".
    years: Counter = Counter()
    for r in rows:
        y = (r.get("year") or "").strip()
        years[y or "?"] += 1
    _print_cumulative_year_table(years)
    _maybe_save_plot(years, plot_path, title="Hits by publication year")
    return years


def cmd_db_overlap(rows: list[dict[str, str]]) -> dict[str, int]:
    """Per-database hit counts + per-pair DOI-overlap."""
    by_db: dict[str, set[str]] = {}
    for r in rows:
        db = (r.get("db") or "").strip() or "?"
        doi = (r.get("doi") or "").strip().lower()
        by_db.setdefault(db, set())
        if doi:
            by_db[db].add(doi)

    print("DB hit counts (rows)", flush=True)
    counts = summarize_by(rows, "db")
    _print_count_table("  by row count", counts)

    print("DB DOI-overlap (intersection sizes)", flush=True)
    dbs = sorted(by_db)
    if len(dbs) < 2:
        print("  (need at least two databases for overlap)", flush=True)
    else:
        for i, db_a in enumerate(dbs):
            for db_b in dbs[i + 1:]:
                inter = len(by_db[db_a] & by_db[db_b])
                only_a = len(by_db[db_a] - by_db[db_b])
                only_b = len(by_db[db_b] - by_db[db_a])
                print(
                    f"  {db_a} ∩ {db_b}: {inter} shared  "
                    f"(only-{db_a}: {only_a}, only-{db_b}: {only_b})",
                    flush=True,
                )
        print(flush=True)
    return {db: len(s) for db, s in by_db.items()}


def cmd_journal_coverage(
    rows: list[dict[str, str]], top: int = 25, plot_path: str = "",
) -> Counter:
    """Top-N journals by hit count + tail summary."""
    counts = summarize_by(rows, "source")
    _print_count_table(
        f"Top {top} journals by hit count "
        f"({len(counts)} distinct journals total)",
        counts, top=top,
    )
    _maybe_save_plot(counts, plot_path, title="Hits by journal (top)")
    return counts


def cmd_field_breakdown(
    rows: list[dict[str, str]], journals_path: Path, plot_path: str = "",
) -> Counter:
    """Cross-reference hits with journals.json to count by field code."""
    if not journals_path.is_file():
        sys.exit(f"ERROR: --journals path does not exist: {journals_path}")
    payload = json.loads(journals_path.read_text(encoding="utf-8"))
    journals = payload.get("journals", [])
    issn_to_field: dict[str, str] = {
        (j.get("issn") or "").strip(): (j.get("field") or "?") for j in journals
        if j.get("issn")
    }
    field_counts: Counter = Counter()
    unknown = 0
    for r in rows:
        # Search CSV emits ISSNs in either Scopus's bare or WoS's
        # hyphenated form. Try both before falling through.
        issn = (r.get("issn") or "").strip()
        if issn in issn_to_field:
            field_counts[issn_to_field[issn]] += 1
            continue
        # Hyphen-insert lookup if the ISSN was bare.
        if len(issn) == 8 and issn.isdigit():
            with_hyphen = f"{issn[:4]}-{issn[4:]}"
            if with_hyphen in issn_to_field:
                field_counts[issn_to_field[with_hyphen]] += 1
                continue
        # Hyphen-strip lookup if the ISSN was hyphenated.
        if "-" in issn:
            without_hyphen = issn.replace("-", "")
            if without_hyphen in issn_to_field:
                field_counts[issn_to_field[without_hyphen]] += 1
                continue
        unknown += 1
    _print_count_table(
        f"Hits by field code ({journals_path.name}; "
        f"{unknown} rows had no matching journal)",
        field_counts,
    )
    _maybe_save_plot(field_counts, plot_path, title="Hits by field code")
    return field_counts


# ---------------------------------------------------------------------------
# CLI dispatch
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Summarize an SLR pilot search-results CSV.",
    )
    parser.add_argument(
        "subcommand",
        choices=("year-cutoff", "db-overlap", "journal-coverage", "field-breakdown"),
        help="Which summary to compute.",
    )
    parser.add_argument(
        "--csv", default="pilot/search_results.csv",
        help="Path to the pilot search results CSV "
             "(default: pilot/search_results.csv).",
    )
    parser.add_argument(
        "--top", type=int, default=25,
        help="(journal-coverage only) Show this many top journals.",
    )
    parser.add_argument(
        "--journals", default="journals.json",
        help="(field-breakdown only) Path to journals.json from "
             "build_journal_list_from_abs.py "
             "(default: journals.json in the current directory).",
    )
    parser.add_argument(
        "--plot", default="",
        help="Optional output PNG path for a bar chart "
             "(requires `matplotlib` installed).",
    )
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.is_file():
        sys.exit(f"ERROR: --csv path not found: {csv_path}")
    rows = read_csv(csv_path)
    print(f"Loaded {len(rows)} rows from {csv_path}\n", flush=True)

    if args.subcommand == "year-cutoff":
        cmd_year_cutoff(rows, plot_path=args.plot)
    elif args.subcommand == "db-overlap":
        cmd_db_overlap(rows)
    elif args.subcommand == "journal-coverage":
        cmd_journal_coverage(rows, top=args.top, plot_path=args.plot)
    elif args.subcommand == "field-breakdown":
        cmd_field_breakdown(rows, Path(args.journals), plot_path=args.plot)
    return 0


if __name__ == "__main__":
    sys.exit(main())
