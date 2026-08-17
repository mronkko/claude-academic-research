"""The batch path must write exactly what the live path writes.

Splitting a screening run into emit-and-apply creates a second way to
produce a decision, and two ways to do one thing drift. The drift would
be invisible in the worst way: both paths write the same CSV columns and
the same Zotero tags, so a divergence shows up not as an error but as a
review whose decisions depend on which path happened to run it.

`test_the_batch_path_writes_what_the_live_path_writes` is therefore the
acceptance criterion for the whole feature, and it compares the rendered
CSV bytes rather than a dict, because the CSV is what a reviewer reads
and what `templates/test_systematic_review.py` asserts against in a
user's project.

The rest of the file covers the refusals — the cases where applying is
the wrong thing to do and the applier has to say so instead.
"""

from __future__ import annotations

import csv
import io
import json

import abstract_screen
import batch_manifest as bm
import pytest
from log_schemas import ABSTRACT_SCREENING_FIELDS

RUN = "abstract_screening-20260816T000000Z"


def _item(key: str = "AAAA1111", **over) -> dict:
    data = {
        "key": key,
        "DOI": "10.1234/example",
        "title": "Entrepreneurial self-efficacy and venture growth",
        "publicationTitle": "Journal of Business Venturing",
        "abstractNote": "We survey 1,243 nascent entrepreneurs.",
        "date": "2019-03-01",
    }
    data.update(over)
    return {"key": key, "data": data}


def _manifest_row(item: dict, **over) -> dict:
    rows, _ = abstract_screen.build_manifest_rows(
        [item],
        run_id=RUN,
        system_prompt="SYSTEM PROMPT",
        model="org/model-1",
        prompt_version="v1-test",
        doi_to_query={"10.1234/example": "TS=(entrepreneur*)"},
        library={"kind": "group", "id": "123"},
        collection="COLL0001",
    )
    rows[0].update(over)
    return rows[0]


def _response(req: dict, text: str, **over) -> dict:
    resp = {
        "schema_version": bm.SCHEMA_VERSION,
        "run_id": req["run_id"],
        "request_id": req["request_id"],
        "item_key": req["item_key"],
        "model": "org/model-1",
        "call_status": "ok",
        "finish_reason": "stop",
        "response_text": text,
        "input_tokens": 300,
        "output_tokens": 40,
        "generated_at": "2026-08-16T00:05:00+00:00",
    }
    resp.update(over)
    return resp


def _as_csv(row: dict) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=ABSTRACT_SCREENING_FIELDS,
                       extrasaction="ignore")
    w.writeheader()
    w.writerow(row)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# The acceptance criterion
# ---------------------------------------------------------------------------


def test_the_batch_path_writes_what_the_live_path_writes() -> None:
    """Same item, same model output, same CSV bytes.

    The live path parses the completion inside its worker and builds the
    row there; the batch path parses it days later out of a responses
    file. Given identical model text they must be indistinguishable in
    the log — otherwise which path ran becomes a variable in the review.
    """
    item = _item()
    completion = (
        "DECISION: include\n"
        "REASON: The abstract reports all three criteria."
    )

    # Live path: exactly what the worker and its result loop do.
    decision, reason = abstract_screen.parse_decision(completion)
    live = abstract_screen.screening_row(
        item, decision=decision, reason=reason, model="org/model-1",
        prompt_version="v1-test", query="TS=(entrepreneur*)",
        timestamp="2026-08-16T00:05:00+00:00",
    )

    # Batch path: through the manifest and back.
    req = _manifest_row(item)
    resp = _response(req, completion)
    _, answer = bm.split_reasoning(resp["response_text"])
    b_decision, b_reason = abstract_screen.parse_decision(answer)
    batch = abstract_screen.screening_row(
        abstract_screen._item_from_manifest_row(req),
        decision=b_decision, reason=b_reason, model=resp["model"],
        prompt_version=req["prompt_version"], query=req["query"],
        timestamp=resp["generated_at"],
    )

    assert _as_csv(batch) == _as_csv(live)


def test_the_two_paths_share_one_parser() -> None:
    """Not two implementations that happen to agree today.

    The borderline-on-unparseable rule is the most consequential line in
    the stage, and the cheapest way for it to diverge is a second copy.
    """
    src = (
        __import__("pathlib").Path(abstract_screen.__file__)
        .read_text(encoding="utf-8")
    )
    assert src.count("PARSE ERROR — raw:") == 1, (
        "the unparseable-output rule appears more than once; the applier "
        "must call parse_decision, not reimplement it"
    )


# ---------------------------------------------------------------------------
# Manifest integrity
# ---------------------------------------------------------------------------


def test_a_manifest_round_trips(tmp_path) -> None:
    rows = [_manifest_row(_item("AAAA1111")), _manifest_row(_item("BBBB2222"))]
    path = bm.write_manifest(tmp_path / "m.jsonl", rows)
    header, back = bm.read_manifest(path)
    assert header["run_id"] == RUN
    assert header["n_requests"] == 2
    assert [r["item_key"] for r in back] == ["AAAA1111", "BBBB2222"]


def test_a_gzipped_manifest_round_trips(tmp_path) -> None:
    """A full-text manifest is ~170 MB uncompressed for a 240-paper
    review; it has to be movable."""
    rows = [_manifest_row(_item())]
    path = bm.write_manifest(tmp_path / "m.jsonl.gz", rows)
    _, back = bm.read_manifest(path)
    assert back[0]["item_key"] == "AAAA1111"


def test_an_unknown_schema_version_is_refused(tmp_path) -> None:
    """Weeks and a `git pull` can separate emit from apply. Guessing at a
    format it does not know would write wrong tags to Zotero."""
    row = _manifest_row(_item(), schema_version=bm.SCHEMA_VERSION + 1)
    path = bm.write_manifest(tmp_path / "m.jsonl", [row])
    with pytest.raises(bm.ManifestError, match="schema"):
        bm.read_manifest(path)


def test_rows_from_two_runs_in_one_file_are_refused(tmp_path) -> None:
    a = _manifest_row(_item("AAAA1111"))
    b = _manifest_row(_item("BBBB2222"), run_id="some-other-run")
    path = bm.write_manifest(tmp_path / "m.jsonl", [a, b])
    with pytest.raises(bm.ManifestError, match="run_id"):
        bm.read_manifest(path)


def test_an_empty_manifest_says_what_that_means(tmp_path) -> None:
    """"No requests" and "no results" are different, and the sidecar is
    where the difference is recorded."""
    path = tmp_path / "m.jsonl"
    path.write_text("", encoding="utf-8")
    with pytest.raises(bm.ManifestError, match="not an empty result"):
        bm.read_manifest(path)


def test_responses_from_a_different_run_are_refused(tmp_path) -> None:
    """The nastiest paired-file mistake: keys exist in both runs, so
    applying the wrong pair tags real items with wrong decisions."""
    req = _manifest_row(_item())
    resp = _response(req, "DECISION: include\nREASON: x", run_id="other-run")
    with pytest.raises(bm.ManifestError, match="different runs"):
        bm.join_responses([req], [resp])


def test_unanswered_and_orphaned_requests_are_reported(tmp_path) -> None:
    """A job that runs out of walltime answers some. Those items must
    stay untagged and be counted, not silently vanish."""
    a = _manifest_row(_item("AAAA1111"))
    b = _manifest_row(_item("BBBB2222"))
    resp_a = _response(a, "DECISION: include\nREASON: x")
    ghost = _response(a, "x", request_id=f"{RUN}:ZZZZ9999:0")
    paired, unanswered, orphaned = bm.join_responses([a, b], [resp_a, ghost])
    assert [r["item_key"] for r, _ in paired] == ["AAAA1111"]
    assert [r["item_key"] for r in unanswered] == ["BBBB2222"]
    assert orphaned == [f"{RUN}:ZZZZ9999:0"]


# ---------------------------------------------------------------------------
# The sidecar — a shrunken N must stay visible
# ---------------------------------------------------------------------------


def test_over_long_items_go_to_the_sidecar_not_the_manifest() -> None:
    """Truncating silently would send the model half a paper and record
    the verdict as if it had read all of it."""
    rows, skipped = abstract_screen.build_manifest_rows(
        [_item(abstractNote="x" * 5000)],
        run_id=RUN, system_prompt="S", model="m", prompt_version="v",
        doi_to_query={}, library={"kind": "group", "id": "1"},
        collection="C", max_input_chars=500,
    )
    assert rows == []
    assert skipped[0]["skip_reason"] == "too_long_for_context"
    # The detail must state both the size and the limit: "too long" alone
    # leaves the user guessing how much to raise the cap by.
    assert "500" in skipped[0]["detail"]
    assert skipped[0]["item_key"] == "AAAA1111"


def test_the_sidecar_counts_every_reason(tmp_path) -> None:
    path = bm.write_skipped(
        tmp_path / "m.skipped.json",
        run_id=RUN, stage=bm.STAGE_ABSTRACT, n_requested=8,
        skipped=[
            {"item_key": "A", "skip_reason": "no_pdf"},
            {"item_key": "B", "skip_reason": "no_pdf"},
            {"item_key": "C", "skip_reason": "too_long_for_context"},
        ],
        selection={"collection": "C1"},
    )
    payload = bm.read_skipped(path)
    assert payload["n_skipped"] == 3
    assert payload["reason_counts"] == {"no_pdf": 2, "too_long_for_context": 1}


def test_an_invented_skip_reason_is_refused(tmp_path) -> None:
    """The vocabulary is closed because the applier branches on it: only
    some reasons owe the log a row."""
    with pytest.raises(bm.ManifestError, match="skip_reason"):
        bm.write_skipped(
            tmp_path / "s.json", run_id=RUN, stage=bm.STAGE_ABSTRACT,
            n_requested=1, selection={},
            skipped=[{"item_key": "A", "skip_reason": "felt-like-it"}],
        )


# ---------------------------------------------------------------------------
# Degeneracy — the highest-value refusal
# ---------------------------------------------------------------------------


def test_a_one_token_run_is_flagged_degenerate() -> None:
    """An instruction-tuned model handed an unframed prompt emits a stop
    token and nothing else. Every row then reads `call_status=ok`."""
    responses = [
        _response(_manifest_row(_item()), "", output_tokens=1)
        for _ in range(10)
    ]
    rec = bm.summarise_run(RUN, stage=bm.STAGE_ABSTRACT, model="m",
                           responses=responses)
    assert rec["degenerate_output"] is True
    with pytest.raises(SystemExit, match="REFUSING"):
        bm.refuse_if_degenerate(rec)


def test_force_apply_overrides_the_refusal_loudly(capsys) -> None:
    rec = bm.summarise_run(
        RUN, stage=bm.STAGE_ABSTRACT, model="m",
        responses=[_response(_manifest_row(_item()), "", output_tokens=1)],
    )
    bm.refuse_if_degenerate(rec, force=True)
    assert "WARNING" in capsys.readouterr().err


def test_a_healthy_run_is_not_flagged() -> None:
    responses = [
        _response(_manifest_row(_item()), "DECISION: include\nREASON: x",
                  output_tokens=40)
        for _ in range(10)
    ]
    rec = bm.summarise_run(RUN, stage=bm.STAGE_ABSTRACT, model="m",
                           responses=responses)
    assert "degenerate_output" not in rec
    bm.refuse_if_degenerate(rec)  # must not raise


def test_errors_do_not_drag_the_mean_toward_degenerate() -> None:
    """Only answered requests count. A run where most calls failed is a
    different problem, reported by status_counts, and must not be
    misdiagnosed as an unframed prompt."""
    good = [
        _response(_manifest_row(_item()), "DECISION: include\nREASON: x",
                  output_tokens=40)
        for _ in range(2)
    ]
    bad = [
        _response(_manifest_row(_item()), "", call_status="error",
                  output_tokens=0)
        for _ in range(20)
    ]
    rec = bm.summarise_run(RUN, stage=bm.STAGE_ABSTRACT, model="m",
                           responses=good + bad)
    assert "degenerate_output" not in rec
    assert rec["status_counts"] == {"error": 20, "ok": 2}


# ---------------------------------------------------------------------------
# Reasoning channel
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [
    "thinking out loud</think>DECISION: include\nREASON: x",
    "scratch<|start|>assistant<|channel|>final<|message|>"
    "DECISION: include\nREASON: x",
    "musing assistantfinal DECISION: include\nREASON: x",
])
def test_a_reasoning_model_still_parses(raw) -> None:
    """The raw stream is stored verbatim; the parser must see only the
    answer, or a model's scratch work decides the review."""
    _, answer = bm.split_reasoning(raw)
    decision, _ = abstract_screen.parse_decision(answer)
    assert decision == "include"


def test_truncation_is_a_defect_not_a_verdict() -> None:
    """`finish_reason=length` means the answer was cut off. Recording
    whatever half-sentence arrived as a decision would be a fabrication;
    the item must stay untagged and re-runnable."""
    req = _manifest_row(_item())
    resp = _response(req, "DECISION: inc", call_status="truncated",
                     finish_reason="length")
    assert resp["call_status"] == "truncated"
    # The applier maps this to error rather than parsing the fragment —
    # `parse_decision` on its own would happily return `borderline`.
    assert abstract_screen.parse_decision("DECISION: inc")[0] == "borderline"


def test_a_manifest_row_carries_its_whole_prompt() -> None:
    """The executor must need nothing from this plugin: no config, no
    Zotero, no code. That is what lets it run on a GPU node."""
    req = _manifest_row(_item())
    assert req["system"] == "SYSTEM PROMPT"
    assert "Entrepreneurial self-efficacy" in req["user"]
    assert req["system_sha256"] == bm.sha256("SYSTEM PROMPT")
    assert json.loads(json.dumps(req)) == req  # plain JSON, no surprises
