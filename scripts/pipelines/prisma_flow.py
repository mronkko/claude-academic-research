#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""PRISMA 2020 flow counts from the pipeline's own outputs.

Reads `search_metadata.json`, the abstract-screening log and the
full-text-coding log, and builds every number of the PRISMA 2020 flow
diagram (Page et al., BMJ 2021;372:n71) with the arithmetic done here,
not by hand and not by a language model. Writes:

    analysis/results/prisma_flow.json   all counts, checks and notes
    analysis/results/prisma_flow.mmd    Mermaid flowchart of the diagram
    analysis/results/prisma_flow.md     the same counts as a table

Counts the pipeline cannot know (Embase or Cochrane exports imported by
hand, trial registers, duplicates removed outside the pipeline, reports
that could not be retrieved, several reports of one study) go in an
optional `--extra` JSON file that the reviewers write and own:

    {
      "databases_extra": {"embase": 412, "cochrane": 9},
      "registers": {"clinicaltrials.gov": 3},
      "duplicates_removed_extra": 188,
      "other_methods_extra": {"reference list checking": 14},
      "reports_not_retrieved": 2,
      "studies_included": 11
    }

`duplicates_removed_extra` is duplicates removed OUTSIDE the pipeline
(for example in Zotero after importing Embase). Everything the script
computes from logs is marked as computed; everything from `--extra` is
marked as reported by the reviewers.

Each count that depends on another gets an arithmetic check. A failed
check never changes a number: it is listed under `checks` and the exit
code is 1 with `--strict`. Latest row wins in both logs, as in
`screening_report.py`.

Usage:
    uv run prisma_flow.py
    uv run prisma_flow.py --extra prisma_extra.json --strict
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

DEFAULT_META = "search_metadata.json"
DEFAULT_ABSTRACT_LOG = "screening/abstract_screening.csv"
DEFAULT_FULLTEXT_LOG = "screening/fulltext_screening.csv"
DEFAULT_OUT_DIR = "analysis/results"

#: Abstract-stage decisions that move an item on to full-text assessment.
SOUGHT = ("include", "borderline")


def last_row_per_key(rows: list[dict]) -> dict[str, dict]:
    """Latest row per `item_key`: the logs are append-only."""
    latest: dict[str, dict] = {}
    for row in rows:
        key = (row.get("item_key") or "").strip()
        if key:
            latest[key] = row
    return latest


def read_csv(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer, got {value!r}")
    return value


def _int_map(value: object, name: str) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object of name -> count")
    return {str(k): _int(v, f"{name}[{k}]") for k, v in value.items()}


def parse_extra(raw: dict | None) -> dict:
    """Validate the reviewers' `--extra` file; unknown keys are an error."""
    raw = raw or {}
    known = {
        "databases_extra", "registers", "duplicates_removed_extra",
        "other_methods_extra", "reports_not_retrieved", "studies_included",
    }
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ValueError(f"unknown key(s) in extra file: {', '.join(unknown)}")
    out: dict = {
        "databases_extra": _int_map(raw.get("databases_extra"), "databases_extra"),
        "registers": _int_map(raw.get("registers"), "registers"),
        "other_methods_extra": _int_map(
            raw.get("other_methods_extra"), "other_methods_extra"),
        "duplicates_removed_extra": _int(
            raw.get("duplicates_removed_extra", 0), "duplicates_removed_extra"),
        "reports_not_retrieved": None,
        "studies_included": None,
    }
    for key in ("reports_not_retrieved", "studies_included"):
        if raw.get(key) is not None:
            out[key] = _int(raw[key], key)
    return out


def build_flow(meta: dict, abstract_rows: list[dict], fulltext_rows: list[dict],
               extra: dict | None = None) -> dict:
    """All PRISMA 2020 flow counts, plus arithmetic checks and notes."""
    extra = parse_extra(extra)
    checks: list[dict] = []
    notes: list[str] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    # ---- Identification ------------------------------------------------
    per_db = meta.get("per_database_counts") or {}
    per_db_cit = meta.get("per_database_citation_counts") or {}
    db_counts = {
        name: max(int(n) - int(per_db_cit.get(name, 0)), 0)
        for name, n in per_db.items()
    }
    for name, n in extra["databases_extra"].items():
        db_counts[f"{name} (imported)"] = n
    databases_total = sum(db_counts.values())
    registers_total = sum(extra["registers"].values())

    by_stream = meta.get("unique_records_by_discovery_source") or {}
    unique_total = int(meta.get("total_unique_records", 0))
    unique_citation = int(by_stream.get("citation_search", 0))
    citation_rows = sum(int(n) for n in per_db_cit.values())
    other_methods = dict(extra["other_methods_extra"])
    if citation_rows:
        other_methods["citation searching"] = citation_rows
    other_total = sum(other_methods.values())

    identified_total = databases_total + registers_total + other_total

    # ---- Removed before screening -------------------------------------
    by_doctype = meta.get("unique_records_by_doctype") or {}
    not_research = sum(int(n) for n in by_doctype.values())
    pipeline_rows = sum(int(n) for n in per_db.values())
    duplicates_pipeline = max(pipeline_rows - unique_total, 0)
    duplicates_total = duplicates_pipeline + extra["duplicates_removed_extra"]
    removed_total = duplicates_total + not_research
    expected_screened = identified_total - removed_total

    # ---- Screening ------------------------------------------------------
    abs_last = last_row_per_key(abstract_rows)
    abs_counts = Counter((r.get("decision") or "").strip() for r in abs_last.values())
    screened = len(abs_last)
    excluded_abstract = abs_counts.get("exclude", 0)
    sought = sum(abs_counts.get(d, 0) for d in SOUGHT)
    abstract_other = screened - excluded_abstract - sought

    ft_last = last_row_per_key(fulltext_rows)
    ft_counts = Counter((r.get("decision") or "").strip() for r in ft_last.values())
    assessed = len(ft_last)
    ft_excluded = ft_counts.get("exclude", 0)
    ft_included = ft_counts.get("include", 0)
    ft_other = assessed - ft_excluded - ft_included
    by_reason = Counter(
        (r.get("exclusion_code") or "").strip() or "(no code)"
        for r in ft_last.values() if (r.get("decision") or "").strip() == "exclude"
    )

    sought_keys = {k for k, r in abs_last.items()
                   if (r.get("decision") or "").strip() in SOUGHT}
    not_in_sought = sorted(set(ft_last) - sought_keys)
    pending = len(sought_keys - set(ft_last))
    not_retrieved = extra["reports_not_retrieved"]
    if not_retrieved is None:
        not_retrieved = pending
        if pending:
            notes.append(
                f"{pending} report(s) passed abstract screening but are not in "
                "the full-text log. They are counted as 'not retrieved'; if "
                "full-text coding is unfinished, rerun this after it ends, or "
                "set reports_not_retrieved in the extra file.")

    studies = extra["studies_included"]
    if studies is None:
        studies = ft_included
    elif studies > ft_included:
        notes.append("studies_included exceeds the included reports; one "
                     "study cannot have fewer reports than studies.")

    # ---- Arithmetic checks ---------------------------------------------
    check("screened = identified - removed before screening",
          screened == expected_screened,
          f"{screened} screened vs {identified_total} - {removed_total} = "
          f"{expected_screened}")
    check("abstract decisions are include, borderline or exclude",
          abstract_other == 0,
          f"{abstract_other} item(s) with another decision (e.g. error)")
    check("sought = screened - excluded at abstract stage",
          sought == screened - excluded_abstract - abstract_other,
          f"{sought} sought")
    check("every full-text item passed abstract screening",
          not not_in_sought,
          f"{len(not_in_sought)} not in the include/borderline set"
          + (f": {', '.join(not_in_sought[:5])}" if not_in_sought else ""))
    check("assessed = sought - not retrieved",
          assessed == sought - not_retrieved,
          f"{assessed} assessed vs {sought} - {not_retrieved} = "
          f"{sought - not_retrieved}")
    check("full-text decisions are include or exclude",
          ft_other == 0, f"{ft_other} item(s) with another decision")
    check("included = assessed - excluded",
          ft_included == assessed - ft_excluded - ft_other,
          f"{ft_included} included")
    check("excluded reasons add up",
          sum(by_reason.values()) == ft_excluded,
          f"{sum(by_reason.values())} with a reason vs {ft_excluded} excluded")
    if "(no code)" in by_reason:
        notes.append(f"{by_reason['(no code)']} full-text exclusion(s) have no "
                     "exclusion code; PRISMA item 16b needs a reason each.")
    if unique_citation and not citation_rows:
        notes.append("unique citation-stream records found but no per-database "
                     "citation counts in the metadata.")
    if not meta:
        notes.append("search metadata not found: identification counts are 0.")

    return {
        "identification": {
            "databases": {"total": databases_total, "by_source": db_counts},
            "registers": {"total": registers_total, "by_source": extra["registers"]},
            "other_methods": {"total": other_total, "by_source": other_methods},
            "identified_total": identified_total,
            "removed_before_screening": {
                "duplicates": {"total": duplicates_total,
                               "by_pipeline": duplicates_pipeline,
                               "outside_pipeline": extra["duplicates_removed_extra"]},
                "not_research_articles": {"total": not_research,
                                          "by_type": dict(by_doctype)},
                "total": removed_total,
            },
        },
        "screening": {
            "records_screened": screened,
            "records_excluded": excluded_abstract,
            "reports_sought": sought,
            "reports_not_retrieved": not_retrieved,
            "reports_assessed": assessed,
            "reports_excluded": {"total": ft_excluded, "by_reason": dict(by_reason)},
        },
        "included": {"studies": studies, "reports": ft_included},
        "provenance": {
            "computed": ["databases (pipeline), duplicates (pipeline), "
                         "not research articles, all screening counts"],
            "reported_by_reviewers": sorted(
                k for k, v in extra.items() if v not in ({}, 0, None)),
        },
        "checks": checks,
        "notes": notes,
        "ok": all(c["ok"] for c in checks),
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _label(text: str) -> str:
    return text.replace('"', "'")


def to_mermaid(flow: dict) -> str:
    ident = flow["identification"]
    scr = flow["screening"]
    inc = flow["included"]
    rem = ident["removed_before_screening"]

    def src(d: dict) -> str:
        return "<br/>".join(f"{k}: n = {v}" for k, v in d["by_source"].items())

    reasons = "<br/>".join(
        f"{k}: n = {v}" for k, v in scr["reports_excluded"]["by_reason"].items())
    lines = [
        "flowchart TD",
        f'  A["Records identified from:<br/>Databases (n = {ident["databases"]["total"]})'
        + (f"<br/>{src(ident['databases'])}" if ident["databases"]["by_source"] else "")
        + f'<br/>Registers (n = {ident["registers"]["total"]})"]',
        f'  B["Records removed before screening:<br/>'
        f'Duplicates (n = {rem["duplicates"]["total"]})<br/>'
        f'Not research articles (n = {rem["not_research_articles"]["total"]})"]',
        f'  C["Records screened<br/>(n = {scr["records_screened"]})"]',
        f'  D["Records excluded<br/>(n = {scr["records_excluded"]})"]',
        f'  E["Reports sought for retrieval<br/>(n = {scr["reports_sought"]})"]',
        f'  F["Reports not retrieved<br/>(n = {scr["reports_not_retrieved"]})"]',
        f'  G["Reports assessed for eligibility<br/>(n = {scr["reports_assessed"]})"]',
        f'  H["Reports excluded (n = {scr["reports_excluded"]["total"]}):<br/>'
        f'{_label(reasons) if reasons else "none"}"]',
        f'  I["Studies included in review (n = {inc["studies"]})<br/>'
        f'Reports of included studies (n = {inc["reports"]})"]',
        "  A --> B",
        "  A --> C",
        "  C --> D",
        "  C --> E",
        "  E --> F",
        "  E --> G",
        "  G --> H",
        "  G --> I",
    ]
    other = ident["other_methods"]
    if other["total"]:
        lines.insert(
            2, f'  O["Records identified from other methods (n = {other["total"]}):'
               f'<br/>{src(other)}"]')
        lines.append("  O --> C")
    return "\n".join(lines) + "\n"


def to_markdown(flow: dict) -> str:
    ident, scr, inc = flow["identification"], flow["screening"], flow["included"]
    rem = ident["removed_before_screening"]
    rows = [
        ("Records identified from databases", ident["databases"]["total"]),
        ("Records identified from registers", ident["registers"]["total"]),
        ("Records identified from other methods", ident["other_methods"]["total"]),
        ("Duplicate records removed", rem["duplicates"]["total"]),
        ("Records removed as not research articles",
         rem["not_research_articles"]["total"]),
        ("Records screened", scr["records_screened"]),
        ("Records excluded at title/abstract", scr["records_excluded"]),
        ("Reports sought for retrieval", scr["reports_sought"]),
        ("Reports not retrieved", scr["reports_not_retrieved"]),
        ("Reports assessed for eligibility", scr["reports_assessed"]),
        ("Reports excluded after full text", scr["reports_excluded"]["total"]),
        ("Studies included", inc["studies"]),
        ("Reports of included studies", inc["reports"]),
    ]
    out = ["| PRISMA 2020 item | n |", "|---|---|"]
    out += [f"| {name} | {n} |" for name, n in rows]
    out += ["", "Full-text exclusions by reason:", ""]
    reasons = scr["reports_excluded"]["by_reason"]
    out += [f"- {k}: {v}" for k, v in sorted(reasons.items())] or ["- none"]
    out += ["", "Checks:", ""]
    out += [f"- {'PASS' if c['ok'] else 'FAIL'}: {c['name']} ({c['detail']})"
            for c in flow["checks"]]
    if flow["notes"]:
        out += ["", "Notes:", ""] + [f"- {n}" for n in flow["notes"]]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--metadata", default=DEFAULT_META)
    parser.add_argument("--abstract-log", default=DEFAULT_ABSTRACT_LOG)
    parser.add_argument("--fulltext-log", default=DEFAULT_FULLTEXT_LOG)
    parser.add_argument("--extra", default="",
                        help="Reviewer-written JSON of counts the pipeline "
                             "cannot know (see the module docstring).")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--strict", action="store_true",
                        help="Exit 1 when any arithmetic check fails.")
    args = parser.parse_args(argv)

    meta_path = Path(args.metadata)
    meta: dict = {}
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"ERROR: {meta_path} is not valid JSON: {exc}", file=sys.stderr)
            return 2
    extra: dict = {}
    if args.extra:
        try:
            extra = json.loads(Path(args.extra).read_text(encoding="utf-8"))
            flow = build_flow(meta, read_csv(Path(args.abstract_log)),
                              read_csv(Path(args.fulltext_log)), extra)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            print(f"ERROR: extra file: {exc}", file=sys.stderr)
            return 2
    else:
        flow = build_flow(meta, read_csv(Path(args.abstract_log)),
                          read_csv(Path(args.fulltext_log)))

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "prisma_flow.json").write_text(
        json.dumps(flow, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "prisma_flow.mmd").write_text(to_mermaid(flow), encoding="utf-8")
    (out_dir / "prisma_flow.md").write_text(to_markdown(flow), encoding="utf-8")

    scr, inc = flow["screening"], flow["included"]
    ident = flow["identification"]
    print(f"Identified:        {ident['identified_total']}")
    print(f"Removed before:    {ident['removed_before_screening']['total']}")
    print(f"Screened:          {scr['records_screened']}")
    print(f"Sought:            {scr['reports_sought']}")
    print(f"Assessed:          {scr['reports_assessed']}")
    print(f"Included:          {inc['studies']}")
    failed = [c for c in flow["checks"] if not c["ok"]]
    print(f"Checks failed:     {len(failed)}")
    for c in failed:
        print(f"  FAIL {c['name']}: {c['detail']}")
    for n in flow["notes"]:
        print(f"  NOTE {n}")
    print(f"Wrote {out_dir / 'prisma_flow.json'}")
    return 1 if (args.strict and failed) else 0


if __name__ == "__main__":
    sys.exit(main())
