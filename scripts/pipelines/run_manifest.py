#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "anthropic>=0.40",
#     "tenacity>=8.0",
#     "httpx>=0.25",
#     "google-genai",
#     "openai>=1.0",
# ]
# ///
"""Execute a screening manifest against the configured LLM provider.

The reference executor for the batch path. A manifest is deliberately
self-contained — every row carries its own rendered prompt — so running
one needs a model and nothing else: no Zotero, no `screening_config.py`,
no project. This script is the proof of that, and the thing to copy when
writing an executor for somewhere the plugin cannot reach.

    uv run run_manifest.py --manifest batch/requests.jsonl \\
        --model org/model-id --out batch/responses.jsonl

It writes two files:

  <out>            one response per request, JSONL
  <out>.run.json   the run record — counts, tokens, timings, and the
                   degeneracy verdict `apply` refuses on

**No retries, and no placeholder text.** A failed generation is recorded
as a failed generation with its error; the item stays untagged and a
re-run picks it up. Writing "could not process" into a results file is
how a corpus acquires sentences no model ever produced.

Why this exists separately from `abstract_screen.py`: the whole point of
the split is that execution can happen where the compute is. Keeping the
executor a separate, dependency-light script means the same manifest can
be run here, on a colleague's laptop, or by a job on a GPU node, and the
applier cannot tell the difference.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = SCRIPT_DIR.parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import batch_manifest  # noqa: E402
from core import llm_provider  # noqa: E402
from core.models import effective_model, model_flag_help  # noqa: E402


def execute_one(req: dict, client, model: str) -> dict:
    """One request → one response row. Never raises."""
    base = {
        "schema_version": batch_manifest.SCHEMA_VERSION,
        "run_id": req["run_id"],
        "request_id": req["request_id"],
        "item_key": req.get("item_key", ""),
        "model": model,
        "prompt_format": "chat",
    }
    # Stamped when the answer arrives, not when the request left: this
    # value becomes the CSV's `timestamp`, which claims to record when a
    # decision was made. On a slow model the two are minutes apart.
    def _stamped(extra: dict) -> dict:
        return {**base, "generated_at": datetime.now(UTC).isoformat(), **extra}
    try:
        text = client.generate(
            model=model,
            system=req.get("system", ""),
            prompt=req.get("user", ""),
            temperature=float(req.get("temperature", 0.0)),
            max_tokens=int(req.get("max_output_tokens") or 1000),
        )
    except Exception as e:  # noqa: BLE001 - classified, not swallowed
        verdict = llm_provider.classify_failure(e)
        return _stamped({
            "call_status": "error",
            "finish_reason": None,
            "response_text": None,
            "error": f"{verdict.status.value}: {verdict.detail}"[:300],
            "input_tokens": 0,
            "output_tokens": 0,
            "_fatal": "" if verdict.retryable else verdict.format(),
        })
    if not (text or "").strip():
        # Distinct from `error`: the provider answered, with nothing.
        # This is the signature of an unframed prompt, and the run
        # record's degeneracy check is what turns a corpus of these into
        # a refusal rather than 240 `borderline` tags.
        return _stamped({
            "call_status": "empty", "finish_reason": "stop",
            "response_text": "", "input_tokens": 0, "output_tokens": 0,
        })
    return _stamped({
        "call_status": "ok",
        "finish_reason": "stop",
        "response_text": text,
        # The SDKs report usage inconsistently across providers and this
        # layer does not see it; character counts are honest proxies and
        # are labelled as estimates in the run record.
        "input_tokens": (len(req.get("system", "")) + len(req.get("user", ""))) // 4,
        "output_tokens": len(text) // 4,
    })


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(line_buffering=True)
        except Exception:
            pass
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest", required=True, help="Manifest JSONL to run.")
    p.add_argument("--out", default="", help="Responses JSONL to write "
                                             "(default: alongside the manifest).")
    p.add_argument("--model", default="",
                   help=model_flag_help("the manifest's model_hint"))
    p.add_argument("--workers", type=int, default=8,
                   help="Parallel requests (default: 8).")
    p.add_argument("--limit", type=int, default=0,
                   help="Execute at most N requests (0 = all). Use a small "
                        "value for a pilot before committing a whole corpus.")
    p.add_argument("--skip-preflight", action="store_true",
                   help="Skip the one ~4-token check that the model answers.")
    args = p.parse_args()

    manifest_path = Path(args.manifest)
    header, requests = batch_manifest.read_manifest(manifest_path)
    if args.limit:
        requests = requests[: args.limit]

    model = effective_model(
        args.model, requests[0].get("model_hint", ""), stage="manifest",
    )
    if not model:
        sys.exit(
            "ERROR: no model. The manifest carries no model_hint, so pass "
            "--model with an ID your provider serves."
        )
    llm_provider.require_credentials(model)
    if not args.skip_preflight:
        llm_provider.preflight_or_exit(model)

    out_path = Path(args.out) if args.out else (
        manifest_path.parent
        / f"{header['run_id']}__{batch_manifest.model_slug(model)}.responses.jsonl"
    )
    client = llm_provider.get_provider(model)

    print(
        f"Executing {len(requests)} request(s) from {manifest_path.name} "
        f"with {model} ({args.workers} workers)...", flush=True,
    )
    responses: list[dict] = []
    fatal: list[str] = []
    lock = threading.Lock()
    started = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(execute_one, r, client, model): r for r in requests}
        for fut in concurrent.futures.as_completed(futures):
            row = fut.result()
            verdict = row.pop("_fatal", "")
            if verdict:
                fatal.append(verdict)
            with lock:
                responses.append(row)
                n = len(responses)
            print(f"[{n}/{len(requests)}] {row['item_key']} → "
                  f"{row['call_status']}", flush=True)
            if fatal:
                # A spent quota fails every remaining request identically.
                for pending in futures:
                    pending.cancel()
                print("", flush=True)
                print(fatal[0], file=sys.stderr, flush=True)
                break
    elapsed = time.time() - started

    # Written in manifest order so the file diffs cleanly between runs.
    order = {r["request_id"]: i for i, r in enumerate(requests)}
    responses.sort(key=lambda r: order.get(r["request_id"], 1 << 30))
    batch_manifest._write_jsonl(out_path, responses)

    record = batch_manifest.summarise_run(
        header["run_id"], stage=header["stage"], model=model,
        responses=responses,
        extra={
            "generate_s": round(elapsed, 2),
            "manifest_sha256": header["sha256"],
            "n_requests": len(requests),
            "workers": args.workers,
            "token_counts_are_estimates": True,
            "executor": "run_manifest.py",
        },
    )
    record_path = batch_manifest.run_record_path(out_path)
    record_path.write_text(
        __import__("json").dumps(record, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"\n{'=' * 60}")
    print(f"Wrote {len(responses)} response(s) to {out_path}")
    print(f"Run record: {record_path}")
    for k, v in sorted(record["status_counts"].items()):
        print(f"  {k}: {v}")
    print(f"  mean output tokens (est.): {record['mean_output_tokens']}")
    if record.get("degenerate_output"):
        print(f"\nWARNING: {record['degenerate_output_note']}", flush=True)
    return 1 if fatal else 0


if __name__ == "__main__":
    # Windows takes stdout's encoding from the locale when output is
    # redirected — normally cp1252, which cannot encode the arrows, em
    # dashes and rules printed below. See scripts/core/console.py.
    from core.console import enable_utf8_output
    enable_utf8_output()
    sys.exit(main())
