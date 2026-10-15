"""In-process tests for the reference executor, `run_manifest.py` (D2, F4).

It had none, and it held every response in memory until the end: Ctrl+C,
a walltime SIGTERM or an OOM kill lost every generation already paid for.
"""

from __future__ import annotations

import json
import threading

import abstract_screen
import batch_manifest as bm
import pytest
import run_manifest
import tag_prefix

NS = tag_prefix.namespace("test-review")
MODEL = "org/model-1"


def _manifest(tmp_path, n: int = 5):
    items = [
        {"key": f"K{i:07d}", "data": {"key": f"K{i:07d}", "title": f"T{i}",
                                      "abstractNote": "A", "DOI": f"10.1/{i}",
                                      "publicationTitle": "J"}}
        for i in range(n)
    ]
    rows, _ = abstract_screen.build_manifest_rows(
        items, run_id=bm.new_run_id(bm.STAGE_ABSTRACT), system_prompt="S",
        model=MODEL, prompt_version="v", doi_to_query={},
        library={"kind": "user", "id": "1"}, collection="C", ns=NS,
    )
    path = bm.write_manifest(tmp_path / "requests.jsonl", rows)
    return path, bm.read_manifest(path)[1]


class _Client:
    """Answers each prompt; `interrupt_at` raises Ctrl+C on that call."""

    def __init__(self, interrupt_at: int | None = None):
        self.calls = 0
        self.prompts: list[str] = []
        self.lock = threading.Lock()
        self.interrupt_at = interrupt_at

    def generate(self, *, model, system, prompt, temperature, max_tokens):
        with self.lock:
            self.calls += 1
            n = self.calls
            self.prompts.append(prompt)
        if self.interrupt_at is not None and n == self.interrupt_at:
            raise KeyboardInterrupt
        return "DECISION: include\nREASON: fits the criteria well"


@pytest.fixture
def provider(monkeypatch):
    holder: dict = {"client": _Client()}
    lp = run_manifest.llm_provider
    monkeypatch.setattr(lp, "require_credentials", lambda m: None)
    monkeypatch.setattr(lp, "preflight_or_exit", lambda m: None)
    monkeypatch.setattr(lp, "get_provider", lambda m: holder["client"])
    return holder


def _run(manifest, out, *extra) -> int:
    return run_manifest.main([
        "--manifest", str(manifest), "--out", str(out), "--model", MODEL,
        "--workers", "1", *extra,
    ])


def test_a_clean_run_writes_responses_and_record(tmp_path, provider) -> None:
    manifest, rows = _manifest(tmp_path)
    out = tmp_path / "responses.jsonl"
    assert _run(manifest, out) == 0
    _, responses = bm.read_responses(out)
    paired, unanswered, orphaned = bm.join_responses(rows, responses)
    assert (len(paired), unanswered, orphaned) == (5, [], [])
    # Manifest order, and the journal is gone once the final file exists.
    assert [r["request_id"] for r in responses] == [r["request_id"] for r in rows]
    assert not run_manifest.partial_path(out).exists()
    record = json.loads(bm.run_record_path(out).read_text())
    assert record["status_counts"] == {"ok": 5}
    assert record["interrupted"] is False


def test_each_response_is_journalled_as_it_completes(tmp_path, provider) -> None:
    """Hard kill (no cleanup at all): the journal already holds every
    response that completed before it."""
    manifest, rows = _manifest(tmp_path, n=3)
    out = tmp_path / "responses.jsonl"
    seen: list[int] = []

    real = run_manifest.execute_one

    def spy(req, client, model):
        # Before each call, count what the journal holds so far.
        j = run_manifest.partial_path(out)
        seen.append(len(j.read_text().splitlines()) if j.exists() else 0)
        return real(req, client, model)

    run_manifest.execute_one = spy
    try:
        assert _run(manifest, out) == 0
    finally:
        run_manifest.execute_one = real
    assert seen == [0, 1, 2]


def test_ctrl_c_keeps_what_was_paid_for_and_a_rerun_resumes(
    tmp_path, provider,
) -> None:
    manifest, rows = _manifest(tmp_path, n=5)
    out = tmp_path / "responses.jsonl"
    provider["client"] = _Client(interrupt_at=3)
    assert _run(manifest, out) == 130

    _, kept = bm.read_responses(out)
    assert len(kept) == 2                      # calls 1 and 2 completed
    assert json.loads(bm.run_record_path(out).read_text())["interrupted"] is True
    # Apply works on the partial file: the rest is simply unanswered.
    paired, unanswered, _ = bm.join_responses(rows, kept)
    assert (len(paired), len(unanswered)) == (2, 3)

    provider["client"] = second = _Client()
    assert _run(manifest, out) == 0
    assert second.calls == 3                   # only the unanswered ones
    _, responses = bm.read_responses(out)
    paired, unanswered, orphaned = bm.join_responses(rows, responses)
    assert (len(paired), unanswered, orphaned) == (5, [], [])


def test_a_hard_kill_journal_is_resumed_from(tmp_path, provider) -> None:
    """SIGKILL leaves only the journal, possibly with a torn last line."""
    manifest, rows = _manifest(tmp_path, n=4)
    out = tmp_path / "responses.jsonl"
    done = run_manifest.execute_one(rows[0], _Client(), MODEL)
    journal = run_manifest.partial_path(out)
    journal.write_text(json.dumps(done) + "\n" + '{"run_id": "torn', encoding="utf-8")

    provider["client"] = client = _Client()
    assert _run(manifest, out) == 0
    assert client.calls == 3
    _, responses = bm.read_responses(out)
    assert len(responses) == 4


def test_no_resume_asks_again(tmp_path, provider) -> None:
    manifest, _ = _manifest(tmp_path, n=2)
    out = tmp_path / "responses.jsonl"
    assert _run(manifest, out) == 0
    provider["client"] = client = _Client()
    assert _run(manifest, out, "--no-resume") == 0
    assert client.calls == 2


def test_errors_are_not_kept_on_resume(tmp_path) -> None:
    manifest, rows = _manifest(tmp_path, n=2)
    out = tmp_path / "responses.jsonl"
    run_id = rows[0]["run_id"]
    bm._write_jsonl(out, [
        {"run_id": run_id, "model": MODEL, "request_id": rows[0]["request_id"],
         "call_status": "ok"},
        {"run_id": run_id, "model": MODEL, "request_id": rows[1]["request_id"],
         "call_status": "error"},
    ])
    kept = run_manifest.load_previous(out, run_id, MODEL)
    assert set(kept) == {rows[0]["request_id"]}
    # A different model's answers are a different experiment.
    assert run_manifest.load_previous(out, run_id, "other/model") == {}


def test_a_fatal_provider_error_stops_the_run(tmp_path, monkeypatch, provider) -> None:
    manifest, _ = _manifest(tmp_path, n=4)
    out = tmp_path / "responses.jsonl"

    class _Dead:
        calls = 0

        def generate(self, **kw):
            _Dead.calls += 1
            if _Dead.calls > 1:
                # Slow enough that the main thread cancels the queue
                # while this one is in flight.
                threading.Event().wait(0.3)
            raise RuntimeError("quota")

    class _Verdict:
        retryable = False
        detail = "quota"

        class status:  # noqa: N801
            value = "quota_exhausted"

        @staticmethod
        def format():
            return "spent"

    monkeypatch.setattr(run_manifest.llm_provider, "classify_failure",
                        lambda e: _Verdict())
    provider["client"] = _Dead()
    assert _run(manifest, out) == 1
    assert _Dead.calls <= 2   # the first, plus at most one in flight
