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

While it runs, each response is appended to `<out>.partial` as it
arrives, so Ctrl+C, a walltime SIGTERM or an OOM kill loses only the
calls in flight. Re-running the same command resumes: answered requests
are kept and only the rest are sent (`--no-resume` to redo everything).

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
import json
import os
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = SCRIPT_DIR.parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import batch_manifest  # noqa: E402
import worker_pool  # noqa: E402
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


#: Statuses a resumed run keeps. Errors are retried: they produced
#: nothing, so asking again pays for nothing twice.
KEEP_ON_RESUME = ("ok", "empty", "truncated")


def partial_path(out_path: Path) -> Path:
    """The crash-safe journal beside the responses file.

    Always plain JSONL, even for a `.gz` output: a gzip stream cut off by
    a kill is unreadable to its end, while a JSONL file cut off mid-line
    loses only that line.
    """
    return out_path.parent / f"{out_path.name}.partial"


def _read_jsonl_tolerant(path: Path) -> list[dict]:
    """Rows of a JSONL file, skipping a torn final line from a kill."""
    rows: list[dict] = []
    if not path.exists():
        return rows
    try:
        with batch_manifest._open_text(path, "r") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    except (OSError, EOFError):
        # A truncated .gz file: keep what decoded before the cut.
        pass
    return rows


def load_previous(out_path: Path, run_id: str, model: str) -> dict[str, dict]:
    """Responses an earlier, interrupted run of this manifest already paid for.

    Read from the final file and the journal both — a run killed hard
    (walltime SIGKILL, OOM) never got to write the final file. Only rows
    from the same run and the same model count; a different model's
    answers are a different experiment.
    """
    kept: dict[str, dict] = {}
    for path in (out_path, partial_path(out_path)):
        for row in _read_jsonl_tolerant(path):
            if (row.get("run_id") == run_id and row.get("model") == model
                    and row.get("call_status") in KEEP_ON_RESUME
                    and row.get("request_id")):
                kept[row["request_id"]] = row
    return kept


class _Terminated(Exception):
    """SIGTERM, raised in the main thread so cleanup runs (batch walltime)."""


def execute_manifest(
    requests: list[dict],
    *,
    client,
    model: str,
    out_path: Path,
    workers: int,
    previous: dict[str, dict] | None = None,
) -> tuple[list[dict], list[str], bool]:
    """Run every request not already answered; `(responses, fatal, interrupted)`.

    **Each response is appended to the journal the moment it completes**,
    flushed and fsynced. This used to hold every response in memory and
    write once at the end, so Ctrl+C, a scheduler's walltime SIGTERM or an
    OOM kill lost every generation already paid for. Now an interruption
    loses at most the calls in flight, and the next run resumes.

    `responses` includes the `previous` rows carried over. Ctrl+C and
    SIGTERM are caught and reported as `interrupted`, so the caller still
    writes the final file and run record from what completed.
    """
    previous = dict(previous or {})
    todo = [r for r in requests if r["request_id"] not in previous]
    responses: list[dict] = list(previous.values())
    fatal: list[str] = []
    interrupted = False
    journal = partial_path(out_path)
    journal.parent.mkdir(parents=True, exist_ok=True)
    total = len(requests)
    with journal.open("a", encoding="utf-8") as jf:
        # Carried-over rows go in first so the journal alone is complete.
        if previous and journal.stat().st_size == 0:
            for row in previous.values():
                jf.write(json.dumps(row, ensure_ascii=False) + "\n")
            jf.flush()
        try:
            with worker_pool.cancelling_pool(workers) as pool:
                futures = {
                    pool.submit(execute_one, r, client, model): r for r in todo
                }
                for fut in concurrent.futures.as_completed(futures):
                    row = fut.result()
                    verdict = row.pop("_fatal", "")
                    if verdict:
                        fatal.append(verdict)
                    jf.write(json.dumps(row, ensure_ascii=False) + "\n")
                    jf.flush()
                    os.fsync(jf.fileno())
                    responses.append(row)
                    print(f"[{len(responses)}/{total}] {row['item_key']} → "
                          f"{row['call_status']}", flush=True)
                    if fatal:
                        # A spent quota fails every remaining request
                        # identically.
                        for pending in futures:
                            pending.cancel()
                        print("", flush=True)
                        print(fatal[0], file=sys.stderr, flush=True)
                        break
        except (KeyboardInterrupt, _Terminated):
            interrupted = True
            print(
                f"\nInterrupted after {len(responses)} of {total} response(s). "
                f"Writing what completed; re-run the same command to resume.",
                file=sys.stderr, flush=True,
            )
    return responses, fatal, interrupted


def write_outputs(
    out_path: Path,
    *,
    requests: list[dict],
    responses: list[dict],
    header: dict,
    model: str,
    extra: dict,
) -> dict:
    """Write the ordered responses file and the run record; drop the journal.

    The final file goes to a temporary name and is renamed, so a kill
    during this step leaves the journal — still complete — in place.
    """
    # Manifest order, so the file diffs cleanly between runs.
    order = {r["request_id"]: i for i, r in enumerate(requests)}
    responses = sorted(
        responses, key=lambda r: order.get(r["request_id"], 1 << 30),
    )
    tmp = out_path.parent / f".{out_path.name}.tmp"
    if out_path.name.endswith(".gz"):
        tmp = out_path.parent / f".{out_path.name[:-3]}.tmp.gz"
    batch_manifest._write_jsonl(tmp, responses)
    os.replace(tmp, out_path)
    partial_path(out_path).unlink(missing_ok=True)

    record = batch_manifest.summarise_run(
        header["run_id"], stage=header["stage"], model=model,
        responses=responses, extra=extra,
    )
    record_path = batch_manifest.run_record_path(out_path)
    record_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return record


def main(argv: list[str] | None = None) -> int:
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
    p.add_argument("--no-resume", action="store_true",
                   help="Ignore responses an interrupted earlier run of this "
                        "manifest left behind, and ask for every request "
                        "again. By default they are kept and only the rest "
                        "is executed.")
    args = p.parse_args(argv)

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

    previous: dict[str, dict] = {}
    if args.no_resume:
        partial_path(out_path).unlink(missing_ok=True)
    else:
        wanted = {r["request_id"] for r in requests}
        previous = {
            k: v for k, v in load_previous(
                out_path, header["run_id"], model,
            ).items() if k in wanted
        }
        if previous:
            print(
                f"Resuming: keeping {len(previous)} response(s) from an "
                f"earlier run of this manifest (--no-resume to redo them).",
                flush=True,
            )

    print(
        f"Executing {len(requests) - len(previous)} request(s) from "
        f"{manifest_path.name} with {model} ({args.workers} workers)...",
        flush=True,
    )

    # A batch scheduler ends a job at walltime with SIGTERM. Raised as an
    # exception in the main thread so the responses that completed are
    # written out instead of dying with the process.
    def _on_term(signum, frame):  # noqa: ARG001
        raise _Terminated()
    try:
        previous_handler = signal.signal(signal.SIGTERM, _on_term)
    except (ValueError, AttributeError):
        previous_handler = None  # not the main thread / no SIGTERM here

    started = time.time()
    try:
        responses, fatal, interrupted = execute_manifest(
            requests, client=client, model=model, out_path=out_path,
            workers=args.workers, previous=previous,
        )
    finally:
        if previous_handler is not None:
            signal.signal(signal.SIGTERM, previous_handler)
    elapsed = time.time() - started

    record = write_outputs(
        out_path, requests=requests, responses=responses, header=header,
        model=model,
        extra={
            "generate_s": round(elapsed, 2),
            "manifest_sha256": header["sha256"],
            "n_requests": len(requests),
            "workers": args.workers,
            "token_counts_are_estimates": True,
            "executor": "run_manifest.py",
            "resumed_responses": len(previous),
            "interrupted": interrupted,
        },
    )
    record_path = batch_manifest.run_record_path(out_path)

    print(f"\n{'=' * 60}")
    print(f"Wrote {len(responses)} response(s) to {out_path}")
    print(f"Run record: {record_path}")
    for k, v in sorted(record["status_counts"].items()):
        print(f"  {k}: {v}")
    print(f"  mean output tokens (est.): {record['mean_output_tokens']}")
    if record.get("degenerate_output"):
        print(f"\nWARNING: {record['degenerate_output_note']}", flush=True)
    if interrupted:
        return 130
    return 1 if fatal else 0


if __name__ == "__main__":
    # Windows takes stdout's encoding from the locale when output is
    # redirected — normally cp1252, which cannot encode the arrows, em
    # dashes and rules printed below. See scripts/core/console.py.
    from core.console import enable_utf8_output
    enable_utf8_output()
    sys.exit(main())
