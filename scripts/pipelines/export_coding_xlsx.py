#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["openpyxl>=3.1"]
# ///
"""Write the full-text coding as an Excel workbook, or a blank hand-coding template.

The CSV `fulltext_code.py` writes stays the pipeline's log; this is a view
of it. Each column header carries the coding instruction as a comment,
taken from `screening_config.py` (the exact text the model was given), and
every closed-vocabulary column gets a drop-down of its allowed values.
Excel's validation only checks what is typed, so a value already in a cell
(written here, or pasted) that is not allowed turns red instead.

    # the AI's coding
    uv run export_coding_xlsx.py --csv screening/fulltext_screening.csv \\
        --out screening/pilot/pilot_coding.xlsx --keys screening/pilot/sample_keys.txt

    # a blank template for a human coder: same columns and drop-downs,
    # identity columns pre-filled from the abstract-screening log
    uv run export_coding_xlsx.py --template --keys screening/pilot/sample_keys.txt \\
        --fields decision,exclusion_code,research_design --out screening/pilot/hand_coding.xlsx

Sheets: `coding` (header row frozen, one row per item, last logged row per
item_key), `codebook` (every field's rule and allowed values), `run`
(provenance), and a hidden `lists` sheet holding the drop-down values
(an inline Excel list is capped at 255 characters).

The full-text exclusion codes (`FE1-...`) are read from the line-starting
`FEn-description` entries in `FULLTEXT_CODING_SYSTEM_PROMPT`, so the
`exclusion_code` drop-down follows the project's own codebook.

Reads no Zotero data and no API key.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import log_schemas  # noqa: E402
import screening_common  # noqa: E402
from openpyxl import Workbook  # noqa: E402
from openpyxl.comments import Comment  # noqa: E402
from openpyxl.formatting.rule import FormulaRule  # noqa: E402
from openpyxl.styles import Alignment, Font, PatternFill  # noqa: E402
from openpyxl.utils import get_column_letter  # noqa: E402
from openpyxl.worksheet.datavalidation import DataValidation  # noqa: E402

#: What the plugin's own columns mean. Their order and membership come
#: from `log_schemas`; only the wording lives here.
BASE_DESCRIPTIONS = {
    "item_key": "Zotero item key.",
    "doi": "DOI.",
    "title": "Title.",
    "year": "Publication year.",
    "journal": "Journal.",
    "decision": "Does the full text meet the inclusion criteria?",
    "exclusion_code": "Why excluded; empty if included.",
    "reason": "One to three sentences justifying the decision.",
    "model": "Model that coded the row.",
    "prompt_version": "Prompt version that coded the row.",
}
#: Columns that make sense in a spreadsheet; the rest of the CSV's
#: bookkeeping (paths, character counts, timestamps) does not.
SHEET_BASE = ["item_key", "doi", "title", "year", "journal",
              "decision", "exclusion_code", "reason"]
TEMPLATE_IDENTITY = ["item_key", "doi", "title", "year", "journal"]

WIDE = {"title": 50, "reason": 60, "evidence_quotes": 80}
RED = PatternFill("solid", start_color="FFC7CE", end_color="FFC7CE")
HEADER_FILL = PatternFill("solid", start_color="DDEBF7", end_color="DDEBF7")

_FE_LINE = re.compile(r"^\s*(FE\d+)\s*[-:–]\s*(.+?)\s*$", re.MULTILINE)


def exclusion_codes(prompt: str) -> dict[str, str]:
    """`{"FE1": "citation only", ...}` from the coding prompt's code list."""
    return {code: desc for code, desc in _FE_LINE.findall(prompt or "")}


def sheet_columns(cfg) -> list[dict]:
    """Every column of the coding sheet, as `{name, description, values?}`."""
    codes = exclusion_codes(getattr(cfg, "FULLTEXT_CODING_SYSTEM_PROMPT", ""))
    cols: list[dict] = []
    for name in SHEET_BASE:
        col = {"name": name, "description": BASE_DESCRIPTIONS[name]}
        if name == "decision":
            col["values"] = ["include", "exclude"]
        if name == "exclusion_code" and codes:
            col["values"] = list(codes)
            col["description"] += " " + "; ".join(
                f"{c} {d}" for c, d in codes.items()) + "."
        cols.append(col)
    cols += list(cfg.FULLTEXT_CODING_FIELDS)
    cols += [{"name": n, "description": BASE_DESCRIPTIONS[n]}
             for n in log_schemas.FULLTEXT_TRAILING_FIELDS]
    return cols


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def read_keys(path: Path) -> list[str]:
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")]


def comment_text(field: dict) -> str:
    text = field.get("description", "").strip()
    if field.get("values"):
        text += "\n\nAllowed: " + " | ".join(field["values"])
    return text


def pretty(value: str) -> str:
    """`evidence_quotes` is a JSON list; one quote per line reads better."""
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return value
    if not isinstance(data, list):
        return value
    lines = []
    for e in data:
        if isinstance(e, dict):
            body = e.get("quote") or f"[{e.get('location', '')}]"
            lines.append(f"{e.get('field', '')}: {body}")
    return "\n".join(lines)


def build(columns: list[dict], rows: list[dict], *, out: Path, run: dict) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "coding"
    lists = wb.create_sheet("lists")
    lists.sheet_state = "hidden"
    n = max(len(rows), 1)

    for c, field in enumerate(columns, start=1):
        cell = ws.cell(row=1, column=c, value=field["name"])
        cell.font = Font(bold=True)
        cell.fill = HEADER_FILL
        cell.comment = Comment(comment_text(field), "codebook", width=360, height=220)
        letter = get_column_letter(c)
        ws.column_dimensions[letter].width = WIDE.get(field["name"], 18)
        for r, row in enumerate(rows, start=2):
            value = row.get(field["name"], "")
            if field["name"] == "evidence_quotes":
                value = pretty(value)
            ws.cell(row=r, column=c, value=value).alignment = Alignment(
                wrap_text=True, vertical="top")
        values = field.get("values")
        if not values:
            continue
        for i, v in enumerate(values, start=1):
            lists.cell(row=i, column=c, value=v)
        ref = f"lists!${letter}$1:${letter}${len(values)}"
        rng = f"{letter}2:{letter}{n + 1}"
        dv = DataValidation(type="list", formula1=f"={ref}", allow_blank=True,
                            showErrorMessage=True, errorTitle="Not in the codebook",
                            error="Choose one of the allowed values.")
        dv.add(rng)
        ws.add_data_validation(dv)
        ws.conditional_formatting.add(rng, FormulaRule(
            formula=[f'AND({letter}2<>"",COUNTIF({ref},{letter}2)=0)'], fill=RED))

    ws.freeze_panes = "B2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{n + 1}"

    book = wb.create_sheet("codebook")
    book.append(["field", "question / rule", "allowed values"])
    for cell in book[1]:
        cell.font = Font(bold=True)
    for f in columns:
        book.append([f["name"], f.get("description", ""),
                     " | ".join(f.get("values") or [])])
    for col, width in (("A", 30), ("B", 90), ("C", 50)):
        book.column_dimensions[col].width = width
    for row in book.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")

    info = wb.create_sheet("run")
    for k, v in run.items():
        info.append([k, v])
    info.column_dimensions["A"].width = 24
    info.column_dimensions["B"].width = 90

    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)


def select_columns(columns: list[dict], fields_arg: str) -> list[dict]:
    """Identity columns plus the named fields, in the order given."""
    wanted = [f.strip() for f in fields_arg.split(",") if f.strip()]
    by_name = {f["name"]: f for f in columns}
    missing = [w for w in wanted if w not in by_name]
    if missing:
        raise SystemExit(f"unknown field(s): {missing}")
    keep = TEMPLATE_IDENTITY + [w for w in wanted if w not in TEMPLATE_IDENTITY]
    return [by_name[w] for w in keep]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="./screening_config.py")
    p.add_argument("--csv", default="screening/fulltext_screening.csv")
    p.add_argument("--abstract-csv", default="screening/abstract_screening.csv",
                   help="Source of titles for --template.")
    p.add_argument("--out", required=True)
    p.add_argument("--keys", default="", help="Restrict to these item keys (one per line).")
    p.add_argument("--template", action="store_true",
                   help="Blank coding columns; identity columns pre-filled.")
    p.add_argument("--fields", default="",
                   help="Comma-separated fields to include (default: all).")
    args = p.parse_args()

    cfg = screening_common.load_config_module(
        args.config, "screening_config", required=("FULLTEXT_CODING_FIELDS",))
    columns = sheet_columns(cfg)
    if args.fields:
        columns = select_columns(columns, args.fields)
    keys = read_keys(Path(args.keys)) if args.keys else []

    if args.template:
        seen = {r["item_key"]: r for r in read_csv(Path(args.abstract_csv))}
        rows = [{k: seen.get(key, {}).get(k, "") for k in TEMPLATE_IDENTITY}
                | {"item_key": key} for key in keys]
        source = f"blank template; titles from {args.abstract_csv}"
    else:
        last = {r["item_key"]: r for r in read_csv(Path(args.csv))}
        rows = [last[k] for k in keys if k in last] if keys else list(last.values())
        source = args.csv

    build(columns, rows, out=Path(args.out), run={
        "written": date.today().isoformat(),
        "source": source,
        "config": args.config,
        "prompt_version": getattr(cfg, "FULLTEXT_CODING_PROMPT_VERSION", ""),
        "model": getattr(cfg, "FULLTEXT_CODING_MODEL", ""),
        "rows": len(rows),
        "note": "Red cell = value not allowed by the codebook. Header comments "
                "hold the coding instruction given to the model.",
    })
    print(f"Wrote {len(rows)} row(s), {len(columns)} column(s) to {args.out}")
    return 0


if __name__ == "__main__":
    from core.console import enable_utf8_output
    enable_utf8_output()
    sys.exit(main())
