"""Record-list coding fields: spec, prompt, quote check, highlight comments."""
import json

import fulltext_code as fc
import highlight_quotes as hq
import pymupdf
import pytest
import records

SPEC = {
    "keys": ["id", "measure", "concept", "context", "quote"],
    "values": {"concept": ["trust", "not covered"]},
    "comment": "{measure} - {concept}",
}
FIELD = {"name": "measures", "description": "Every measure.", "records": SPEC}
Q1 = "Trust was measured with a five item scale adapted from earlier work."
Q2 = "Commitment was assessed with three survey questions."


def _pdf(tmp_path, text, name="p.pdf"):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(72, 72, 520, 700), text, fontsize=11)
    path = tmp_path / name
    doc.save(str(path))
    doc.close()
    return path


def _rec(i, concept, quote, measure="m"):
    return {"id": i, "measure": measure, "concept": concept,
            "context": "c", "quote": quote}


def test_validate_spec():
    assert records.validate_spec("m", FIELD) == []
    bad = {"name": "m", "records": {"keys": ["a"], "values": {"x": ["y"]}}}
    assert any("quote key" in p for p in records.validate_spec("m", bad))
    assert any("not in keys" in p for p in records.validate_spec("m", bad))
    bad = {"name": "m", "records": {"keys": ["quote"], "comment": "{nope}"}}
    assert any("{nope}" in p for p in records.validate_spec("m", bad))


def test_config_validation_exits_on_bad_spec():
    with pytest.raises(SystemExit):
        fc._validate_coding_field({"name": "m", "records": {"keys": []}})


def test_prompt_shows_record_schema_and_guide():
    out = fc._render_prompt("Return {coding_fields}", [FIELD]) \
        if fc.PLACEHOLDER == "{coding_fields}" else \
        fc._render_prompt(fc.PLACEHOLDER, [FIELD])
    assert '"concept": "trust" | "not covered"' in out
    assert "verbatim" in out and "JSON list" in out


def test_check_drops_unfound_quotes_and_flags_vocab():
    recs = [_rec(1, "trust", Q1), _rec(2, "other", Q2), _rec(3, "trust", "invented")]
    ok, dropped, vocab = records.check(json.dumps(recs), SPEC, lambda q: q != "invented")
    assert [r["id"] for r in ok] == [1, 2]
    assert len(dropped) == 1 and "record 3" in dropped[0]
    assert vocab == ["record 2: concept='other'"]
    assert records.check("nope", SPEC, lambda q: True)[1]


def test_verify_record_fields_against_pdf(tmp_path):
    pdf = _pdf(tmp_path, Q1)
    row = {"decision": "include", "reason": "ok", "item_key": "K",
           "pdf_path": str(pdf),
           "measures": json.dumps([_rec(1, "trust", Q1), _rec(2, "trust", Q2)])}
    fc.verify_record_fields(row, [FIELD])
    assert [r["id"] for r in json.loads(row["measures"])] == [1]
    assert row["decision"] == "include"
    assert "1 record(s) dropped" in row["reason"]
    assert "measures" in row["_dropped_records"]


def test_quote_in_supplement_counts(tmp_path):
    main, supp = _pdf(tmp_path, Q1, "a.pdf"), _pdf(tmp_path, Q2, "s.pdf")
    row = {"decision": "include", "pdf_path": str(main),
           "_supplement_paths": [str(supp)],
           "measures": json.dumps([_rec(1, "trust", Q1), _rec(2, "trust", Q2)])}
    fc.verify_record_fields(row, [FIELD])
    assert len(json.loads(row["measures"])) == 2 and "_dropped_records" not in row


def test_all_records_unverifiable_becomes_error(tmp_path):
    row = {"decision": "include", "pdf_path": str(_pdf(tmp_path, Q1)),
           "measures": json.dumps([_rec(1, "trust", Q2)])}
    fc.verify_record_fields(row, [FIELD])
    assert row["decision"] == "error"


def test_highlight_entries_use_record_fields_for_comment():
    coded = {"measures": json.dumps([_rec(1, "trust", Q1, measure="Trust scale")])}
    (entry,) = hq.record_quotes(coded, {"measures": SPEC})
    assert entry["quote"] == Q1
    assert hq.entry_comment(entry, "ignored") == "Trust scale - trust"


def test_default_comment_joins_non_quote_keys():
    spec = {"keys": ["measure", "concept", "quote"]}
    assert records.render_comment(
        {"measure": "A", "concept": "B", "quote": "q"}, spec) == "A · B"
