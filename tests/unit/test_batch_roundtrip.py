"""A hermetic fake cluster: emit → execute → apply, with no GPU anywhere.

The batch path crosses two boundaries that nothing else in this
repository crosses. Its manifest leaves the machine holding Zotero, and
its runner (`scripts/cluster/run_batch.py`) imports nothing from this
plugin at all — deliberately, because it is copied to a cluster on its
own. So the two halves of the contract are written twice, validated
twice, and connected by nothing but this file.

What makes it hermetic is a fake `vllm` module injected into
`sys.modules`. **The runner under test is the real one**, loaded by file
path exactly as the sbatch wrapper loads it, and it takes the real
`llm.generate` code path down to the last line; only the weights and the
GPU are fake. A stub that re-implemented the runner would pass while the
runner was broken, which is the failure this test exists to prevent.

This is what CI runs. A real job on a real cluster is the `live_cluster`
marker's business, and cannot be a gate.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
import types
from pathlib import Path

import abstract_screen
import batch_manifest as bm
import pytest
from log_schemas import ABSTRACT_SCREENING_FIELDS

REPO = Path(__file__).resolve().parents[2]
RUNNER_PATH = REPO / "scripts" / "cluster" / "run_batch.py"

COLLECTION = "COLL0001"
LIBRARY = {"kind": "group", "id": "123456"}
SYSTEM_PROMPT = "You screen abstracts. Answer with DECISION and REASON."


def _load_runner():
    spec = importlib.util.spec_from_file_location("cluster_run_batch_rt", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


runner = _load_runner()


# ---------------------------------------------------------------------------
# The fake cluster
# ---------------------------------------------------------------------------


class _Tokenizer:
    """Stands in for a real chat template.

    `refuse_system` reproduces the widely-shipped instruction models whose
    templates raise on a system role; `encode` is a deterministic
    character-based stand-in so the context-window arithmetic is
    exercised with numbers a test can reason about.
    """

    def __init__(self, refuse_system: bool = False) -> None:
        self.refuse_system = refuse_system

    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=False):
        if self.refuse_system and any(m["role"] == "system" for m in messages):
            raise ValueError("System role not supported")
        return "<s>" + "\n".join(
            f"[{m['role']}]{m['content']}" for m in messages
        ) + "\n[assistant]"

    def encode(self, text):
        return [0] * max(1, len(text) // 4)


class _Completion:
    def __init__(self, text: str, finish_reason: str = "stop") -> None:
        self.text = text
        self.finish_reason = finish_reason
        # vLLM reports the tokens it actually generated; a character
        # proxy keeps the degeneracy arithmetic honest without a
        # tokenizer, and an empty answer must count as zero.
        self.token_ids = [0] * (len(text) // 4)


class _Output:
    def __init__(self, prompt: str, completion: _Completion) -> None:
        self.prompt_token_ids = [0] * max(1, len(prompt) // 4)
        self.outputs = [completion]


def install_fake_vllm(monkeypatch, answer, *, refuse_system: bool = False,
                      finish=None, version: str = "0.11.0"):
    """Put a fake `vllm` on `sys.modules` and record how it was called.

    `answer` and `finish` are called with each request's index, so a test
    can vary the model's reply and its stop condition per item — which is
    what distinguishes a real screening run from the degenerate one this
    whole path exists to refuse, and a real batch from one where a single
    answer was cut off.
    """
    calls: dict = {"init": None, "params": [], "prompts": []}
    finish = finish or (lambda _i: "stop")

    class _LLM:
        def __init__(self, **kwargs):
            calls["init"] = kwargs

        def get_tokenizer(self):
            return _Tokenizer(refuse_system=refuse_system)

        def generate(self, prompts, params):
            calls["prompts"] = list(prompts)
            calls["params"] = list(params)
            return [
                _Output(p, _Completion(answer(i), finish(i)))
                for i, p in enumerate(prompts)
            ]

    class _SamplingParams:
        def __init__(self, temperature=0.0, seed=None, max_tokens=None):
            self.temperature = temperature
            self.seed = seed
            self.max_tokens = max_tokens

    module = types.ModuleType("vllm")
    module.LLM = _LLM
    module.SamplingParams = _SamplingParams
    module.__version__ = version
    monkeypatch.setitem(sys.modules, "vllm", module)
    return calls


class _FakeZot:
    """Enough Zotero for the applier: collection reads and tag writes."""

    def __init__(self, items=None) -> None:
        self.items = items or []
        self.tag_calls: list[list[tuple[str, dict]]] = []

    def collection_items(self, collection, item_type=None):
        return list(self.items)

    def batch_update_tags(self, updates):
        self.tag_calls.append(list(updates))
        return {"applied": len(updates), "unchanged": 0, "failed": 0}

    @property
    def tags_by_key(self) -> dict:
        return {key: op for call in self.tag_calls for key, op in call}


# ---------------------------------------------------------------------------
# Emit
# ---------------------------------------------------------------------------


def _item(key: str, title: str, abstract: str) -> dict:
    return {
        "key": key,
        "data": {
            "key": key,
            "DOI": f"10.1234/{key.lower()}",
            "title": title,
            "publicationTitle": "Journal of Business Venturing",
            "abstractNote": abstract,
            "date": "2019-03-01",
        },
    }


ITEMS = [
    _item("AAAA1111", "Founder self-efficacy and growth", "We survey 1,243 founders."),
    _item("BBBB2222", "A review of venture capital", "We review 90 papers."),
    _item("CCCC3333", "Team composition and exit", "We follow 300 teams."),
]


def emit(tmp_path: Path, items=None, max_input_chars: int = 0, run_id: str = ""):
    """The plugin's half: assemble a manifest and its sidecar.

    `run_id` is overridable because `new_run_id` is timestamped to the
    second, so two manifests emitted inside one test share an identity —
    which is the one thing the mispairing guard needs them not to.
    """
    run_id = run_id or bm.new_run_id(bm.STAGE_ABSTRACT)
    rows, skipped = abstract_screen.build_manifest_rows(
        list(items or ITEMS),
        run_id=run_id,
        system_prompt=SYSTEM_PROMPT,
        model="org/model-1",
        prompt_version="v1-test",
        doi_to_query={},
        library=LIBRARY,
        collection=COLLECTION,
        max_input_chars=max_input_chars,
    )
    manifest = tmp_path / "requests.jsonl"
    bm.write_manifest(manifest, rows)
    bm.write_skipped(
        bm.skipped_path(manifest),
        run_id=run_id,
        stage=bm.STAGE_ABSTRACT,
        skipped=skipped,
        selection={"collection": COLLECTION},
        n_requested=len(items or ITEMS),
    )
    return manifest, rows


DECISIONS = {
    0: "DECISION: include\nREASON: Reports all three inclusion criteria.",
    1: "DECISION: exclude\nREASON: A review, not a primary study.",
    2: "DECISION: borderline\nREASON: Sample described only in the methods.",
}


def run_cluster(tmp_path: Path, manifest: Path, argv_extra=(),
                max_model_len: int = 4096) -> Path:
    """The cluster's half, run for real against a fake GPU."""
    assert runner.main([
        "--manifest", str(manifest),
        "--model", "org/model-1",
        "--out-dir", str(tmp_path / "results"),
        "--max-model-len", str(max_model_len),
        "--execute", "--confirm",
        *argv_extra,
    ]) == 0
    produced = sorted((tmp_path / "results").glob("*.responses.jsonl"))
    assert len(produced) == 1, produced
    return produced[0]


# ---------------------------------------------------------------------------
# The round trip
# ---------------------------------------------------------------------------


def test_emit_execute_apply(tmp_path, monkeypatch) -> None:
    """The whole loop, with only the weights faked.

    Three papers in, three decisions out, three Zotero tags written and
    three CSV rows logged — and every field in them derived from a file
    that crossed a machine boundary in each direction.
    """
    calls = install_fake_vllm(monkeypatch, lambda i: DECISIONS[i])
    manifest, rows = emit(tmp_path)
    responses_path = run_cluster(tmp_path, manifest)

    # The runner asked for what the manifest specified.
    assert calls["init"]["model"] == "org/model-1"
    assert calls["init"]["max_model_len"] == 4096
    assert calls["init"]["enable_prefix_caching"] is True
    assert calls["init"]["trust_remote_code"] is False
    # One generate() call over the whole manifest, one budget per request.
    assert [p.max_tokens for p in calls["params"]] == [200, 200, 200]
    assert {p.seed for p in calls["params"]} == {42}

    header, responses = bm.read_responses(responses_path)
    assert header["n_responses"] == 3
    assert {r["call_status"] for r in responses} == {"ok"}
    assert {r["prompt_format"] for r in responses} == {"chat_template"}

    # The plugin's own reader accepts it, and the join is exact.
    paired, unanswered, orphaned = bm.join_responses(rows, responses)
    assert (len(paired), unanswered, orphaned) == (3, [], [])

    zot = _FakeZot(items=[])
    output = tmp_path / "screening" / "abstract_screening.csv"
    assert abstract_screen.apply_responses(
        zot,
        manifest_path=manifest,
        responses_path=responses_path,
        output_path=output,
        tag_batch_size=50,
        force=False,
        skip_already_tagged=False,
    ) == 0

    logged = _read_csv(output)
    assert {row["item_key"]: row["decision"] for row in logged} == {
        "AAAA1111": "include",
        "BBBB2222": "exclude",
        "CCCC3333": "borderline",
    }
    assert zot.tags_by_key == {
        "AAAA1111": {"add": ["abstract:include"], "remove_prefixed": ["abstract:"]},
        "BBBB2222": {"add": ["abstract:exclude"], "remove_prefixed": ["abstract:"]},
        "CCCC3333": {"add": ["abstract:borderline"], "remove_prefixed": ["abstract:"]},
    }


def test_the_csv_records_the_model_that_actually_ran(tmp_path, monkeypatch) -> None:
    """Not the manifest's hint — the model the cluster loaded.

    The CSV's `model` column is what the systematic-review skill declares
    a manuscript cites, and a manifest assembled with one model in mind
    can be executed with another. Whatever the GPU node actually loaded
    is what happened.
    """
    install_fake_vllm(monkeypatch, lambda i: DECISIONS[0])
    manifest, _ = emit(tmp_path, items=ITEMS[:1])
    assert runner.main([
        "--manifest", str(manifest), "--model", "org/actually-ran",
        "--out-dir", str(tmp_path / "results"),
        "--max-model-len", "4096", "--execute", "--confirm",
    ]) == 0
    responses_path = next((tmp_path / "results").glob("*.responses.jsonl"))

    zot = _FakeZot()
    output = tmp_path / "log.csv"
    abstract_screen.apply_responses(
        zot, manifest_path=manifest, responses_path=responses_path,
        output_path=output, tag_batch_size=50, force=False,
        skip_already_tagged=False,
    )
    assert _read_csv(output)[0]["model"] == "org/actually-ran"


def test_the_timestamp_is_when_the_model_answered(tmp_path, monkeypatch) -> None:
    """Not when the file was applied, which can be days later.

    The column claims to record when a decision was made, and on this
    path those two moments are separated by a queue wait, a job and a
    file copy.
    """
    install_fake_vllm(monkeypatch, lambda i: DECISIONS[0])
    manifest, _ = emit(tmp_path, items=ITEMS[:1])
    responses_path = run_cluster(tmp_path, manifest)
    _, responses = bm.read_responses(responses_path)

    zot = _FakeZot()
    output = tmp_path / "log.csv"
    abstract_screen.apply_responses(
        zot, manifest_path=manifest, responses_path=responses_path,
        output_path=output, tag_batch_size=50, force=False,
        skip_already_tagged=False,
    )
    assert _read_csv(output)[0]["timestamp"] == responses[0]["generated_at"]


def test_the_batch_path_writes_what_the_synchronous_path_writes(
    tmp_path, monkeypatch,
) -> None:
    """Same item, same model text, same CSV row.

    `test_batch_manifest.py` pins this for the emit/apply seam. Here it is
    pinned across the *cluster* runner as well, because the runner is a
    third implementation of "turn a completion into a response row" and
    it lives in a file that cannot import the other two.
    """
    completion = DECISIONS[0]
    install_fake_vllm(monkeypatch, lambda i: completion)
    manifest, _ = emit(tmp_path, items=ITEMS[:1])
    responses_path = run_cluster(tmp_path, manifest)
    _, responses = bm.read_responses(responses_path)

    zot = _FakeZot()
    output = tmp_path / "log.csv"
    abstract_screen.apply_responses(
        zot, manifest_path=manifest, responses_path=responses_path,
        output_path=output, tag_batch_size=50, force=False,
        skip_already_tagged=False,
    )
    via_cluster = _read_csv(output)[0]

    decision, reason = abstract_screen.parse_decision(completion)
    synchronous = abstract_screen.screening_row(
        ITEMS[0], decision=decision, reason=reason, model="org/model-1",
        prompt_version="v1-test", query="",
        timestamp=responses[0]["generated_at"],
    )
    for field in ABSTRACT_SCREENING_FIELDS:
        assert via_cluster[field] == str(synchronous.get(field, "")), field


# ---------------------------------------------------------------------------
# The refusals — the failures that otherwise look like successes
# ---------------------------------------------------------------------------


def test_a_degenerate_run_is_flagged_by_the_runner_and_refused_by_the_applier(
    tmp_path, monkeypatch, capsys,
) -> None:
    """The failure mode that looks healthy.

    An instruction-tuned model handed an unframed prompt emits one
    end-of-turn token: three requests in, three responses out, nothing in
    the file saying the run was worthless. Applied, it writes three
    `borderline` tags — and because tags are the resume source of truth,
    the next run skips those items forever.
    """
    install_fake_vllm(monkeypatch, lambda i: "")
    manifest, _ = emit(tmp_path)
    responses_path = run_cluster(tmp_path, manifest)

    record = bm.load_run_record(bm.run_record_path(responses_path))
    assert record["degenerate_output"] is True
    assert record["status_counts"] == {"empty": 3}
    assert "WARNING" in capsys.readouterr().out

    zot = _FakeZot()
    output = tmp_path / "log.csv"
    with pytest.raises(SystemExit, match="REFUSING to apply"):
        abstract_screen.apply_responses(
            zot, manifest_path=manifest, responses_path=responses_path,
            output_path=output, tag_batch_size=50, force=False,
            skip_already_tagged=False,
        )
    assert not output.exists()
    assert zot.tag_calls == []


def test_the_runner_records_a_degenerate_run_rather_than_deleting_it(
    tmp_path, monkeypatch,
) -> None:
    """It exits 0 and writes both files.

    The responses are real evidence about a real run and belong on disk;
    a runner that deleted them would leave a user with a failed job and
    nothing to diagnose it from. Refusing is the applier's job, and it
    refuses on the flag this run wrote.
    """
    install_fake_vllm(monkeypatch, lambda i: "")
    manifest, _ = emit(tmp_path)
    responses_path = run_cluster(tmp_path, manifest)
    assert responses_path.exists()
    assert Path(bm.run_record_path(responses_path)).exists()


def test_a_truncated_answer_becomes_an_error_not_a_decision(
    tmp_path, monkeypatch,
) -> None:
    """A cut-off answer is a run defect, not a verdict.

    Recorded as `error`, the item stays untagged and a re-run picks it
    up. Recorded as whatever the half-answer parsed to, it would be a
    decision nobody made.
    """
    install_fake_vllm(
        monkeypatch,
        lambda i: DECISIONS[0] if i == 0 else "DECISION: inc",
        finish=lambda i: "stop" if i == 0 else "length",
    )
    manifest, _ = emit(tmp_path, items=ITEMS[:2])
    responses_path = run_cluster(tmp_path, manifest)

    _, responses = bm.read_responses(responses_path)
    assert [r["call_status"] for r in responses] == ["ok", "truncated"]

    zot = _FakeZot()
    output = tmp_path / "log.csv"
    abstract_screen.apply_responses(
        zot, manifest_path=manifest, responses_path=responses_path,
        output_path=output, tag_batch_size=50, force=False,
        skip_already_tagged=False,
    )
    rows = {r["item_key"]: r for r in _read_csv(output)}
    assert rows["BBBB2222"]["decision"] == "error"
    assert "truncated" in rows["BBBB2222"]["reason"]
    # Untagged, so a re-run re-screens it; the item that answered is tagged.
    assert list(zot.tags_by_key) == ["AAAA1111"]


def test_a_run_where_every_answer_was_cut_off_is_refused(
    tmp_path, monkeypatch,
) -> None:
    """No answered request means no evidence the model can answer at all.

    The degeneracy check averages over `ok` responses only, so a batch
    with none of them scores zero and the applier refuses. That is the
    right call: an all-truncated pass says the output budget is wrong,
    not that three papers were undecidable, and applying it would tag
    every item `error` and leave a user believing the model was tried.
    """
    install_fake_vllm(
        monkeypatch, lambda i: "DECISION: inc", finish=lambda i: "length",
    )
    manifest, _ = emit(tmp_path)
    responses_path = run_cluster(tmp_path, manifest)

    record = bm.load_run_record(bm.run_record_path(responses_path))
    assert record["status_counts"] == {"truncated": 3}
    assert record["degenerate_output"] is True

    zot = _FakeZot()
    with pytest.raises(SystemExit, match="REFUSING to apply"):
        abstract_screen.apply_responses(
            zot, manifest_path=manifest, responses_path=responses_path,
            output_path=tmp_path / "log.csv", tag_batch_size=50, force=False,
            skip_already_tagged=False,
        )


def test_a_request_too_long_for_the_context_is_recorded_not_sent(
    tmp_path, monkeypatch,
) -> None:
    """It fails against its own item, and the rest of the batch still runs.

    Sent, it fails inside vLLM where the traceback reads as a model
    problem — and this is the single largest technical risk in the batch
    path, because an emit-time character cap and a serving-time token
    cap are different numbers.
    """
    calls = install_fake_vllm(monkeypatch, lambda i: DECISIONS[0])
    long_item = _item("DDDD4444", "A very long abstract", "word " * 4000)
    manifest, _ = emit(tmp_path, items=[ITEMS[0], long_item])
    responses_path = run_cluster(tmp_path, manifest)

    # One of the two never reached the model.
    assert len(calls["prompts"]) == 1

    _, responses = bm.read_responses(responses_path)
    by_key = {r["item_key"]: r for r in responses}
    assert by_key["AAAA1111"]["call_status"] == "ok"
    assert by_key["DDDD4444"]["call_status"] == "error"
    assert "--max-input-chars" in by_key["DDDD4444"]["error"]

    zot = _FakeZot()
    output = tmp_path / "log.csv"
    abstract_screen.apply_responses(
        zot, manifest_path=manifest, responses_path=responses_path,
        output_path=output, tag_batch_size=50, force=False,
        skip_already_tagged=False,
    )
    rows = {r["item_key"]: r for r in _read_csv(output)}
    assert rows["AAAA1111"]["decision"] == "include"
    assert rows["DDDD4444"]["decision"] == "error"
    assert list(zot.tags_by_key) == ["AAAA1111"]


def test_nothing_that_fits_is_a_refusal_to_run(tmp_path, monkeypatch) -> None:
    """Spending a GPU allocation to fail every request is not a run."""
    install_fake_vllm(monkeypatch, lambda i: DECISIONS[0])
    manifest, _ = emit(tmp_path, items=[_item("EEEE5555", "Long", "word " * 4000)])
    with pytest.raises(SystemExit, match="not one of"):
        run_cluster(tmp_path, manifest)


def test_a_model_that_refuses_a_system_role_still_gets_a_framed_prompt(
    tmp_path, monkeypatch,
) -> None:
    """Several widely-used open-weight instruction models do exactly this.

    The system prompt is the screening criteria. Dropping it — or falling
    through to a raw completion — is the difference between a screening
    run and 240 empty answers that every row reports as `ok`.
    """
    calls = install_fake_vllm(
        monkeypatch, lambda i: DECISIONS[0], refuse_system=True,
    )
    manifest, _ = emit(tmp_path, items=ITEMS[:1])
    responses_path = run_cluster(tmp_path, manifest)

    assert SYSTEM_PROMPT in calls["prompts"][0]
    _, responses = bm.read_responses(responses_path)
    assert responses[0]["prompt_format"] == "chat_template_merged_system"
    assert responses[0]["call_status"] == "ok"


def test_the_sidecar_still_owes_the_log_a_row(tmp_path, monkeypatch) -> None:
    """A shrunken N must be visible.

    An item dropped at emit time never appears in the responses file, so
    the only record that it existed is the sidecar — and "we screened 2
    of 3" is a finding where "we screened 2" is a misreport.
    """
    install_fake_vllm(monkeypatch, lambda i: DECISIONS[0])
    long_item = _item("FFFF6666", "Long", "word " * 4000)
    manifest, rows = emit(
        tmp_path, items=[ITEMS[0], long_item], max_input_chars=2000,
    )
    assert len(rows) == 1  # the long one went to the sidecar, not the cluster

    sidecar = bm.read_skipped(bm.skipped_path(manifest))
    assert sidecar["reason_counts"] == {"too_long_for_context": 1}

    responses_path = run_cluster(tmp_path, manifest)
    zot = _FakeZot()
    output = tmp_path / "log.csv"
    abstract_screen.apply_responses(
        zot, manifest_path=manifest, responses_path=responses_path,
        output_path=output, tag_batch_size=50, force=False,
        skip_already_tagged=False,
    )
    # `too_long_for_context` is not one of the skip reasons that owes a
    # CSV row — the item was never attempted — but the sidecar records it.
    assert [r["item_key"] for r in _read_csv(output)] == ["AAAA1111"]


def test_a_partial_run_leaves_the_rest_re_runnable(tmp_path, monkeypatch) -> None:
    """A job that runs out of walltime answers some of its manifest.

    Those responses are worth applying; the rest must stay untagged so
    the next manifest picks them up. `--limit` produces the same shape,
    which is also what a pilot run looks like.
    """
    install_fake_vllm(monkeypatch, lambda i: DECISIONS[0])
    manifest, rows = emit(tmp_path)
    responses_path = run_cluster(tmp_path, manifest, argv_extra=("--limit", "1"))

    _, responses = bm.read_responses(responses_path)
    assert len(responses) == 1
    _, unanswered, _ = bm.join_responses(rows, responses)
    assert len(unanswered) == 2

    zot = _FakeZot()
    output = tmp_path / "log.csv"
    abstract_screen.apply_responses(
        zot, manifest_path=manifest, responses_path=responses_path,
        output_path=output, tag_batch_size=50, force=False,
        skip_already_tagged=False,
    )
    assert len(zot.tags_by_key) == 1


def test_responses_from_another_run_are_refused(tmp_path, monkeypatch) -> None:
    """Two manifests and two response files are easy to mispair by hand.

    Applied together they would tag one run's items with another run's
    decisions, and nothing downstream could tell.
    """
    install_fake_vllm(monkeypatch, lambda i: DECISIONS[0])
    first, _ = emit(tmp_path / "a", items=ITEMS[:1], run_id="run-monday")
    second, _ = emit(tmp_path / "b", items=ITEMS[:1], run_id="run-tuesday")
    other_responses = run_cluster(tmp_path / "b", second)

    with pytest.raises(bm.ManifestError, match="run_id mismatch"):
        abstract_screen.apply_responses(
            _FakeZot(), manifest_path=first, responses_path=other_responses,
            output_path=tmp_path / "log.csv", tag_batch_size=50, force=False,
            skip_already_tagged=False,
        )


# ---------------------------------------------------------------------------
# Full-text coding shares the runner
# ---------------------------------------------------------------------------


def test_a_full_text_manifest_keeps_its_larger_budget(tmp_path, monkeypatch) -> None:
    """One flat budget for the batch would truncate every coding response.

    An abstract decision is 200 tokens; a coding response over a
    multi-field schema is thousands. The budget travels on the row, and
    the runner has to honour it per request — a truncated coding response
    is unparseable, so it scores as a schema failure rather than as the
    budget failure it is.
    """
    calls = install_fake_vllm(monkeypatch, lambda i: '{"design": "survey"}')
    run_id = bm.new_run_id(bm.STAGE_FULLTEXT)
    rows = [
        {
            "schema_version": bm.SCHEMA_VERSION,
            "run_id": run_id,
            "request_id": bm.request_id(run_id, "AAAA1111"),
            "ordinal": 0,
            "item_key": "AAAA1111",
            "stage": bm.STAGE_FULLTEXT,
            "mode": "code",
            "coding_fields": [{"name": "design", "description": "Study design"}],
            "system": SYSTEM_PROMPT,
            "user": "FULL TEXT",
            "temperature": 0.0,
            "max_output_tokens": 4096,
            "input_chars": 9,
        },
    ]
    manifest = tmp_path / "coding.jsonl"
    bm.write_manifest(manifest, rows)
    responses_path = run_cluster(tmp_path, manifest, max_model_len=32768)

    assert [p.max_tokens for p in calls["params"]] == [4096]
    _, responses = bm.read_responses(responses_path)
    assert responses[0]["response_text"] == '{"design": "survey"}'
    # And the coding schema the manifest froze is untouched by the round
    # trip: the applier reads it from the manifest, not from a config
    # file that may have moved on.
    _, read_back = bm.read_manifest(manifest)
    assert read_back[0]["coding_fields"] == rows[0]["coding_fields"]


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_the_run_record_carries_what_sizes_the_next_run(
    tmp_path, monkeypatch,
) -> None:
    """Load time separate from generate time, and the manifest's digest.

    Loading weights takes minutes and generating for a small batch takes
    seconds, so the ratio is the number that tells a user how big to make
    the next manifest. The digest is what proves which manifest this was.
    """
    install_fake_vllm(monkeypatch, lambda i: DECISIONS[i], version="0.11.0")
    manifest, _ = emit(tmp_path)
    responses_path = run_cluster(tmp_path, manifest)
    record = json.loads(
        Path(bm.run_record_path(responses_path)).read_text(encoding="utf-8"),
    )

    assert "model_load_s" in record and "generate_s" in record
    assert record["manifest_sha256"] == bm.file_sha256(manifest)
    assert record["n_requests"] == 3
    assert record["n_sent"] == 3
    assert record["executor"] == "run_batch.py"
    assert record["vllm_version"] == "0.11.0"
    assert record["max_model_len"] == 4096
    assert record["seed"] == 42
    assert record["prompt_format"] == "chat_template"
    assert not record.get("degenerate_output")


def test_a_gzipped_manifest_makes_the_same_trip(tmp_path, monkeypatch) -> None:
    """~170 MB of full-text prompts is worth compressing before it moves."""
    import gzip

    install_fake_vllm(monkeypatch, lambda i: DECISIONS[i])
    manifest, rows = emit(tmp_path)
    packed = tmp_path / "requests.jsonl.gz"
    with gzip.open(packed, "wt", encoding="utf-8") as fh:
        fh.write(manifest.read_text(encoding="utf-8"))

    responses_path = run_cluster(tmp_path, packed)
    _, responses = bm.read_responses(responses_path)
    assert len(responses) == 3
    _, unanswered, orphaned = bm.join_responses(rows, responses)
    assert (unanswered, orphaned) == ([], [])


def _read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))
