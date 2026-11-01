#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "anthropic>=0.40",
#     "tenacity>=8.0",
#     "httpx>=0.25",
# ]
# ///
"""Execute a screening manifest through the Anthropic Message Batches API.

The batch-priced sibling of `run_manifest.py`: same manifest in, same
responses file and run record out, so `--apply-responses` cannot tell
which executor ran. The Batches API bills half the synchronous price and
answers within 24 hours (usually minutes for a small batch).

    uv run run_manifest_batch.py --manifest batch/requests.jsonl \\
        --model claude-opus-5-5 --out batch/responses.jsonl

It writes three files:

  <out>             one response per request, JSONL
  <out>.run.json    the run record, with **real** token usage from the API
  <out>.batch.json  the submitted batch's id and the custom_id map

**Submission is recorded before anything else happens.** The batch id
goes to `<out>.batch.json` the moment the API accepts it, so Ctrl+C, a
closed laptop or a crash while polling never leads to a second, paid
submission: re-running the same command finds the state file and polls
the batch already in flight. `--no-wait` submits and exits; run the same
command later to collect.

Anthropic only, by construction: the Batches API is Anthropic's. For any
other provider use `run_manifest.py`.

**No retries inside the batch, and no placeholder text.** A request the
batch could not answer is recorded as `error`; an answer cut off by
`max_tokens` is `truncated`. Both leave the item untagged, and a re-emit
picks them up. Requests are built exactly as the synchronous path builds
them (`llm_provider.anthropic_request`): Claude 5+ get the output cap and
medium effort, and the manifest's `request_params` go on top.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = SCRIPT_DIR.parent
for p in (SCRIPT_DIR, SCRIPTS_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import batch_manifest  # noqa: E402
import run_manifest  # noqa: E402
from core import llm_provider  # noqa: E402
from core.models import effective_model, model_flag_help  # noqa: E402

#: Seconds between status polls. A small batch ends in minutes.
POLL_SECONDS = 60


def state_path(out_path: Path) -> Path:
    return out_path.parent / f"{out_path.name}.batch.json"


def custom_id(index: int) -> str:
    """A Batches-API-safe id. `request_id` carries colons, which the API's
    `^[a-zA-Z0-9_-]{1,64}$` rejects, so the state file holds the map."""
    return f"r{index:05d}"


def _build(req: dict, model: str) -> tuple[dict, dict]:
    return llm_provider.anthropic_request(
        model, req.get("system", ""), req.get("user", ""),
        max_tokens=int(req.get("max_output_tokens") or 1000),
        temperature=float(req.get("temperature", 0.0)),
        params=req.get("request_params"),
    )


def batch_request(req: dict, cid: str, model: str) -> dict:
    return {"custom_id": cid, "params": _build(req, model)[0]}


def batch_headers(requests: list[dict], model: str) -> dict:
    """The `extra_headers` the requests ask for, which must agree.

    A batch takes headers once, on submission, not per request.
    """
    found = {json.dumps(_build(r, model)[1], sort_keys=True) for r in requests}
    if len(found) > 1:
        raise SystemExit("ERROR: the manifest's requests ask for different "
                         "extra_headers; a batch can carry only one set.")
    return json.loads(found.pop()) if found else {}


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def submit(client, requests: list[dict], *, model: str, run_id: str,
           path: Path) -> dict:
    ids = {custom_id(i): r["request_id"] for i, r in enumerate(requests)}
    by_request = {v: k for k, v in ids.items()}
    headers = batch_headers(requests, model)
    batch = client.messages.batches.create(
        requests=[batch_request(r, by_request[r["request_id"]], model)
                  for r in requests],
        **({"extra_headers": headers} if headers else {}),
    )
    state = {
        "batch_id": batch.id,
        "run_id": run_id,
        "model": model,
        "submitted_at": datetime.now(UTC).isoformat(),
        "custom_ids": ids,
    }
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return state


def wait_for(client, batch_id: str) -> object:
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        c = batch.request_counts
        print(
            f"  {datetime.now().strftime('%H:%M:%S')} {batch.processing_status}: "
            f"{c.succeeded} ok, {c.errored} errored, {c.processing} processing",
            flush=True,
        )
        if batch.processing_status == "ended":
            return batch
        time.sleep(POLL_SECONDS)


def response_row(entry, *, req: dict, model: str, ended_at: str) -> dict:
    """One batch result → one response row, in `run_manifest`'s shape."""
    base = {
        "schema_version": batch_manifest.SCHEMA_VERSION,
        "run_id": req["run_id"],
        "request_id": req["request_id"],
        "item_key": req.get("item_key", ""),
        "model": model,
        "prompt_format": "chat",
        # Batch results carry no per-request completion time; the batch's
        # end is the latest a decision can have been made.
        "generated_at": ended_at,
    }
    result = entry.result
    if result.type != "succeeded":
        detail = getattr(getattr(result, "error", None), "error", None)
        return {**base, "call_status": "error", "finish_reason": None,
                "response_text": None,
                "error": f"batch result {result.type}: {detail or ''}"[:300],
                "input_tokens": 0, "output_tokens": 0}
    msg = result.message
    usage = msg.usage
    tokens = {
        "input_tokens": int(usage.input_tokens or 0),
        "output_tokens": int(usage.output_tokens or 0),
    }
    text = "".join(
        b.text for b in msg.content if getattr(b, "type", None) == "text"
    ).strip()
    if msg.stop_reason == "max_tokens":
        return {**base, "call_status": "truncated", "finish_reason": "length",
                "response_text": text, **tokens}
    if not text:
        status = "error" if msg.stop_reason == "refusal" else "empty"
        return {**base, "call_status": status, "finish_reason": msg.stop_reason,
                "response_text": "",
                "error": f"stop_reason={msg.stop_reason}" if status == "error" else None,
                **tokens}
    return {**base, "call_status": "ok", "finish_reason": "stop",
            "response_text": text, **tokens}


def collect(client, state: dict, requests: list[dict], model: str) -> list[dict]:
    by_request = {r["request_id"]: r for r in requests}
    ended_at = datetime.now(UTC).isoformat()
    rows: list[dict] = []
    for entry in client.messages.batches.results(state["batch_id"]):
        rid = state["custom_ids"].get(entry.custom_id)
        if rid is None or rid not in by_request:
            continue
        rows.append(response_row(entry, req=by_request[rid], model=model,
                                 ended_at=ended_at))
    return rows


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--manifest", required=True, help="Manifest JSONL to run.")
    p.add_argument("--out", default="", help="Responses JSONL to write "
                                             "(default: alongside the manifest).")
    p.add_argument("--model", default="",
                   help=model_flag_help("the manifest's model_hint"))
    p.add_argument("--limit", type=int, default=0,
                   help="Submit at most N requests (0 = all).")
    p.add_argument("--no-wait", action="store_true",
                   help="Submit and exit. Run the same command again to collect.")
    p.add_argument("--resubmit", action="store_true",
                   help="Ignore the saved batch id and submit again (pays again).")
    args = p.parse_args(argv)

    manifest_path = Path(args.manifest)
    header, requests = batch_manifest.read_manifest(manifest_path)
    if args.limit:
        requests = requests[: args.limit]
    model = effective_model(
        args.model, requests[0].get("model_hint", ""), stage="manifest",
    )
    if not model:
        sys.exit("ERROR: no model. Pass --model.")
    if llm_provider.resolve_provider(model).transport != "anthropic":
        sys.exit(f"ERROR: {model} is not an Anthropic model; the Batches API "
                 f"is Anthropic's. Use run_manifest.py.")
    llm_provider.require_credentials(model)

    out_path = Path(args.out) if args.out else (
        manifest_path.parent
        / f"{header['run_id']}__{batch_manifest.model_slug(model)}.responses.jsonl"
    )
    spath = state_path(out_path)
    client = llm_provider.AnthropicProvider().client

    state = {} if args.resubmit else load_state(spath)
    if state and (state.get("run_id") != header["run_id"]
                  or state.get("model") != model):
        sys.exit(f"ERROR: {spath} belongs to run {state.get('run_id')} / "
                 f"{state.get('model')}, not this manifest. Use another --out.")
    if state:
        print(f"Batch {state['batch_id']} already submitted at "
              f"{state['submitted_at']}; not resubmitting.", flush=True)
    else:
        state = submit(client, requests, model=model,
                       run_id=header["run_id"], path=spath)
        print(f"Submitted {len(requests)} request(s) as batch "
              f"{state['batch_id']} ({model}). State: {spath}", flush=True)
    if args.no_wait:
        return 0

    started = time.time()
    batch = wait_for(client, state["batch_id"])
    responses = collect(client, state, requests, model)
    record = run_manifest.write_outputs(
        out_path, requests=requests, responses=responses, header=header,
        model=model,
        extra={
            "generate_s": round(time.time() - started, 2),
            "manifest_sha256": header["sha256"],
            "n_requests": len(requests),
            "token_counts_are_estimates": False,
            "executor": "run_manifest_batch.py",
            "batch_id": state["batch_id"],
            "batch_created_at": str(getattr(batch, "created_at", "")),
            "batch_ended_at": str(getattr(batch, "ended_at", "")),
        },
    )
    print(f"\n{'=' * 60}")
    print(f"Wrote {len(responses)} response(s) to {out_path}")
    for k, v in sorted(record["status_counts"].items()):
        print(f"  {k}: {v}")
    print(f"  input tokens:  {record['input_tokens']}")
    print(f"  output tokens: {record['output_tokens']}")
    if record.get("degenerate_output"):
        print(f"\nWARNING: {record['degenerate_output_note']}", flush=True)
    return 0


if __name__ == "__main__":
    from core.console import enable_utf8_output
    enable_utf8_output()
    sys.exit(main())
