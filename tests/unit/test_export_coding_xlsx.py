"""export_coding_xlsx: codebook comments, drop-downs, out-of-vocabulary rule."""

from __future__ import annotations

import json
from types import SimpleNamespace

import export_coding_xlsx as x
from openpyxl import load_workbook

PROMPT = """EXCLUSION CODES:
  FE1-citation only
  FE2-other method
  FE5-catch-all
"""
CFG = SimpleNamespace(
    FULLTEXT_CODING_SYSTEM_PROMPT=PROMPT,
    FULLTEXT_CODING_FIELDS=[
        {"name": "design", "description": "Primary design.",
         "values": ["panel", "survey"]},
        {"name": "evidence_quotes", "description": "Verbatim quotes."},
    ],
)


def test_exclusion_codes_come_from_the_prompt():
    assert x.exclusion_codes(PROMPT) == {
        "FE1": "citation only", "FE2": "other method", "FE5": "catch-all"}


def test_columns_follow_log_schemas_and_config():
    names = [c["name"] for c in x.sheet_columns(CFG)]
    assert names[:8] == x.SHEET_BASE
    assert names[8:10] == ["design", "evidence_quotes"]
    assert names[-2:] == ["model", "prompt_version"]
    code = next(c for c in x.sheet_columns(CFG) if c["name"] == "exclusion_code")
    assert code["values"] == ["FE1", "FE2", "FE5"]


def test_workbook_has_comments_dropdowns_and_pretty_quotes(tmp_path):
    cols = x.sheet_columns(CFG)
    quotes = json.dumps([{"field": "design", "quote": "We use a panel."}])
    row = {"item_key": "K1", "design": "panel", "evidence_quotes": quotes}
    out = tmp_path / "c.xlsx"
    x.build(cols, [row], out=out, run={"rows": 1})
    wb = load_workbook(out)
    ws = wb["coding"]
    header = {c.value: c for c in ws[1]}
    assert "Primary design." in header["design"].comment.text
    assert "panel | survey" in header["design"].comment.text
    assert ws.freeze_panes == "B2"
    assert wb["lists"].sheet_state == "hidden"
    assert len(ws.data_validations.dataValidation) >= 3   # decision, code, design
    assert ws.cell(row=2, column=header["evidence_quotes"].column).value == (
        "design: We use a panel.")
    assert {"codebook", "run"} <= set(wb.sheetnames)


def test_select_columns_keeps_identity_first_and_rejects_unknown():
    cols = x.sheet_columns(CFG)
    picked = x.select_columns(cols, "design,decision")
    assert [c["name"] for c in picked][:5] == x.TEMPLATE_IDENTITY
    assert [c["name"] for c in picked][5:] == ["design", "decision"]
    try:
        x.select_columns(cols, "nope")
    except SystemExit as e:
        assert "nope" in str(e)
    else:
        raise AssertionError("unknown field accepted")


def test_exclusion_codes_accept_space_and_colon_styles_and_explicit_constant():
    v4 = "  FE1 Citation only: it cites the method\n  FE2: other method\n"
    assert x.exclusion_codes(v4) == {
        "FE1": "Citation only: it cites the method", "FE2": "other method"}
    cfg = SimpleNamespace(EXCLUSION_CODES={"FE9": "explicit"})
    assert x.exclusion_codes(v4, cfg) == {"FE9": "explicit"}
