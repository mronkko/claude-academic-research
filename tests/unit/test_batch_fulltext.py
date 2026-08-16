"""The batch coding path must write exactly what the live path writes.

The companion to `test_batch_manifest.py`, which covers the abstract
stage. Full-text coding raises three things abstract screening does not:

- **A missing PDF is a state, not an error.** It goes to the skipped
  sidecar at emit time and must come back as the same `no_pdf` CSV row
  the synchronous path writes — not as an absence, and not as `error`.
- **The CSV's columns are project-specific**, coming from
  `screening_config.FULLTEXT_CODING_FIELDS`. So the manifest carries its
  own copy of that schema: editing the config between emitting a run and
  applying it would otherwise change the shape of a log describing a run
  whose model was never asked about the new fields.
- **`--update-fields` merges into an existing note** rather than writing
  a decision, and the merge must survive the round trip with the
  adjudicated decision intact.

`test_the_batch_path_writes_what_the_live_path_writes` is the acceptance
criterion, and it compares rendered CSV bytes rather than dicts, because
the CSV is what a reviewer reads and what
`templates/test_systematic_review.py` asserts against in a user's
project.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import batch_manifest as bm
import fulltext_code as fc
import pytest
from log_schemas import fulltext_screening_fields

RUN = "fulltext_coding-20260816T000000Z"
MODEL = "org/model-1"
PV = "v1-test"
SYS = "SYSTEM PROMPT"
TS = "2026-08-16T00:05:00+00:00"
FULLTEXT = "1. Introduction\nWe survey 1,243 nascent entrepreneurs."

FIELDS = [
    {"name": "sample", "description": "Who was studied."},
    {"name": "method", "description": "How they were studied."},
]
COLS = fulltext_screening_fields([f["name"] for f in FIELDS])

COMPLETION = json.dumps({
    "decision": "include",
    "exclusion_code": "",
    "reason": "Reports all three criteria.",
    "sample": "1,243 nascent entrepreneurs",
    "method": "cross-sectional survey",
})


# ---------------------------------------------------------------------------
# Fixtures — a Zotero that records instead of writing, and a PDF that
# needs no PDF reader.
# ---------------------------------------------------------------------------


class FakeZot:
    def __init__(self, notes: dict[str, str] | None = None) -> None:
        self.tags: list[tuple[str, tuple, tuple]] = []
        self.notes: dict[str, str] = dict(notes or {})
        self.cloud = self

    def update_tags(self, key, add=(), remove_prefixed=()) -> None:
        self.tags.append((key, tuple(add), tuple(remove_prefixed)))

    def upsert_child_note(self, key, *, marker, note_html) -> None:
        self.notes[key] = note_html

    def children(self, key):  # `zot.cloud.children`, for update mode
        body = self.notes.get(key)
        if body is None:
            return []
        return [{"key": "NOTE0001", "data": {"itemType": "note", "note": body}}]

    def library_ref(self) -> dict:
        return {"kind": "group", "id": "123"}


@pytest.fixture(autouse=True)
def _no_pdf_reader(monkeypatch):
    """Extraction is not what these tests are about."""
    monkeypatch.setattr(
        fc.pdf_text_cache, "get_text", lambda key, path: FULLTEXT,
    )


def _item(key: str = "AAAA1111", **over) -> dict:
    data = {
        "key": key,
        "DOI": "10.1234/example",
        "title": "Entrepreneurial self-efficacy and venture growth",
        "publicationTitle": "Journal of Business Venturing",
        "date": "2019-03-01",
        "tags": [],
    }
    data.update(over)
    return {"key": key, "data": data}


def _pdf(tmp_path: Path, key: str = "AAAA1111") -> Path:
    path = tmp_path / f"{key}.pdf"
    path.write_bytes(b"%PDF-1.4 not really a pdf\n")
    return path


def _attachments(item: dict, pdf: Path) -> dict[str, list[dict]]:
    return {item["key"]: [{
        "key": f"ATT{item['key']}",
        "data": {
            "key": f"ATT{item['key']}",
            "parentItem": item["key"],
            "contentType": "application/pdf",
            "md5": "0" * 32,
            "linkMode": "linked_file",
            "path": str(pdf),
            "filename": pdf.name,
        },
    }]}


def _emit(
    items, tmp_path, *, fields=None, attachments=None, **over,
) -> tuple[list[dict], list[dict]]:
    """Emit a manifest, giving every item a PDF unless told otherwise."""
    fields = FIELDS if fields is None else fields
    atts: dict[str, list[dict]] = {}
    if attachments is None:
        for it in items:
            atts.update(_attachments(it, _pdf(tmp_path, it["key"])))
    else:
        atts.update(attachments)
    kwargs = {
        "run_id": RUN, "system_prompt": SYS, "model": MODEL,
        "prompt_version": PV, "fields": fields,
        "library": {"kind": "group", "id": "123"}, "collection": "COLL0001",
        "attachments_by_parent": atts,
    }
    kwargs.update(over)
    return fc.build_manifest_rows(items, **kwargs)


def _response(req: dict, text: str, **over) -> dict:
    resp = {
        "schema_version": bm.SCHEMA_VERSION,
        "run_id": req["run_id"],
        "request_id": req["request_id"],
        "item_key": req["item_key"],
        "model": MODEL,
        "call_status": "ok",
        "finish_reason": "stop",
        "response_text": text,
        "input_tokens": 4000,
        "output_tokens": 120,
        "generated_at": TS,
    }
    resp.update(over)
    return resp


# ---------------------------------------------------------------------------
# The acceptance criterion
# ---------------------------------------------------------------------------


def test_the_batch_path_writes_what_the_live_path_writes(tmp_path) -> None:
    """Same paper, same model output, same CSV bytes — and same note.

    The live path parses the answer inside its worker and writes the row
    there; the batch path parses it days later out of a responses file.
    Given identical model text the two must be indistinguishable in the
    log, or which path ran becomes a variable in the review.
    """
    item = _item()
    pdf = _pdf(tmp_path)
    lock = threading.Lock()

    # Live path: build the request, generate, parse, apply.
    live_zot = FakeZot()
    live_csv = tmp_path / "live.csv"
    live_row, live_user = fc.build_coding_request(
        item, pdf, model=MODEL, fields=FIELDS,
    )
    live_row.update(fc.parse_coding_response(COMPLETION, FIELDS))
    fc.apply_coded_row(
        live_zot, live_row, fields=FIELDS, prompt_version=PV,
        output_path=live_csv, csv_columns=COLS, log_lock=lock, timestamp=TS,
    )

    # Batch path: through the manifest and back.
    rows, skipped = _emit([item], tmp_path, attachments=_attachments(item, pdf))
    assert skipped == []
    req = rows[0]
    resp = _response(req, COMPLETION)
    batch_zot = FakeZot()
    batch_csv = tmp_path / "batch.csv"
    _, answer = bm.split_reasoning(resp["response_text"])
    batch_row = fc._base_row_from_manifest_row(
        req, model=resp["model"], fields=FIELDS,
    )
    batch_row.update(fc.parse_coding_response(answer, FIELDS))
    fc.apply_coded_row(
        batch_zot, batch_row, fields=FIELDS, prompt_version=PV,
        output_path=batch_csv, csv_columns=COLS, log_lock=lock,
        timestamp=resp["generated_at"],
    )

    assert batch_csv.read_bytes() == live_csv.read_bytes()
    # The prompt the executor was handed is the prompt the live path built.
    assert req["user"] == live_user
    # And Zotero saw the same two writes.
    assert batch_zot.tags == live_zot.tags
    assert batch_zot.notes == live_zot.notes


def test_the_two_paths_share_one_parser() -> None:
    """Not two implementations that happen to agree today.

    The JSON-parse-failure rule decides whether a paper is coded or comes
    back for a re-run, and the cheapest way for it to diverge is a second
    copy in the applier.
    """
    src = Path(fc.__file__).read_text(encoding="utf-8")
    assert src.count("JSON PARSE ERROR — raw:") == 1, (
        "the unparseable-answer rule appears more than once; the applier "
        "must call parse_coding_response, not reimplement it"
    )


def test_the_manifest_reserves_the_budget_the_live_path_uses(tmp_path) -> None:
    """A manifest run with a smaller budget produces truncated JSON for
    exactly the wide configs the formula exists to protect."""
    rows, _ = _emit([_item()], tmp_path)
    assert rows[0]["max_output_tokens"] == fc.output_budget(FIELDS)


def test_a_manifest_row_carries_its_whole_prompt_and_schema(tmp_path) -> None:
    """The executor must need nothing from this plugin: no config, no
    Zotero, no code. That is what lets it run on a GPU node."""
    rows, _ = _emit([_item()], tmp_path)
    req = rows[0]
    assert req["system"] == SYS
    assert FULLTEXT in req["user"]
    assert req["system_sha256"] == bm.sha256(SYS)
    assert req["coding_fields"] == [
        {"name": "sample", "description": "Who was studied."},
        {"name": "method", "description": "How they were studied."},
    ]
    assert json.loads(json.dumps(req)) == req  # plain JSON, no surprises


# ---------------------------------------------------------------------------
# The frozen coding schema
# ---------------------------------------------------------------------------


def test_a_config_edited_after_emit_cannot_change_the_csv_schema() -> None:
    """Adding a field to `screening_config.py` between emit and apply
    would otherwise widen the log with a column no model answered."""
    widened = FIELDS + [{"name": "theory", "description": "Which theory."}]
    with pytest.raises(SystemExit, match="REFUSING"):
        fc.check_coding_fields_match(FIELDS, widened, force=False)


def test_force_apply_logs_the_run_under_the_schema_it_was_emitted_with(
    capsys,
) -> None:
    widened = FIELDS + [{"name": "theory", "description": "Which theory."}]
    fc.check_coding_fields_match(FIELDS, widened, force=True)
    out = capsys.readouterr().out
    assert "WARNING" in out and "theory" in out


def test_an_unchanged_config_applies_without_comment() -> None:
    fc.check_coding_fields_match(FIELDS, list(FIELDS), force=False)


def test_rows_disagreeing_about_the_schema_are_refused() -> None:
    a = {"coding_fields": [{"name": "sample", "description": ""}]}
    b = {"coding_fields": [{"name": "method", "description": ""}]}
    with pytest.raises(bm.ManifestError, match="coding_fields"):
        fc.coding_fields_from_manifest([a, b])


# ---------------------------------------------------------------------------
# The sidecar — a shrunken N must stay visible
# ---------------------------------------------------------------------------


def test_an_item_with_no_pdf_is_skipped_not_sent(tmp_path) -> None:
    rows, skipped = _emit([_item()], tmp_path, attachments={})
    assert rows == []
    assert skipped[0]["skip_reason"] == "no_pdf"
    assert skipped[0]["item_key"] == "AAAA1111"


def test_a_pdf_that_yields_no_text_is_skipped_not_sent(
    tmp_path, monkeypatch,
) -> None:
    """A GPU pass over an empty document is a wasted pass, and whatever
    the model says about it is about no paper at all."""
    monkeypatch.setattr(fc.pdf_text_cache, "get_text", lambda key, path: "")
    rows, skipped = _emit([_item()], tmp_path)
    assert rows == []
    assert skipped[0]["skip_reason"] == "pdf_unreadable"


def test_over_long_fulltext_goes_to_the_sidecar_not_the_manifest(
    tmp_path, monkeypatch,
) -> None:
    """720k chars is ~180k tokens; a self-hosted model serves 8k–128k.
    Truncating silently would send the model half a paper and record the
    verdict as if it had read all of it."""
    monkeypatch.setattr(
        fc.pdf_text_cache, "get_text", lambda key, path: "x" * 5000,
    )
    rows, skipped = _emit([_item()], tmp_path, max_input_chars=500)
    assert rows == []
    assert skipped[0]["skip_reason"] == "too_long_for_context"
    # Both numbers, so the user knows how far over the limit it was.
    assert "500" in skipped[0]["detail"]


def test_a_skipped_no_pdf_comes_back_as_the_row_the_live_path_writes(
    tmp_path,
) -> None:
    """End to end: emit with one codable item and one PDF-less one,
    execute the first, apply both. The absent paper owes the log a row."""
    have, lack = _item("AAAA1111"), _item("BBBB2222")
    manifest = tmp_path / "m.jsonl"
    rows, skipped = _emit(
        [have, lack], tmp_path, attachments=_attachments(have, _pdf(tmp_path)),
    )
    bm.write_manifest(manifest, rows)
    bm.write_skipped(
        bm.skipped_path(manifest), run_id=RUN, stage=bm.STAGE_FULLTEXT,
        skipped=skipped, n_requested=len(rows), selection={},
    )
    responses = tmp_path / "r.jsonl"
    bm._write_jsonl(responses, [_response(rows[0], COMPLETION)])

    out = tmp_path / "log.csv"
    zot = FakeZot()
    assert fc.apply_responses(
        zot, manifest_path=manifest, manifest_rows=rows,
        responses_path=responses, output_path=out, items=[have, lack],
        fields=FIELDS, csv_columns=COLS, force=False,
        skip_already_tagged=False,
    ) == 0

    by_key = {r["item_key"]: r for r in _read_csv(out)}
    assert by_key["AAAA1111"]["decision"] == "include"
    assert by_key["BBBB2222"]["decision"] == fc.NO_PDF_DECISION
    assert by_key["BBBB2222"]["reason"] == fc.NO_PDF_REASON
    assert by_key["BBBB2222"]["model"] == MODEL
    # Only the coded item is tagged; the PDF-less one stays re-runnable.
    assert [t[0] for t in zot.tags] == ["AAAA1111"]


def _read_csv(path: Path) -> list[dict]:
    import csv
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_output_truncation_is_an_error_and_leaves_the_input_column_alone(
    tmp_path,
) -> None:
    """`truncated` in the CSV says the *input* hit the 720k cap. A
    cut-off *answer* is a different fact and must not overwrite it."""
    item = _item()
    rows, _ = _emit([item], tmp_path)
    manifest = tmp_path / "m.jsonl"
    bm.write_manifest(manifest, rows)
    responses = tmp_path / "r.jsonl"
    bm._write_jsonl(responses, [_response(
        rows[0], '{"decision": "inc', call_status="truncated",
        finish_reason="length",
    )])

    out = tmp_path / "log.csv"
    zot = FakeZot()
    fc.apply_responses(
        zot, manifest_path=manifest, manifest_rows=rows,
        responses_path=responses, output_path=out, items=[item],
        fields=FIELDS, csv_columns=COLS, force=False,
        skip_already_tagged=False,
    )
    row = _read_csv(out)[0]
    assert row["decision"] == "error"
    assert "truncated: output hit" in row["reason"]
    assert row["truncated"] == "false"      # the input was not truncated
    assert row["fulltext_chars"] == str(len(FULLTEXT))
    assert zot.tags == []                   # untagged, so a re-run gets it


def test_a_degenerate_run_is_refused_before_anything_is_written(
    tmp_path,
) -> None:
    """240 one-token answers apply cleanly and look like a screening
    pass. The tags they write then make the next run skip them all."""
    item = _item()
    rows, _ = _emit([item], tmp_path)
    manifest = tmp_path / "m.jsonl"
    bm.write_manifest(manifest, rows)
    responses = tmp_path / "r.jsonl"
    resp = _response(rows[0], "", output_tokens=1)
    bm._write_jsonl(responses, [resp])
    bm.run_record_path(responses).write_text(
        json.dumps(bm.summarise_run(
            RUN, stage=bm.STAGE_FULLTEXT, model=MODEL, responses=[resp],
        )), encoding="utf-8",
    )

    out = tmp_path / "log.csv"
    zot = FakeZot()
    with pytest.raises(SystemExit, match="REFUSING"):
        fc.apply_responses(
            zot, manifest_path=manifest, manifest_rows=rows,
            responses_path=responses, output_path=out, items=[item],
            fields=FIELDS, csv_columns=COLS, force=False,
            skip_already_tagged=False,
        )
    assert not out.exists()
    assert zot.tags == []


def test_items_coded_since_the_manifest_was_emitted_can_be_left_alone(
    tmp_path, capsys,
) -> None:
    """Emit and apply can be days apart. Something else may have decided
    the item in between, and overwriting that silently loses it."""
    item = _item(tags=[{"tag": "fulltext:exclude"}])
    rows, _ = _emit([item], tmp_path)
    manifest = tmp_path / "m.jsonl"
    bm.write_manifest(manifest, rows)
    responses = tmp_path / "r.jsonl"
    bm._write_jsonl(responses, [_response(rows[0], COMPLETION)])

    out = tmp_path / "log.csv"
    zot = FakeZot()
    fc.apply_responses(
        zot, manifest_path=manifest, manifest_rows=rows,
        responses_path=responses, output_path=out, items=[item],
        fields=FIELDS, csv_columns=COLS, force=False,
        skip_already_tagged=True,
    )
    assert "tagged fulltext:* since this manifest was emitted" in \
        capsys.readouterr().out
    assert zot.tags == []
    assert not out.exists()


# ---------------------------------------------------------------------------
# --update-fields through the manifest
# ---------------------------------------------------------------------------


def test_update_mode_survives_the_round_trip_with_its_decision_intact(
    tmp_path,
) -> None:
    """The manifest carries `mode` and `target_fields`, so the applier
    takes the merge branch without being told — and the adjudicated
    include decision is preserved, not re-decided by the model."""
    item = _item()
    rows, _ = _emit(
        [item], tmp_path, mode="update_fields", target_fields=["method"],
    )
    req = rows[0]
    assert req["mode"] == "update_fields"
    assert req["target_fields"] == ["method"]

    # An existing note, adjudicated to include with its own reason.
    existing_row = {
        "item_key": "AAAA1111", "decision": "include", "exclusion_code": "",
        "reason": "Adjudicated by hand.", "model": "older/model",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "sample": "hand-checked sample", "method": "stale method",
    }
    zot = FakeZot({"AAAA1111": fc._build_slr_coding_note_html(
        existing_row, FIELDS, "v0-old",
    )})

    manifest = tmp_path / "m.jsonl"
    bm.write_manifest(manifest, rows)
    responses = tmp_path / "r.jsonl"
    # The model answers `exclude` — update mode must ignore that.
    bm._write_jsonl(responses, [_response(req, json.dumps({
        "decision": "exclude", "exclusion_code": "X1", "reason": "no",
        "sample": "model's sample", "method": "fixed method",
    }))])

    out = tmp_path / "log.csv"
    fc.apply_responses(
        zot, manifest_path=manifest, manifest_rows=rows,
        responses_path=responses, output_path=out, items=[item],
        fields=FIELDS, csv_columns=COLS, force=False,
        skip_already_tagged=False,
    )
    row = _read_csv(out)[0]
    assert row["decision"] == "include"          # adjudication survives
    assert row["exclusion_code"] == ""
    assert row["method"] == "fixed method"       # targeted field updated
    assert row["sample"] == "hand-checked sample"  # untargeted preserved
    assert row["reason"].startswith("[UPDATE-FIELDS:method] Adjudicated")
    assert row["timestamp"] == TS                # when it was decided
    assert zot.tags == []                        # update mode never tags
    assert "fixed method" in zot.notes["AAAA1111"]


def test_update_mode_leaves_the_note_alone_when_the_answer_failed(
    tmp_path,
) -> None:
    """Merging an unparseable answer would blank the targeted fields and
    destroy the error reason. The note stays as it was."""
    item = _item()
    rows, _ = _emit(
        [item], tmp_path, mode="update_fields", target_fields=["method"],
    )
    original = fc._build_slr_coding_note_html(
        {"item_key": "AAAA1111", "decision": "include", "reason": "keep",
         "sample": "keep sample", "method": "keep method"},
        FIELDS, "v0-old",
    )
    zot = FakeZot({"AAAA1111": original})

    manifest = tmp_path / "m.jsonl"
    bm.write_manifest(manifest, rows)
    responses = tmp_path / "r.jsonl"
    bm._write_jsonl(responses, [_response(rows[0], "I could not comply.")])

    out = tmp_path / "log.csv"
    fc.apply_responses(
        zot, manifest_path=manifest, manifest_rows=rows,
        responses_path=responses, output_path=out, items=[item],
        fields=FIELDS, csv_columns=COLS, force=False,
        skip_already_tagged=False,
    )
    assert _read_csv(out)[0]["decision"] == "error"
    assert zot.notes["AAAA1111"] == original
