"""The Batches API executor: one paid submission, rows in run_manifest's shape."""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

import batch_manifest
import pytest
import run_manifest_batch as rmb

MODEL = "claude-opus-5-5"


def _req(i: int, budget: int = 4000) -> dict:
    return {"run_id": "run1", "request_id": f"run1:K{i}:0", "item_key": f"K{i}",
            "system": "sys", "user": f"paper {i}", "max_output_tokens": budget}


def _msg(text: str, stop: str = "end_turn", out: int = 900) -> NS:
    return NS(content=[NS(type="thinking", thinking=""), NS(type="text", text=text)],
              stop_reason=stop, usage=NS(input_tokens=50_000, output_tokens=out))


class _Batches:
    def __init__(self, results):
        self.created: list[list[dict]] = []
        self._results = results

    def create(self, requests):
        self.created.append(requests)
        return NS(id="msgbatch_1")

    def retrieve(self, batch_id):
        return NS(processing_status="ended", created_at="t0", ended_at="t1",
                  request_counts=NS(succeeded=1, errored=0, processing=0))

    def results(self, batch_id):
        return iter(self._results)


def _client(results=()):
    return NS(messages=NS(batches=_Batches(list(results))))


def test_custom_ids_satisfy_the_api_pattern() -> None:
    # request_id has colons; the Batches API allows [a-zA-Z0-9_-]{1,64}.
    assert ":" in _req(1)["request_id"]
    assert rmb.custom_id(12).isalnum() and len(rmb.custom_id(12)) <= 64


def test_a_claude_5_request_gets_the_cap_and_medium_effort_and_no_temperature() -> None:
    body = rmb.batch_request(_req(1, budget=4000), "r00000", MODEL)
    params = body["params"]
    assert params["max_tokens"] == 128_000
    assert params["output_config"] == {"effort": "medium"}
    assert "temperature" not in params  # Claude 5.x rejects it
    assert params["messages"] == [{"role": "user", "content": "paper 1"}]


def test_manifest_request_params_go_on_top_and_headers_go_on_the_batch(tmp_path) -> None:
    rp = {"max_tokens": 52_000, "output_config": {"effort": "low"},
          "extra_headers": {"anthropic-beta": "x-2026"}}
    reqs = [{**_req(i), "request_params": rp} for i in range(2)]
    params = rmb.batch_request(reqs[0], "r00000", MODEL)["params"]
    assert params["max_tokens"] == 52_000
    assert params["output_config"] == {"effort": "low"}
    assert "extra_headers" not in params
    client = _client()
    seen = {}
    create = client.messages.batches.create
    client.messages.batches.create = lambda **kw: seen.update(kw) or create(kw["requests"])
    rmb.submit(client, reqs, model=MODEL, run_id="run1",
               path=tmp_path / "s.json")
    assert seen["extra_headers"] == {"anthropic-beta": "x-2026"}


def test_requests_asking_for_different_headers_are_refused() -> None:
    a = {**_req(0), "request_params": {"extra_headers": {"anthropic-beta": "a"}}}
    with pytest.raises(SystemExit):
        rmb.batch_headers([a, _req(1)], MODEL)


def test_the_batch_id_is_saved_before_waiting(tmp_path) -> None:
    client = _client()
    path = tmp_path / "out.jsonl.batch.json"
    state = rmb.submit(client, [_req(0), _req(1)], model=MODEL, run_id="run1",
                       path=path)
    saved = json.loads(path.read_text())
    assert saved["batch_id"] == "msgbatch_1" == state["batch_id"]
    assert saved["custom_ids"] == {"r00000": "run1:K0:0", "r00001": "run1:K1:0"}
    assert len(client.messages.batches.created) == 1


def test_results_become_response_rows_with_real_usage() -> None:
    reqs = [_req(0), _req(1), _req(2), _req(3)]
    results = [
        NS(custom_id="r00000", result=NS(type="succeeded", message=_msg('{"decision": "include"}'))),
        NS(custom_id="r00001", result=NS(type="succeeded", message=_msg("{", stop="max_tokens"))),
        NS(custom_id="r00002", result=NS(type="errored", error=NS(error="overloaded"))),
        NS(custom_id="r00003", result=NS(type="succeeded", message=_msg("", stop="refusal"))),
    ]
    state = {"batch_id": "b", "custom_ids": {rmb.custom_id(i): r["request_id"]
                                             for i, r in enumerate(reqs)}}
    rows = {r["item_key"]: r for r in rmb.collect(_client(results), state, reqs, MODEL)}
    assert rows["K0"]["call_status"] == "ok"
    assert rows["K0"]["input_tokens"] == 50_000 and rows["K0"]["output_tokens"] == 900
    assert rows["K0"]["response_text"] == '{"decision": "include"}'
    assert rows["K1"]["call_status"] == "truncated"
    assert rows["K2"]["call_status"] == "error" and "errored" in rows["K2"]["error"]
    assert rows["K3"]["call_status"] == "error"
    for r in rows.values():
        assert r["schema_version"] == batch_manifest.SCHEMA_VERSION
        assert r["run_id"] == "run1"
