"""A three-item manifest, run on a real GPU cluster, by hand.

Opt in with `pytest -m live_cluster`. **This can never be a CI gate and
should not pretend to be.** It needs an account on a batch scheduler, a
GPU allocation, an SSH credential the plugin never sees, and queue time
measured in minutes to hours. What it buys is the one thing every
hermetic test in this repository cannot: proof that the file the plugin
writes is readable by the runner, that the runner is readable by the
site's Python, that the sbatch wrapper survives contact with a real
scheduler, and that what comes back is applicable.

That list is not hypothetical. `scripts/cluster/run_batch.py` imports
nothing from this repository, so the two halves of the contract are
written twice; `tests/unit/test_batch_roundtrip.py` connects them with a
fake `vllm` and is what CI runs. Everything below the Python — the
module system, the chat template of a real open-weight model, the
scheduler's environment, an offline compute node — is only ever
exercised here.

Nothing site-specific is written down. Every value comes from the
environment, so this file names no institution, no host, no partition,
no module and no model:

    ACADEMIC_RESEARCH_CLUSTER_HOST         ssh destination, e.g. user@login-node
    ACADEMIC_RESEARCH_CLUSTER_STAGING      remote directory to work in
    ACADEMIC_RESEARCH_CLUSTER_SBATCH_ARGS  the resource request, e.g.
                                           "--time=00:30:00 --gres=gpu:1 --mem=32G"
    ACADEMIC_RESEARCH_CLUSTER_MODEL        a model your site's cache holds
    ACADEMIC_RESEARCH_CLUSTER_SITE_ENV     remote path to your SITE_ENV snippet
    ACADEMIC_RESEARCH_CLUSTER_TIMEOUT      seconds to wait in the queue (default 3600)

The two tests are deliberately unequal. The first costs nothing — it
transfers three files and runs the login-node pre-flight, which imports
no GPU stack — and catches most of what goes wrong. Run it first, and
alone, when setting a new site up. The second spends an allocation.

**On polling:** `cluster-screening` tells the *agent* at most one
`squeue` per turn and never in a loop. This is a program that has been
told to wait for one job, so a paced loop is the honest implementation
of the same discipline — one query a minute, a hard cap, and a
`scancel` on the way out if the wait runs over. What the rule forbids is
an agent spinning on a scheduler; it does not forbid waiting.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from pathlib import Path

import abstract_screen
import batch_manifest as bm
import pytest

pytestmark = pytest.mark.live_cluster

REPO = Path(__file__).resolve().parents[2]
CLUSTER_DIR = REPO / "scripts" / "cluster"

#: Long enough that a cold model load (weights come off a shared
#: filesystem, minutes not seconds) is not mistaken for a hang.
SSH_TIMEOUT = 300

#: Deliberately explicit about the *literal* `DECISION:` token, because a
#: terser version was not enough. Asking only for "exactly two lines" left
#: `Qwen2.5-7B-Instruct` answering `exclude\nREASON: ...` on one item in
#: three: a correct judgement in a shape `parse_screening_response` cannot
#: read, which it therefore records as `borderline` with a PARSE ERROR.
#: The assertion at the bottom of this file stays strict because the
#: production parser is strict — it is the prompt that has to earn the
#: compliance, and a prompt this test ships is the right place to spend
#: the words. Real screening prompts should be at least this explicit.
SYSTEM_PROMPT = (
    "You screen paper abstracts for a systematic review of team "
    "coordination in software projects. Reply with exactly two lines. "
    "Begin the first line with the literal token DECISION: — the word "
    "DECISION followed by a colon — even when the answer seems obvious:\n"
    "DECISION: include|exclude|borderline\n"
    "REASON: one sentence."
)

#: Three abstracts with obvious, different answers. The point is not to
#: measure screening quality — it is that a real model, framed by a real
#: chat template, returns something the parser can read. An ambiguous
#: item would make a template failure look like a judgement call.
ITEMS = [
    {
        "key": "LIVECL001",
        "data": {
            "title": "Coordination practices in distributed software teams",
            "abstractNote": (
                "We interviewed 42 developers across six distributed teams "
                "to understand how they coordinate work across time zones, "
                "and derive four coordination mechanisms."
            ),
            "publicationTitle": "Journal of Software Engineering Research",
            "date": "2019",
            "DOI": "10.0000/live.cluster.001",
        },
    },
    {
        "key": "LIVECL002",
        "data": {
            "title": "Thermal tolerance of intertidal gastropods",
            "abstractNote": (
                "Heat-shock responses were measured in three gastropod "
                "species across a tidal gradient over two summers."
            ),
            "publicationTitle": "Marine Biology Letters",
            "date": "2021",
            "DOI": "10.0000/live.cluster.002",
        },
    },
    {
        "key": "LIVECL003",
        "data": {
            "title": "Agile adoption and team autonomy: a survey study",
            "abstractNote": (
                "A survey of 310 software teams relates agile practice "
                "adoption to perceived autonomy and coordination effort."
            ),
            "publicationTitle": "Information and Software Technology",
            "date": "2020",
            "DOI": "10.0000/live.cluster.003",
        },
    },
]


# ---------------------------------------------------------------------------
# Site settings, all from the environment
# ---------------------------------------------------------------------------


def _require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        pytest.skip(f"{name} not set; skipping live cluster test.")
    return val


def _settings() -> dict[str, str]:
    return {
        "host": _require_env("ACADEMIC_RESEARCH_CLUSTER_HOST"),
        "staging": _require_env("ACADEMIC_RESEARCH_CLUSTER_STAGING"),
        "sbatch_args": _require_env("ACADEMIC_RESEARCH_CLUSTER_SBATCH_ARGS"),
        "model": _require_env("ACADEMIC_RESEARCH_CLUSTER_MODEL"),
        "site_env": _require_env("ACADEMIC_RESEARCH_CLUSTER_SITE_ENV"),
    }


def _ssh(host: str, command: str, timeout: int = SSH_TIMEOUT) -> subprocess.CompletedProcess:
    """One remote command. `BatchMode` so a password prompt fails fast.

    A test that blocks on an interactive prompt looks exactly like a
    test that is waiting on the scheduler, and the difference matters at
    minute forty.
    """
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host, command],
        capture_output=True, text=True, timeout=timeout,
    )


def _scp(src: str, dst: str, timeout: int = SSH_TIMEOUT) -> None:
    proc = subprocess.run(
        ["scp", "-o", "BatchMode=yes", "-q", src, dst],
        capture_output=True, text=True, timeout=timeout,
    )
    assert proc.returncode == 0, f"scp {src} -> {dst} failed: {proc.stderr.strip()}"


def _emit(tmp_path: Path) -> tuple[Path, str]:
    """Write a real three-request manifest. No Zotero, no LLM.

    Built through `abstract_screen.build_manifest_rows` rather than by
    hand: a hand-written manifest tests the runner against a fixture
    nobody ships, which is how the two schemas drift apart.
    """
    run_id = bm.new_run_id(bm.STAGE_ABSTRACT)
    rows, skipped = abstract_screen.build_manifest_rows(
        ITEMS,
        run_id=run_id,
        system_prompt=SYSTEM_PROMPT,
        model="",
        prompt_version="live-cluster-test",
        doi_to_query={},
        library={"kind": "group", "id": "0"},
        collection="LIVECLUSTER",
    )
    assert not skipped, f"the fixture items were skipped: {skipped}"
    assert len(rows) == len(ITEMS)
    path = tmp_path / "live_cluster_requests.jsonl"
    bm.write_manifest(path, rows)
    return path, run_id


def _stage_three_files(cfg: dict[str, str], manifest: Path) -> str:
    """Copy runner, wrapper and manifest into a per-run remote directory.

    Three files out — never a clone of this repository. `scripts` is an
    ordinary directory name and a site's own software stack very likely
    has one; the runner is import-free of the plugin precisely so that
    no checkout is needed, and a checkout would reintroduce the
    namespace collision it was designed around.
    """
    remote = f"{cfg['staging'].rstrip('/')}/{manifest.stem}"
    # `logs/` before submitting, not inside the job: SLURM opens the
    # `--output` path itself, before the first line of the script runs,
    # and a missing directory there fails the job rather than creating
    # it.
    proc = _ssh(cfg["host"], f"mkdir -p {shlex.quote(remote)}/logs")
    assert proc.returncode == 0, f"cannot create {remote}: {proc.stderr.strip()}"
    for path in (CLUSTER_DIR / "run_batch.py", CLUSTER_DIR / "run_batch.sbatch", manifest):
        _scp(str(path), f"{cfg['host']}:{remote}/{path.name}")
    return remote


# ---------------------------------------------------------------------------
# 1. Costs no allocation
# ---------------------------------------------------------------------------


def test_the_preflight_runs_on_the_login_node(tmp_path: Path) -> None:
    """Transfer plus `--dry-run`: no GPU, no queue, no allocation spent.

    This is the test to run when pointing the plugin at a new site. It
    proves the transfer works, the site's Python can parse and execute
    the runner at all, and the runner can read a manifest this plugin
    wrote — three of the four ways a first attempt fails, for free.
    """
    cfg = _settings()
    manifest, run_id = _emit(tmp_path)
    remote = _stage_three_files(cfg, manifest)

    proc = _ssh(cfg["host"], " && ".join([
        f"cd {shlex.quote(remote)}",
        "python3 run_batch.py --manifest {} --model {} --dry-run".format(
            shlex.quote(manifest.name), shlex.quote(cfg["model"]),
        ),
    ]))
    assert proc.returncode == 0, (
        f"the login-node pre-flight failed:\n{proc.stdout}\n{proc.stderr}"
    )
    assert run_id in proc.stdout, "the pre-flight does not report the run_id"
    assert "DRY RUN" in proc.stdout
    assert "3" in proc.stdout, "the pre-flight does not report three requests"


def test_the_runner_refuses_to_execute_without_confirm(tmp_path: Path) -> None:
    """`--execute` alone must not spend anything.

    Cheap to check and worth checking on the real machine: the refusal
    is what stands between a mistyped command and someone else's
    allocation, and it is the one guard whose failure is invisible
    until it has already cost something.
    """
    cfg = _settings()
    manifest, _ = _emit(tmp_path)
    remote = _stage_three_files(cfg, manifest)

    proc = _ssh(cfg["host"], " && ".join([
        f"cd {shlex.quote(remote)}",
        "python3 run_batch.py --manifest {} --model {} --execute".format(
            shlex.quote(manifest.name), shlex.quote(cfg["model"]),
        ),
    ]))
    assert proc.returncode != 0, "the runner executed without --confirm"
    assert "REFUSING" in (proc.stdout + proc.stderr)


# ---------------------------------------------------------------------------
# 2. Spends an allocation
# ---------------------------------------------------------------------------


def _submit(cfg: dict[str, str], remote: str, manifest_name: str) -> str:
    command = " ".join([
        f"cd {shlex.quote(remote)}",
        "&&",
        f"MANIFEST={shlex.quote(remote + '/' + manifest_name)}",
        f"MODELS={shlex.quote(cfg['model'])}",
        f"SITE_ENV={shlex.quote(cfg['site_env'])}",
        f"RUNNER={shlex.quote(remote + '/run_batch.py')}",
        # One array job, one index. Never a loop of individual
        # submissions: schedulers and their operators both prefer it, and
        # it is one job ID to poll rather than N.
        f"sbatch --parsable --array=0 {cfg['sbatch_args']} run_batch.sbatch",
    ])
    proc = _ssh(cfg["host"], command)
    assert proc.returncode == 0, f"sbatch failed:\n{proc.stdout}\n{proc.stderr}"
    job_id = proc.stdout.strip().splitlines()[-1].split(";")[0].strip()
    assert job_id, f"no job id in sbatch output: {proc.stdout!r}"
    return job_id


def _wait_for(cfg: dict[str, str], job_id: str, timeout_s: int) -> None:
    """Poll once a minute until the job leaves the queue, then give up.

    `scancel` on timeout, and deliberately so: a test that walks away
    from a queued job leaves it to start unattended and write output
    nobody is waiting for.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        proc = _ssh(cfg["host"], f"squeue -j {shlex.quote(job_id)} -h -o %T")
        states = [s for s in proc.stdout.split() if s]
        if not states:
            return
        time.sleep(60)
    _ssh(cfg["host"], f"scancel {shlex.quote(job_id)}")
    pytest.fail(
        f"job {job_id} was still queued or running after {timeout_s}s and has "
        f"been cancelled. Raise ACADEMIC_RESEARCH_CLUSTER_TIMEOUT if your "
        f"site's queue is simply slow."
    )


def _fetch(cfg: dict[str, str], remote: str, run_id: str, dest: Path) -> tuple[Path, Path]:
    listing = _ssh(cfg["host"], f"ls {shlex.quote(remote)}")
    names = [n for n in listing.stdout.split() if n.startswith(run_id)]
    responses = [n for n in names if n.endswith(".responses.jsonl")]
    assert responses, (
        f"no responses file under {remote} — the job produced nothing. "
        f"Read the job log: {listing.stdout}"
    )
    record = [n for n in names if n.endswith(".run.json")]
    assert record, f"responses without a run record under {remote}: {names}"
    # Two files back, and only two.
    _scp(f"{cfg['host']}:{remote}/{responses[0]}", str(dest / responses[0]))
    _scp(f"{cfg['host']}:{remote}/{record[0]}", str(dest / record[0]))
    return dest / responses[0], dest / record[0]


def test_a_three_item_manifest_runs_and_comes_back_applicable(tmp_path: Path) -> None:
    """The whole round trip, minus Zotero. Spends a real allocation.

    Zotero is left out on purpose: the applier is covered hermetically
    and writing to a real library from a live test would need a library
    to write to. What is checked here is everything the applier would
    check before writing — schema, run identity, pairing, degeneracy,
    and whether the answers parse — which is precisely the set that
    depends on a real model.
    """
    cfg = _settings()
    timeout_s = int(os.environ.get("ACADEMIC_RESEARCH_CLUSTER_TIMEOUT", "3600"))
    manifest, run_id = _emit(tmp_path)
    remote = _stage_three_files(cfg, manifest)

    job_id = _submit(cfg, remote, manifest.name)
    print(f"\nsubmitted job {job_id}; waiting up to {timeout_s}s")
    _wait_for(cfg, job_id, timeout_s)

    responses_path, record_path = _fetch(cfg, remote, run_id, tmp_path)

    header, response_rows = bm.read_responses(responses_path)
    assert header["run_id"] == run_id, "the responses belong to a different run"
    assert len(response_rows) == len(ITEMS)

    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["run_id"] == run_id
    assert record["schema_version"] == bm.SCHEMA_VERSION, (
        "the runner on the cluster is a different version from this plugin; "
        "copy the current run_batch.py over rather than editing either side"
    )

    # The failure that looks like success: every row `ok`, nothing in any
    # of them. `refuse_if_degenerate` raises rather than returning, which
    # is the behaviour the applier depends on.
    bm.refuse_if_degenerate(record)
    assert not record.get("degenerate_output")

    statuses = record["status_counts"]
    assert statuses.get("ok") == len(ITEMS), (
        f"not every request was answered: {statuses}. prompt_format was "
        f"{record.get('prompt_format')!r}"
    )
    assert record.get("prompt_format") != "raw_completion", (
        "the runner found no usable chat template and fell back to raw "
        "completion — an instruction-tuned model handed an unframed prompt "
        "answers with a single end-of-turn token"
    )

    # Pairing is what the applier does first, and a run_id or request_id
    # mismatch here means responses that cannot be attributed to items.
    _, manifest_rows = bm.read_manifest(manifest)
    paired, unanswered, orphaned = bm.join_responses(manifest_rows, response_rows)
    assert not unanswered, f"requests with no response: {[r['item_key'] for r in unanswered]}"
    assert not orphaned, f"responses with no request: {orphaned}"
    assert len(paired) == len(ITEMS)

    # And the answers must be readable. A real open-weight model, framed
    # by a real chat template, is the only thing that can prove this.
    for req, resp in paired:
        assert resp["call_status"] == "ok", (req["item_key"], resp.get("error"))
        text = (resp.get("response_text") or "").strip()
        assert text, f"{req['item_key']} came back empty"
        assert "DECISION" in text.upper(), (
            f"{req['item_key']} did not answer in the requested format: {text[:200]!r}"
        )

    print(
        f"run {run_id}: {statuses}, mean output "
        f"{record['mean_output_tokens']} tokens, model load "
        f"{record.get('model_load_s')}s, generate {record.get('generate_s')}s"
    )
