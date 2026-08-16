#!/usr/bin/env python3
"""Execute a screening manifest with vLLM offline batch inference.

The compute-side half of the batch path. A manifest emitted by
`abstract_screen.py --emit-manifest` or `fulltext_code.py --emit-manifest`
is self-contained — every row carries its own rendered prompt — so
running one needs a model and nothing else. This script is what runs one
on a GPU node behind a batch scheduler, where the marginal cost of
screening a corpus is electricity rather than dollars.

    # on a login node: assemble, count and guard, with zero GPU work
    python3 run_batch.py --manifest requests.jsonl --model <org/model> --dry-run

    # on a compute node, from run_batch.sbatch
    python3 run_batch.py --manifest requests.jsonl --model <org/model> \\
        --out-dir results --execute --confirm

It writes two files, exactly like `scripts/pipelines/run_manifest.py`:

  <out>            one response per request, JSONL
  <out>.run.json   the run record — counts, tokens, timings and the
                   degeneracy verdict that `--apply-responses` refuses on

**This file has no plugin imports, by design.** It is copied to the
cluster on its own, alongside the manifest, and run there. Nothing else
from this repository goes with it: not `batch_manifest.py`, not a
checkout, not a virtualenv. Three consequences, each of which is a
constraint rather than a preference:

1. **Stdlib plus `vllm`, and `vllm` is imported lazily inside `execute()`**
   so `--dry-run` works on a login node that has no GPU stack.
2. **Python 3.9-compatible syntax.** A cluster's system interpreter is
   frequently older than this plugin's own 3.11 floor, and the runner has
   to work with whatever the site's module system provides.
3. **Invoke it by file path — `python3 run_batch.py` — never
   `python3 -m scripts.cluster.run_batch`.** `scripts/` is a namespace
   package here, and on a cluster the name `scripts` is very likely
   already taken by the site's own software stack. The reference project
   lost exactly that name and `PYTHONPATH` did not win it back. By file
   path there is no package to resolve, so there is nothing to lose.

The schema constants below are duplicated from
`scripts/pipelines/batch_manifest.py` for the same reason. That is a
real cost, so `tests/unit/test_cluster_runner.py` asserts the two agree —
including that this file's run record is byte-comparable with the one
`batch_manifest.summarise_run` produces.

Three rules carried over from the reference implementation, each learned
from a run that looked healthy and was not:

**An instruction-tuned model handed a bare document answers with
nothing.** Every prompt goes through the model's own chat template with
`add_generation_prompt=True`. Where the template refuses a system role
the system prompt is merged into the user turn; only if there is no
usable template at all does the raw payload go out, and that path shouts.
The format used is recorded on every response so the two are never
confused afterwards.

**A failed generation is recorded as a failed generation.** No retries,
no sentinel text. An item whose generation failed stays untagged and a
re-run picks it up; "could not process" written into a results file is
how a corpus acquires sentences no model ever produced.

**An answer that does not fit is not sent.** A request whose prompt
leaves no room for an answer inside `--max-model-len` is recorded as an
error before generation rather than failing inside vLLM, where it reads
as a model failure instead of the budgeting failure it is.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

# --------------------------------------------------------------------------
# Schema constants. Duplicated from scripts/pipelines/batch_manifest.py
# because this file ships to the cluster alone; kept honest by
# tests/unit/test_cluster_runner.py.
# --------------------------------------------------------------------------

SCHEMA_VERSION = 1

#: Mean output tokens at or below which a run is presumed degenerate.
DEGENERATE_OUTPUT_TOKENS = 2

#: Markers separating a reasoning model's thinking channel from its
#: answer. The raw stream is still recorded verbatim in `response_text`;
#: `reasoning_text` is the convenience split.
REASONING_MARKERS = (
    "<|start|>assistant<|channel|>final<|message|>",
    "assistantfinal",
    "</think>",
    "<|im_start|>assistant",
)

#: Reasoning models spend their budget twice: the trace is generated
#: before the answer and both come out of one token budget, so a budget
#: sized for the answer truncates the answer. Applied here rather than at
#: emit time because the model is not known when a manifest is assembled.
REASONING_MODEL_PATTERNS = ("gpt-oss", "deepseek-r1", "qwq", "-thinking")
REASONING_BUDGET_FACTOR = 3

#: Ceiling on any single request's output budget, and the floor below
#: which an answer is not worth generating: a screening decision cut off
#: at 16 tokens is an error either way, and saying so before the run
#: costs nothing.
MAX_OUTPUT_BUDGET = 16384
MIN_OUTPUT_BUDGET = 32
DEFAULT_OUTPUT_BUDGET = 1000

#: vLLM's own default. Overridden by --max-model-len, which the sbatch
#: wrapper always passes.
DEFAULT_MAX_MODEL_LEN = 32768


class ManifestError(RuntimeError):
    """A manifest cannot be trusted."""


# --------------------------------------------------------------------------
# Manifest IO
# --------------------------------------------------------------------------


def _open_text(path, mode):
    """Open plain or gzipped JSONL by suffix.

    A full-text manifest is up to ~720 kB of prompt per paper, so a few
    hundred papers is a file worth compressing before it crosses a
    network to a cluster.
    """
    if str(path).endswith(".gz"):
        return gzip.open(path, mode + "t", encoding="utf-8")
    return open(path, mode, encoding="utf-8")


def read_manifest(path):
    """`(header, rows)`. Raises ManifestError rather than guessing.

    The same validation `batch_manifest.read_manifest` does, for the same
    reason: a manifest and the code that applies it can be separated by
    weeks, a file copy and a `git pull`, and rows from two runs in one
    file would apply one run's decisions under the other's identity.
    """
    rows = []
    with _open_text(path, "r") as fh:
        for n, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError as e:
                raise ManifestError(f"{path}:{n}: not valid JSON - {e}") from e
            if not isinstance(obj, dict):
                raise ManifestError(f"{path}:{n}: expected an object")
            rows.append(obj)
    if not rows:
        raise ManifestError(
            f"{path} is empty. An empty manifest is not an empty result - "
            "re-emit it, and read the .skipped.json sidecar to see why "
            "every unit was dropped."
        )
    versions = set(r.get("schema_version") for r in rows)
    if versions != set([SCHEMA_VERSION]):
        seen_versions = sorted(str(v) for v in versions)
        raise ManifestError(
            f"{path}: manifest schema {seen_versions} but this runner "
            f"understands {SCHEMA_VERSION}. Copy the current run_batch.py "
            f"across rather than executing a format it may misread."
        )
    for field in ("run_id", "stage"):
        seen = set(r.get(field) for r in rows)
        if len(seen) != 1:
            raise ManifestError(
                f"{path}: rows disagree on {field}: {sorted(str(s) for s in seen)}"
            )
    ids = [r.get("request_id") for r in rows]
    if len(set(ids)) != len(ids):
        raise ManifestError(f"{path}: duplicate request_id values")
    header = {
        "run_id": rows[0]["run_id"],
        "stage": rows[0]["stage"],
        "schema_version": SCHEMA_VERSION,
        "n_requests": len(rows),
        "sha256": file_sha256(path),
    }
    return header, rows


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 16)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def model_slug(model):
    """A model ID reduced to something safe in a filename."""
    return re.sub(r"[^A-Za-z0-9]+", "-", model or "").strip("-") or "model"


def write_jsonl(path, rows):
    parent = os.path.dirname(str(path))
    if parent:
        _makedirs(parent)
    with _open_text(path, "w") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def _makedirs(path):
    if not os.path.isdir(path):
        os.makedirs(path)


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


def split_reasoning(text):
    """`(reasoning, answer)`. `reasoning` is `""` when there is none.

    Splits on the last marker found, since a model may emit several
    channel switches before settling. Mirrors
    `batch_manifest.split_reasoning`, which the applier runs over
    `response_text` independently — this copy exists so the responses
    file carries the split for a human reading it.
    """
    if not text:
        return "", ""
    best = -1
    best_end = 0
    for marker in REASONING_MARKERS:
        idx = text.rfind(marker)
        if idx > best:
            best, best_end = idx, idx + len(marker)
    if best < 0:
        return "", text
    return text[:best].strip(), text[best_end:].strip()


def reasoning_factor(model):
    """How much to multiply an output budget by for this model.

    A manifest's `max_output_tokens` sizes the *answer*. A reasoning
    model writes its trace into the same budget, so the answer is what
    gets truncated — and a truncated answer is unparseable, which scores
    as a schema failure rather than as the budget failure it is.
    """
    lowered = (model or "").lower()
    for pattern in REASONING_MODEL_PATTERNS:
        if pattern in lowered:
            return REASONING_BUDGET_FACTOR
    return 1


def output_budget(row, factor=1, default=DEFAULT_OUTPUT_BUDGET):
    """The per-request output budget, from the manifest row.

    Per request, not one flat value for the batch: an abstract-screening
    decision is 200 tokens and a full-text coding response over a
    30-field schema is thousands. One flat budget either truncates the
    large requests or pays decode headroom on every small one.
    """
    try:
        budget = int(row.get("max_output_tokens") or default)
    except (TypeError, ValueError):
        budget = default
    return max(1, min(MAX_OUTPUT_BUDGET, factor * budget))


def fit_budget(n_input_tokens, budget, max_model_len):
    """`(budget, reason)` for one request. `budget` is None when unsendable.

    vLLM fails a request whose prompt plus its output budget exceeds
    `max_model_len`, and it fails it *inside* generation, where the
    traceback reads as a model problem. Deciding here means an over-long
    request is recorded as an error against its own item, with the two
    numbers that explain it, and the rest of the batch still runs.

    A budget that only just fits is refused as well: a decision cut off
    after twenty tokens is unparseable, so it would be recorded as an
    error regardless, and the GPU time spent producing it is wasted.
    """
    room = max_model_len - n_input_tokens
    if room <= 0:
        return None, (
            f"input is {n_input_tokens} tokens; --max-model-len is "
            f"{max_model_len}, so this request cannot be sent. Re-emit with "
            f"--max-input-chars, or raise --max-model-len if the model "
            f"supports it."
        )
    if room < MIN_OUTPUT_BUDGET:
        return None, (
            f"input is {n_input_tokens} tokens of a {max_model_len}-token "
            f"context, leaving room for {room} output token(s) — below the "
            f"{MIN_OUTPUT_BUDGET} needed for an answer worth parsing. Not sent."
        )
    if budget > room:
        return room, (
            f"output budget reduced from {budget} to {room} tokens to fit "
            f"the {max_model_len}-token context."
        )
    return budget, ""


def is_degenerate(mean_output_tokens):
    return mean_output_tokens <= DEGENERATE_OUTPUT_TOKENS


# --------------------------------------------------------------------------
# Prompt rendering
# --------------------------------------------------------------------------


def _messages(row, merge_system):
    system = row.get("system") or ""
    user = row.get("user") or ""
    if not system:
        return [{"role": "user", "content": user}]
    if merge_system:
        return [{"role": "user", "content": system + "\n\n" + user}]
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def render_prompts(tokenizer, rows, log=print):
    """`(texts, prompt_format)` — every prompt in the model's own template.

    Three formats, tried in order, because the failure they guard against
    is silent. An instruction-tuned model handed an unframed document
    decides the turn is over and emits one end-of-turn token:
    `finish_reason="stop"`, `call_status="ok"`, and a whole run of empty
    answers that nothing downstream distinguishes from real ones.

    - `chat_template` — system and user as separate turns. What almost
      every model wants.
    - `chat_template_merged_system` — the system prompt prepended to the
      user turn. Several widely-used open-weight instruction models ship
      templates that raise on a system role outright; merging is the
      documented way to prompt them, not a degradation.
    - `raw_completion` — no template at all. Genuinely last-resort, so it
      says so loudly and the format is on every response row.
    """
    attempts = (
        ("chat_template", False),
        ("chat_template_merged_system", True),
    )
    first_error = None
    for fmt, merge in attempts:
        try:
            texts = [
                tokenizer.apply_chat_template(
                    _messages(row, merge),
                    tokenize=False,
                    add_generation_prompt=True,
                )
                for row in rows
            ]
        except Exception as exc:  # noqa: BLE001 - reported, then downgraded
            if first_error is None:
                first_error = exc
            continue
        if merge:
            log(
                f"NOTE: this model's chat template does not accept a system "
                f"role ({type(first_error).__name__}), so the system prompt "
                f"was prepended to the user turn. Recorded as "
                f"prompt_format={fmt}."
            )
        return texts, fmt

    why = type(first_error).__name__ if first_error else "no tokenizer"
    log(
        f"WARNING: no usable chat template for this model ({why}). Sending "
        f"RAW COMPLETIONS.\n"
        f"         An instruction-tuned model handed an unframed prompt "
        f"typically answers with a single end-of-turn token while every row "
        f"still reads call_status=ok. Check the run record's "
        f"mean_output_tokens before applying anything from this run."
    )
    texts = []
    for row in rows:
        system = row.get("system") or ""
        user = row.get("user") or ""
        texts.append((system + "\n\n" + user) if system else user)
    return texts, "raw_completion"


def count_tokens(tokenizer, texts):
    """Exact token counts for the rendered prompts.

    Exact, not `len(text) // 4`: the whole point of counting here is to
    decide what fits in the context window, and an estimate that is 20%
    low turns a refusal-before-sending into a failure inside vLLM.
    """
    return [len(tokenizer.encode(text)) for text in texts]


# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------


def hardware_provenance():
    """Node, job, GPU and driver. "An A100" is not provenance.

    Every field is optional: this runs on a login node under `--dry-run`
    and on a workstation under test, neither of which has `nvidia-smi` or
    a job ID, and a missing value is recorded as missing rather than
    guessed.
    """
    record = {
        "node": os.environ.get("SLURMD_NODENAME") or platform.node(),
        "partition": os.environ.get("SLURM_JOB_PARTITION"),
        "job_id": os.environ.get("SLURM_JOB_ID"),
        "array_job_id": os.environ.get("SLURM_ARRAY_JOB_ID"),
        "array_task_id": os.environ.get("SLURM_ARRAY_TASK_ID"),
        "gpu_name": None,
        "gpu_count": 0,
        "gpu_vram_mb": None,
        "driver_version": None,
    }
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            timeout=20,
        )
        out = proc.stdout.decode("utf-8", "replace").strip()
        if proc.returncode != 0:
            raise OSError(f"nvidia-smi exit {proc.returncode}")
    except Exception as exc:  # noqa: BLE001 - absence is a recorded state
        record["gpu_query_error"] = type(exc).__name__
        return record
    lines = [line for line in out.splitlines() if line.strip()]
    if lines:
        parts = [part.strip() for part in lines[0].split(",")]
        if len(parts) == 3:
            record["gpu_name"] = parts[0]
            record["gpu_count"] = len(lines)
            try:
                record["gpu_vram_mb"] = int(parts[1])
            except ValueError:
                record["gpu_vram_mb"] = None
            record["driver_version"] = parts[2]
    return record


def vllm_version():
    try:
        import vllm  # noqa: PLC0415 - cluster-only import

        return getattr(vllm, "__version__", None)
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# Run record. Mirrors batch_manifest.summarise_run; the applier reads
# `degenerate_output` off it and refuses.
# --------------------------------------------------------------------------


def summarise(run_id, stage, model, responses, extra=None):
    statuses = {}
    for r in responses:
        s = str(r.get("call_status", "error"))
        statuses[s] = statuses.get(s, 0) + 1
    out_tokens = [
        int(r.get("output_tokens") or 0)
        for r in responses
        if r.get("call_status") == "ok"
    ]
    mean_out = (sum(out_tokens) / len(out_tokens)) if out_tokens else 0.0
    record = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "stage": stage,
        "model": model,
        "n_responses": len(responses),
        "status_counts": dict(sorted(statuses.items())),
        "input_tokens": sum(int(r.get("input_tokens") or 0) for r in responses),
        "output_tokens": sum(int(r.get("output_tokens") or 0) for r in responses),
        "mean_output_tokens": round(mean_out, 2),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    record.update(extra or {})
    if responses and is_degenerate(mean_out):
        record["degenerate_output"] = True
        record["degenerate_output_note"] = (
            f"mean output {mean_out:.2f} tokens per answered request. The model "
            "replied with (almost) nothing — usually an unframed prompt, "
            "the wrong chat template, or a stop token inside the payload. "
            "Do not score or apply this run."
        )
    return record


def write_run_record(path, record):
    parent = os.path.dirname(str(path))
    if parent:
        _makedirs(parent)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    return path


def run_record_path(responses_path):
    name = str(responses_path)
    for suffix in (".gz", ".jsonl"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name + ".run.json"


# --------------------------------------------------------------------------
# Pre-flight
# --------------------------------------------------------------------------


def preflight(header, rows, args, out_path):
    """The summary printed before anything is sent, dry run or not.

    Token figures here are `chars // 4` estimates and say so: counting
    exactly needs the model's tokenizer, which needs the GPU stack, which
    is the thing `--dry-run` exists to avoid needing. The runner counts
    exactly, with the real tokenizer, before it sends.
    """
    factor = reasoning_factor(args.model)
    budgets = [output_budget(r, factor) for r in rows]
    chars = [int(r.get("input_chars") or len(r.get("user") or "")) for r in rows]
    total_chars = sum(chars)
    reasoning_note = f" (x{factor}, reasoning model)" if factor > 1 else ""
    lines = [
        "",
        "=" * 70,
        f"PRE-FLIGHT  run_id={header['run_id']}",
        "=" * 70,
        f"  manifest             : {args.manifest}",
        f"  manifest sha256      : {header['sha256'][:16]}",
        f"  stage                : {header['stage']}",
        f"  requests             : {len(rows)}",
        f"  model                : {args.model}",
        f"  max_model_len        : {args.max_model_len}",
        f"  temperature          : {args.temperature}",
        f"  seed                 : {args.seed}",
        f"  output budget        : {min(budgets)}-{max(budgets)} "
        f"tokens/request{reasoning_note}",
        f"  input chars          : {total_chars} total, largest "
        f"{max(chars) if chars else 0}",
        f"  input tokens         : ~{total_chars // 4} (chars/4 estimate; "
        f"counted exactly with the model's tokenizer before sending)",
        f"  responses            : {out_path}",
        "=" * 70,
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


def execute(rows, args, log=print):
    """Run every request through vLLM in one batch. `(responses, record)`.

    One `llm.generate()` call over the whole manifest, not a loop: vLLM
    schedules continuously across requests, and feeding it one prompt at
    a time forfeits nearly all of the throughput that made a GPU node
    worth queueing for.

    Nothing is written until generation finishes, because that is what
    offline batch inference is. A job that runs out of walltime produces
    no responses file at all, which is why the pre-flight prints how big
    the batch is and the run record separates load time from generate
    time — those are the two numbers that size the next manifest.
    """
    from vllm import LLM, SamplingParams  # noqa: PLC0415 - cluster-only import

    load_start = time.time()
    llm = LLM(
        model=args.model,
        max_model_len=args.max_model_len,
        gpu_memory_utilization=args.gpu_memory_utilization,
        enable_prefix_caching=True,
        trust_remote_code=False,
    )
    model_load_s = time.time() - load_start

    tokenizer = llm.get_tokenizer()
    texts, prompt_format = render_prompts(tokenizer, rows, log=log)
    n_input = count_tokens(tokenizer, texts)

    factor = reasoning_factor(args.model)
    if factor > 1:
        log(
            f"Reasoning model: multiplying every output budget by {factor} so "
            f"the answer is not truncated by the trace preceding it."
        )

    # Split into what can be sent and what cannot, before sending
    # anything. An over-long request is a fact about the manifest, not a
    # model verdict, and it must not take the rest of the batch with it.
    sendable = []
    not_sent = []
    for idx, row in enumerate(rows):
        budget, note = fit_budget(n_input[idx], output_budget(row, factor), args.max_model_len)
        if budget is None:
            not_sent.append((idx, note))
        else:
            if note:
                log("  {}: {}".format(row.get("item_key", "?"), note))
            sendable.append((idx, budget))
    if not_sent:
        log(
            f"\n{len(not_sent)} of {len(rows)} request(s) do not fit in a "
            f"{args.max_model_len}-token context and will be recorded as "
            f"errors without being sent."
        )
    if not sendable:
        raise SystemExit(
            f"REFUSING to run: not one of {len(rows)} request(s) fits in a "
            f"{args.max_model_len}-token context. Re-emit the manifest with "
            f"--max-input-chars, or run a model with a larger context."
        )

    params = [
        SamplingParams(temperature=args.temperature, seed=args.seed, max_tokens=budget)
        for _, budget in sendable
    ]
    generate_start = time.time()
    outputs = llm.generate([texts[idx] for idx, _ in sendable], params)
    generate_s = time.time() - generate_start

    # One stamp for the batch: offline inference returns everything at
    # once, so a per-row time would be invented precision. This value
    # becomes the CSV's `timestamp`, which claims to record when the
    # decision was made — and for a batch run, it was.
    generated_at = datetime.now(timezone.utc).isoformat()
    by_index = {}
    # No `strict=`: it is Python 3.10+, and this file runs on 3.9.
    # vLLM returns one output per prompt, and the pairing is checked by
    # the response loop below rather than by zip.
    for (idx, budget), output in zip(sendable, outputs):  # noqa: B905
        by_index[idx] = (output, budget)
    refusals = dict(not_sent)

    responses = []
    for idx, row in enumerate(rows):
        base = _response_base(row, args, prompt_format, generated_at)
        if idx not in by_index:
            note = refusals[idx]
            responses.append(
                _merge(
                    base,
                    call_status="error",
                    finish_reason=None,
                    response_text=None,
                    reasoning_text="",
                    error=note[:300],
                    input_tokens=n_input[idx],
                    output_tokens=0,
                )
            )
            continue
        output, budget = by_index[idx]
        responses.append(
            _response_row(base, output, n_input[idx], budget)
        )

    record = {
        "model_load_s": round(model_load_s, 2),
        "generate_s": round(generate_s, 2),
        "prompt_format": prompt_format,
        "n_sent": len(sendable),
        "n_not_sent": len(not_sent),
        "max_model_len": args.max_model_len,
        "gpu_memory_utilization": args.gpu_memory_utilization,
        "temperature": args.temperature,
        "seed": args.seed,
        "vllm_version": vllm_version(),
    }
    return responses, record


def _response_base(row, args, prompt_format, generated_at):
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": row.get("run_id", ""),
        "request_id": row.get("request_id", ""),
        "item_key": row.get("item_key", ""),
        "model": args.model,
        "model_revision": args.model_revision or "",
        "prompt_format": prompt_format,
        "generated_at": generated_at,
    }


def _merge(base, **extra):
    merged = dict(base)
    merged.update(extra)
    return merged


def _response_row(base, output, n_in, budget):
    """One vLLM output → one response row. Never raises."""
    completions = getattr(output, "outputs", None) or []
    if not completions:
        return _merge(
            base,
            call_status="error",
            finish_reason=None,
            response_text=None,
            reasoning_text="",
            error="vllm returned no completion for this request",
            input_tokens=n_in,
            output_tokens=0,
        )
    completion = completions[0]
    text = getattr(completion, "text", "") or ""
    finish_reason = getattr(completion, "finish_reason", None)
    n_out = len(getattr(completion, "token_ids", None) or [])

    # vLLM splits the reasoning channel itself for models it knows how
    # to; where it does not, the markers do. Either way `response_text`
    # stays verbatim — the applier re-splits it, and a file that has been
    # edited to "help" the parser is no longer evidence of what the model
    # said.
    reasoning = getattr(completion, "reasoning_content", None)
    if not reasoning:
        reasoning, _ = split_reasoning(text)

    if finish_reason == "length":
        # Cut off. A run defect, not a model verdict: the applier records
        # `error` and leaves the item untagged so a re-run picks it up.
        status = "truncated"
    elif not text.strip():
        # The model answered, with nothing. Distinct from an error, and
        # the signature the degeneracy check exists to catch.
        status = "empty"
    else:
        status = "ok"

    row = _merge(
        base,
        call_status=status,
        finish_reason=finish_reason,
        response_text=text,
        reasoning_text=reasoning or "",
        input_tokens=n_in,
        output_tokens=n_out,
    )
    if status == "truncated":
        row["error"] = f"output hit the {budget}-token budget"
    return row


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser():
    p = argparse.ArgumentParser(
        prog="python3 run_batch.py",
        description=(
            "Execute a screening manifest with vLLM offline batch "
            "inference. Invoke by file path; this file is not importable "
            "as a module on a cluster."
        ),
    )
    p.add_argument("--manifest", required=True,
                   help="Manifest JSONL (or .jsonl.gz) to execute.")
    p.add_argument("--model", default="",
                   help="Model to run, as your site's model cache names it. "
                        "Recorded verbatim in every response and in the CSV's "
                        "`model` column, which is what a manuscript cites. "
                        "Defaults to the manifest's model_hint.")
    p.add_argument("--model-revision", default="",
                   help="Optional revision/commit of the weights, recorded "
                        "for provenance. Nothing here resolves it for you.")
    p.add_argument("--out-dir", default="",
                   help="Directory for the responses file and run record "
                        "(default: alongside the manifest).")
    p.add_argument("--out", default="",
                   help="Explicit responses path, overriding --out-dir.")
    p.add_argument("--max-model-len", type=int, default=DEFAULT_MAX_MODEL_LEN,
                   help=f"Context window to serve (default: "
                        f"{DEFAULT_MAX_MODEL_LEN}). Requests that do not fit "
                        f"are recorded as errors rather than sent.")
    p.add_argument("--gpu-memory-utilization", type=float, default=0.90,
                   help="Fraction of VRAM vLLM may claim (default: 0.90).")
    p.add_argument("--temperature", type=float, default=0.0,
                   help="Sampling temperature (default: 0.0).")
    p.add_argument("--seed", type=int, default=42,
                   help="Sampling seed (default: 42), so a re-run of the same "
                        "manifest on the same model is reproducible.")
    p.add_argument("--limit", type=int, default=0,
                   help="Execute at most N requests (0 = all). Use a small "
                        "value for a pilot before committing a whole corpus — "
                        "an open-weight model's JSON compliance is worth "
                        "measuring on ten papers, not discovering on 240.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the pre-flight summary and exit. Imports no "
                        "GPU stack, so it works on a login node.")
    p.add_argument("--execute", action="store_true",
                   help="Actually load the model and generate.")
    p.add_argument("--confirm", action="store_true",
                   help="Required alongside --execute. The pre-flight summary "
                        "is printed first either way.")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not (args.dry_run or args.execute):
        raise SystemExit("choose --dry-run or --execute")
    if args.dry_run and args.execute:
        raise SystemExit("--dry-run and --execute are mutually exclusive")

    try:
        header, rows = read_manifest(args.manifest)
    except ManifestError as exc:
        # The message is the whole point; a traceback above it in a job
        # log buries the one line the user needs.
        raise SystemExit(f"ERROR: {exc}") from None
    if args.limit:
        rows = rows[: args.limit]

    args.model = args.model or rows[0].get("model_hint", "")
    if not args.model:
        raise SystemExit(
            "ERROR: no model. The manifest carries no model_hint, so pass "
            "--model with an ID your site's model cache serves."
        )

    if args.out:
        out_path = args.out
    else:
        out_dir = args.out_dir or os.path.dirname(os.path.abspath(args.manifest))
        out_path = os.path.join(
            out_dir,
            "{}__{}.responses.jsonl".format(header["run_id"], model_slug(args.model)),
        )

    print(preflight(header, rows, args, out_path))
    if args.dry_run:
        print("\nDRY RUN: nothing was loaded and nothing was generated.")
        return 0
    if not args.confirm:
        raise SystemExit(
            "REFUSING to run: --execute needs --confirm. A GPU allocation is "
            "a shared facility's resource and this run would spend it on the "
            "batch summarised above. Re-run with --confirm once that is what "
            "you meant."
        )

    started = time.time()
    hardware = hardware_provenance()
    responses, extra = execute(rows, args)
    elapsed = time.time() - started

    write_jsonl(out_path, responses)
    extra.update({
        "executor": "run_batch.py",
        "manifest": os.path.abspath(args.manifest),
        "manifest_sha256": header["sha256"],
        "n_requests": len(rows),
        "elapsed_s": round(elapsed, 2),
        "gpu_seconds": round(elapsed * max(1, hardware.get("gpu_count") or 0), 2),
        "responses_path": os.path.abspath(out_path),
        "python": sys.version.split()[0],
    })
    extra.update(hardware)
    record = summarise(
        header["run_id"], header["stage"], args.model, responses, extra,
    )
    record_path = write_run_record(run_record_path(out_path), record)

    print("\n" + "=" * 60)
    print(f"Wrote {len(responses)} response(s) to {out_path}")
    print(f"Run record: {record_path}")
    for k in sorted(record["status_counts"]):
        print(f"  {k}: {record['status_counts'][k]}")
    print(f"  model load: {record['model_load_s']:.1f}s   "
          f"generate: {record['generate_s']:.1f}s")
    print(f"  mean output tokens: {record['mean_output_tokens']}")
    if record.get("degenerate_output"):
        print(f"\nWARNING: {record['degenerate_output_note']}")
        # Not a non-zero exit: the responses and the record are real
        # evidence and belong on disk. The applier is what refuses, and
        # it refuses on this flag.
    return 0


if __name__ == "__main__":
    sys.exit(main())
