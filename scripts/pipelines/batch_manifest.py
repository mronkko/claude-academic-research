"""The contract between assembling a screening run and executing it.

Screening normally calls an LLM per item from a thread pool. That needs
a provider that answers synchronously, which rules out the cheapest
compute a university has: a GPU node behind a batch scheduler, where a
job is submitted and collected minutes or hours later.

So the run splits in two, and this module is the seam:

    emit   →  <stage>_requests_<run_id>.jsonl   (+ .skipped.json sidecar)
              ... executed anywhere: a gateway, a laptop, a GPU node ...
    apply  ←  <stage>_responses_<run_id>__<model>.jsonl (+ .run.json)

**Assembly happens where the data is; execution happens where the
compute is.** Emission needs Zotero, the project's `screening_config.py`
and the PDF cache; execution needs a model and nothing else. Each
manifest row is therefore self-contained — prompt included — so the
executor can be a dumb loop with no knowledge of this plugin.

Three rules this module enforces, each learned from a real failure:

**A shrunken N must stay visible.** Items dropped at emit time — no PDF,
too long for the context — go to a sidecar with a reason, never silently
missing. An absence is not a result, and a reviewer reconstructing the
run needs to see that 12 of 240 never reached the model.

**A failed generation is recorded as a failed generation.** No
placeholder text, ever. Sentinel strings in a results file is how the
reference project learned that rule.

**A degenerate run is refused, not applied.** A model handed an
unframed prompt answers with one empty token and `finish_reason=stop`;
every row then looks healthy. Applying 240 of those writes 240
`borderline` tags into Zotero and nothing anywhere says the run was
worthless. `refuse_if_degenerate` is the stop.

Stdlib-only, so importing it costs neither orchestrator a PEP 723
dependency. No HTTP happens here.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: Bumped when a field changes meaning or disappears. The applier
#: refuses a version it does not know rather than guessing — a manifest
#: and the code that applies it can be separated by weeks and a `git
#: pull`, and a half-understood row would write wrong tags to Zotero.
SCHEMA_VERSION = 1

#: Mean output tokens at or below which a run is presumed degenerate.
#: One token is the classic unframed-prompt signature; two allows for a
#: model that emits a stop token plus a newline.
DEGENERATE_OUTPUT_TOKENS = 2

STAGE_ABSTRACT = "abstract_screening"
STAGE_FULLTEXT = "fulltext_coding"

#: Why a unit is in the sidecar rather than the manifest. Only the first
#: two produce a CSV row on apply; the rest were never in scope for this
#: run and saying otherwise would corrupt the counts.
SKIP_REASONS = (
    "no_pdf",
    "pdf_unreadable",
    "too_long_for_context",
    "already_tagged",
    "csv_error_row_not_rerun",
    "excluded_by_limit",
    "excluded_by_only_keys",
    "excluded_by_sample",
)
#: Skips that still owe the log a row, because they describe a real
#: attempt to screen a real item that could not be made.
SKIP_REASONS_WITH_ROWS = ("no_pdf", "pdf_unreadable")


class ManifestError(RuntimeError):
    """A manifest, sidecar or response file cannot be trusted."""


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def new_run_id(stage: str) -> str:
    """A run identifier: stage, UTC timestamp, no randomness.

    Sortable, greppable, and stable enough that a user can tell two runs
    apart in a directory listing without opening them.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stage}-{stamp}"


def model_slug(model: str) -> str:
    """A model ID reduced to something safe in a filename.

    `Qwen/Qwen3-30B-A3B-Instruct-FP8` contains a slash, which would
    otherwise create a directory.
    """
    return re.sub(r"[^A-Za-z0-9]+", "-", model or "").strip("-") or "model"


def request_id(run_id: str, item_key: str, ordinal: int = 0) -> str:
    """The join key between a manifest row and its response.

    Carries an ordinal so a future mode emitting more than one request
    per item — a second prompt, a re-ask — needs no new identifier.
    """
    return f"{run_id}:{item_key}:{ordinal}"


def sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# JSONL IO. `.gz` is detected by suffix on both read and write, because a
# full-text manifest is one 720 kB prompt per paper and 240 papers of
# that is ~170 MB to move across a network.
# ---------------------------------------------------------------------------


def _open_text(path: Path, mode: str):
    if str(path).endswith(".gz"):
        return gzip.open(path, mode + "t", encoding="utf-8")
    return path.open(mode, encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _open_text(path, "w") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        raise ManifestError(f"no such file: {path}")
    rows: list[dict] = []
    with _open_text(path, "r") as fh:
        for n, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise ManifestError(f"{path}:{n}: not valid JSON — {e}") from e
            if not isinstance(obj, dict):
                raise ManifestError(f"{path}:{n}: expected an object")
            rows.append(obj)
    return rows


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def write_manifest(path: Path, rows: list[dict]) -> Path:
    """Write request rows, sorted by `(item_key, ordinal)`.

    Sorted so that any future multi-request-per-item mode keeps a
    document's requests consecutive, which is what lets a vLLM-style
    executor reuse the shared prefix instead of thrashing its cache.

    Note the inversion from the batch-inference literature, which groups
    by *document*: there the document was the shared prefix across many
    prompts. Here the **system prompt** is identical on every row and the
    document is the per-item part, so the ordinary system-then-user
    layout is already prefix-optimal. A naive port gets this backwards.
    """
    ordered = sorted(rows, key=lambda r: (r.get("item_key", ""), r.get("ordinal", 0)))
    return _write_jsonl(path, ordered)


def read_manifest(path: Path) -> tuple[dict, list[dict]]:
    """`(header, rows)`. Raises ManifestError on drift or inconsistency.

    `header` is derived from the rows rather than stored separately —
    every row is self-contained so the executor never has to parse a
    envelope — but the derivation asserts the rows agree about which run,
    stage and schema they belong to. A manifest concatenated from two
    runs is a real hazard once files are being moved by hand.
    """
    rows = _read_jsonl(path)
    if not rows:
        raise ManifestError(
            f"{path} is empty. An empty manifest is not an empty result — "
            f"re-assemble it, and read the .skipped.json sidecar to see "
            f"why every unit was dropped."
        )
    versions = {r.get("schema_version") for r in rows}
    if versions != {SCHEMA_VERSION}:
        raise ManifestError(
            f"{path}: manifest schema {sorted(versions)} but this build "
            f"understands {SCHEMA_VERSION}. Re-emit the manifest with the "
            f"current version rather than applying a format it may "
            f"misread."
        )
    for field in ("run_id", "stage"):
        seen = {r.get(field) for r in rows}
        if len(seen) != 1:
            raise ManifestError(
                f"{path}: rows disagree on {field}: {sorted(map(str, seen))}. "
                f"Two runs' rows in one file would apply one run's "
                f"decisions under the other's identity."
            )
    ids = [r.get("request_id") for r in rows]
    if len(set(ids)) != len(ids):
        raise ManifestError(f"{path}: duplicate request_id values")
    return {
        "run_id": rows[0]["run_id"],
        "stage": rows[0]["stage"],
        "schema_version": SCHEMA_VERSION,
        "n_requests": len(rows),
        "sha256": file_sha256(path),
    }, rows


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Skipped sidecar
# ---------------------------------------------------------------------------


def skipped_path(manifest_path: Path) -> Path:
    """The sidecar beside a manifest, `.gz` suffix stripped."""
    name = manifest_path.name
    if name.endswith(".gz"):
        name = name[: -len(".gz")]
    if name.endswith(".jsonl"):
        name = name[: -len(".jsonl")]
    return manifest_path.parent / f"{name}.skipped.json"


def write_skipped(
    path: Path,
    *,
    run_id: str,
    stage: str,
    skipped: list[dict],
    selection: dict,
    n_requested: int,
) -> Path:
    """Record every unit that did not make it into the manifest.

    This file is the difference between "we screened 228 papers" and "we
    screened 228 of 240; 10 had no PDF and 2 were longer than the
    model's context". The second is a finding; the first, told alone, is
    a misreport.
    """
    unknown = {s.get("skip_reason") for s in skipped} - set(SKIP_REASONS)
    if unknown:
        raise ManifestError(f"unknown skip_reason(s): {sorted(map(str, unknown))}")
    counts: dict[str, int] = {}
    for s in skipped:
        counts[s["skip_reason"]] = counts.get(s["skip_reason"], 0) + 1
    payload = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "stage": stage,
        "generated_at": datetime.now(UTC).isoformat(),
        "n_requested": n_requested,
        "n_skipped": len(skipped),
        "reason_counts": dict(sorted(counts.items())),
        "selection": selection,
        "skipped": skipped,
        "note": (
            "Units excluded from this run, each with its reason. A "
            "shrunken N must be visible; an absence is not a result."
        ),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def read_skipped(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ManifestError(f"{path}: {e}") from e


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------

#: Markers separating a reasoning model's thinking channel from its
#: answer. The raw stream is recorded verbatim; this is only used to find
#: where the answer starts, because a JSON parser handed the whole
#: stream finds the model's scratch work first.
REASONING_MARKERS = (
    "<|start|>assistant<|channel|>final<|message|>",
    "assistantfinal",
    "</think>",
    "<|im_start|>assistant",
)


def split_reasoning(text: str) -> tuple[str, str]:
    """`(reasoning, answer)`. `reasoning` is `""` when there is none.

    Splits on the last marker found, since a model may emit several
    channel switches before settling.
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


def read_responses(path: Path) -> tuple[dict, list[dict]]:
    """`(header, rows)` for a responses file, validated like a manifest."""
    rows = _read_jsonl(path)
    if not rows:
        raise ManifestError(f"{path} contains no responses")
    versions = {r.get("schema_version") for r in rows}
    if versions != {SCHEMA_VERSION}:
        raise ManifestError(
            f"{path}: response schema {sorted(versions)} but this build "
            f"understands {SCHEMA_VERSION}."
        )
    run_ids = {r.get("run_id") for r in rows}
    if len(run_ids) != 1:
        raise ManifestError(f"{path}: rows disagree on run_id: {sorted(run_ids)}")
    models = {r.get("model") for r in rows}
    return {
        "run_id": rows[0]["run_id"],
        "model": rows[0].get("model", ""),
        "models": sorted(m for m in models if m),
        "n_responses": len(rows),
    }, rows


def join_responses(
    manifest_rows: list[dict], response_rows: list[dict],
) -> tuple[list[tuple[dict, dict]], list[dict], list[str]]:
    """Pair each request with its response.

    Returns `(paired, unanswered, orphaned)`:

    - `paired` — request and response, in manifest order.
    - `unanswered` — manifest rows with no response. Not an error: a job
      that ran out of walltime answers some. They stay untagged so a
      re-run picks them up, and the caller must say how many there were.
    - `orphaned` — response ids absent from the manifest. Usually the
      wrong pair of files; always worth naming rather than ignoring.
    """
    m_run = {r.get("run_id") for r in manifest_rows}
    r_run = {r.get("run_id") for r in response_rows}
    if m_run != r_run:
        raise ManifestError(
            f"run_id mismatch: manifest {sorted(m_run)} vs responses "
            f"{sorted(r_run)}. These files are from different runs; "
            f"applying them together would tag items from one run with "
            f"another run's decisions."
        )
    by_id = {r["request_id"]: r for r in response_rows if r.get("request_id")}
    paired: list[tuple[dict, dict]] = []
    unanswered: list[dict] = []
    for req in manifest_rows:
        resp = by_id.pop(req.get("request_id", ""), None)
        if resp is None:
            unanswered.append(req)
        else:
            paired.append((req, resp))
    return paired, unanswered, sorted(by_id)


# ---------------------------------------------------------------------------
# Run record
# ---------------------------------------------------------------------------


def run_record_path(responses_path: Path) -> Path:
    name = responses_path.name
    for suffix in (".gz", ".jsonl"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return responses_path.parent / f"{name}.run.json"


def summarise_run(
    run_id: str,
    *,
    stage: str,
    model: str,
    responses: list[dict],
    extra: dict[str, Any] | None = None,
) -> dict:
    """Build the run record, including the degeneracy verdict.

    `model_load_s` and `generate_s` belong in `extra` and are kept
    separate on purpose: on a GPU node, loading weights takes 77–382 s
    against seconds of generation for a small batch, and that ratio is
    what tells a user how big to make the next manifest.
    """
    statuses: dict[str, int] = {}
    for r in responses:
        s = str(r.get("call_status", "error"))
        statuses[s] = statuses.get(s, 0) + 1
    out_tokens = [
        int(r.get("output_tokens") or 0) for r in responses
        if r.get("call_status") == "ok"
    ]
    mean_out = (sum(out_tokens) / len(out_tokens)) if out_tokens else 0.0
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "stage": stage,
        "model": model,
        "n_responses": len(responses),
        "status_counts": dict(sorted(statuses.items())),
        "input_tokens": sum(int(r.get("input_tokens") or 0) for r in responses),
        "output_tokens": sum(int(r.get("output_tokens") or 0) for r in responses),
        "mean_output_tokens": round(mean_out, 2),
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    record.update(extra or {})
    if responses and mean_out <= DEGENERATE_OUTPUT_TOKENS:
        record["degenerate_output"] = True
        record["degenerate_output_note"] = (
            f"mean output {mean_out:.2f} tokens per answered request. The "
            f"model replied with (almost) nothing — usually an unframed "
            f"prompt, the wrong chat template, or a stop token inside the "
            f"payload. Do not score or apply this run."
        )
    return record


def append_run_record(path: Path, record: dict) -> Path:
    """Append to a JSONL history. Never rewrites; a run record is
    evidence about a run that happened."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def load_run_record(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ManifestError(f"{path}: {e}") from e


def refuse_if_degenerate(record: dict, *, force: bool = False) -> None:
    """Stop before a degenerate run reaches Zotero.

    The highest-value guard in the batch path. Without it, a run where
    every answer was one empty token applies cleanly: the CSV fills with
    `borderline`, every item gets tagged, and nothing distinguishes it
    from a real screening pass. The tags are the resume source of truth,
    so the next run then skips all of them.
    """
    if not record.get("degenerate_output"):
        return
    note = record.get("degenerate_output_note", "the run looks degenerate")
    if force:
        print(
            f"WARNING: applying a run flagged degenerate — {note}\n"
            f"         --force-apply was given, so this is going into "
            f"Zotero anyway. The stage tags it writes will make a re-run "
            f"skip these items.",
            file=sys.stderr, flush=True,
        )
        return
    raise SystemExit(
        f"REFUSING to apply: {note}\n"
        f"  Re-run the generation and check the executor's prompt_format "
        f"— a raw-completion fallback on an instruction-tuned model "
        f"produces exactly this.\n"
        f"  Pass --force-apply to override, if you are certain."
    )
