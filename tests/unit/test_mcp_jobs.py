"""Job lifecycle of the plugin's MCP server: start_*, job_status,
job_answer, cancel_job, list_jobs (BACKLOG P21 phase 3b).

The launcher is replaced (`ACADEMIC_RESEARCH_MCP_LAUNCHER`) with a fake
script that speaks the real protocols — `--progress-json` events and the
`ControlFileChannel` handshake — so the real `job_runner.py` runs it
detached exactly as it would a pipeline script, with no Zotero, network
or model anywhere. Most tests call the server in process; the survival
test uses two separate server processes to show that nothing about a job
lives in the server.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MCP_DIR = REPO / "mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import job_runner  # noqa: E402
import server  # noqa: E402

FAKE = r'''
import json, os, signal, sys, time

def flag(name):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else ""

progress, control = flag("--progress-json"), flag("--control-file")
mode = os.environ["FAKE_MODE"]
marker_dir = os.environ["FAKE_MARKERS"]

def emit(event, **fields):
    with open(progress, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": time.time(), "event": event, **fields}) + "\n")

def mark(name):
    open(os.path.join(marker_dir, name), "w").close()

emit("start", script="fake", queued=2)
print("fake script running", flush=True)
if mode == "ok":
    for i in (1, 2):
        emit("item", key=f"K{i}", outcome="include", done=i, queued=2)
    emit("summary", counts={"include": 2})
    emit("done", exit_code=0, ok=True)
    sys.exit(0)
if mode == "fail":
    print("ERROR: the fake failed", flush=True)
    sys.exit(3)
if mode == "ask":
    with open(control, "w", encoding="utf-8") as fh:
        json.dump({"state": "awaiting_user", "kind": "line",
                   "prompt": "Apply DOI 10.1/x? [y/N]", "seq": 1}, fh)
    reply = control + ".reply"
    while True:
        try:
            with open(reply, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            time.sleep(0.05)
            continue
        if data.get("seq") == 1:
            break
    os.remove(reply)
    with open(control, "w", encoding="utf-8") as fh:
        json.dump({"state": "running", "seq": 1}, fh)
    emit("item", key="K1", outcome="answer:" + data["answer"], done=1, queued=1)
    sys.exit(0)
if mode == "stubborn":
    signal.signal(signal.SIGINT, signal.SIG_IGN)
mark("started")
try:
    while True:
        time.sleep(0.05)
except KeyboardInterrupt:
    mark("flushed")
    sys.exit(130)
'''

FINAL = ("succeeded", "failed", "cancelled")


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Path:
    proj = tmp_path / "project"
    proj.mkdir()
    fake = tmp_path / "fake_script.py"
    fake.write_text(FAKE, encoding="utf-8")
    markers = tmp_path / "markers"
    markers.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    monkeypatch.setenv(server.LAUNCHER_ENV, json.dumps([sys.executable, str(fake)]))
    monkeypatch.setenv(server.GRACE_ENV, "1")
    monkeypatch.setenv("FAKE_MARKERS", str(markers))
    monkeypatch.setenv("FAKE_MODE", "ok")
    yield proj
    # Never leave a detached process behind, whatever the test did.
    for _job_dir, job in server._iter_jobs(proj):
        for key in ("pid", "runner_pid"):
            pid = job.get(key)
            if server._pid_alive(pid):
                try:
                    if sys.platform == "win32":
                        os.kill(pid, signal.SIGTERM)
                    else:
                        os.killpg(pid, signal.SIGKILL)
                except OSError:
                    pass


def _call(name: str, arguments: dict | None = None) -> dict:
    return server.call_tool(name, arguments or {})


def _wait(job_id: str, until=lambda s: s["status"] in FINAL, timeout: float = 30) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        status = _call("job_status", {"job_id": job_id})
        if until(status):
            return status
        if time.monotonic() > deadline:
            pytest.fail(f"job {job_id} did not get there in {timeout}s: {status}")
        time.sleep(0.1)


def _wait_for_file(path: Path, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            pytest.fail(f"{path} never appeared")
        time.sleep(0.05)


# ---------------------------------------------------------------------------


def test_a_job_runs_to_success_and_reports_progress(project: Path) -> None:
    started = _call("start_search", {"streams": "keyword"})
    assert started["ok"] and started["status"] == "running"
    job_id = started["job_id"]
    assert job_id.startswith("search-")

    status = _wait(job_id)
    assert status["status"] == "succeeded" and status["exit_code"] == 0
    progress = status["progress"]
    assert progress["done"] == 2 and progress["queued"] == 2
    assert progress["outcomes"] == {"include": 2}
    assert progress["summary"]["counts"] == {"include": 2}
    assert progress["finished"]["ok"] is True
    assert "fake script running" in status["log_tail"]

    job_dir = project / ".claude" / "jobs" / job_id
    job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    assert job["tool"] == "start_search"
    assert job["finished_at"] and isinstance(job["pid"], int)
    # The server appended the progress file to the script's own argv.
    assert job["argv"][-2:] == ["--progress-json", str(job_dir / "progress.jsonl")]
    assert "--streams" in job["argv"] and "--control-file" not in job["argv"]


def test_a_non_zero_exit_is_failed(project: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "fail")
    job_id = _call("start_enrich_abstracts", {"dry_run": True})["job_id"]
    status = _wait(job_id)
    assert status["status"] == "failed" and status["exit_code"] == 3
    assert "ERROR: the fake failed" in status["log_tail"]


def test_a_question_is_relayed_and_answered(project: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "ask")
    job_id = _call("start_enrich_dois", {"group": "123"})["job_id"]
    job = json.loads((project / ".claude" / "jobs" / job_id / "job.json")
                     .read_text(encoding="utf-8"))
    assert "--control-file" in job["argv"]

    waiting = _wait(job_id, until=lambda s: s["status"] == "awaiting_user")
    assert waiting["prompt"] == "Apply DOI 10.1/x? [y/N]"
    assert "job_answer" in waiting["next"]

    answered = _call("job_answer", {"job_id": job_id, "answer": "y"})
    assert answered["ok"] and answered["seq"] == 1

    done = _wait(job_id)
    assert done["status"] == "succeeded"
    assert done["progress"]["outcomes"] == {"answer:y": 1}


def test_answering_a_job_that_is_not_asking_is_refused(project: Path) -> None:
    job_id = _call("start_search")["job_id"]
    _wait(job_id)
    with pytest.raises(server.ToolInputError, match="not waiting"):
        _call("job_answer", {"job_id": job_id, "answer": "y"})


def test_cancel_interrupts_first_so_the_script_can_flush(project: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "hang")
    markers = Path(os.environ["FAKE_MARKERS"])
    job_id = _call("start_search")["job_id"]
    _wait_for_file(markers / "started")

    reply = _call("cancel_job", {"job_id": job_id})
    assert reply["ok"] and reply["cancel_requested"]

    status = _wait(job_id)
    assert status["status"] == "cancelled"
    if sys.platform != "win32":
        # SIGINT reached the script as KeyboardInterrupt — the path where
        # the real scripts flush their CSVs and tag buffers.
        assert (markers / "flushed").exists()
        assert status["exit_code"] == 130
    again = _call("cancel_job", {"job_id": job_id})
    assert again["status"] == "cancelled" and "already finished" in again["note"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal escalation")
def test_cancel_escalates_to_sigterm_when_sigint_is_ignored(project: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "stubborn")
    markers = Path(os.environ["FAKE_MARKERS"])
    job_id = _call("start_search")["job_id"]
    _wait_for_file(markers / "started")
    _call("cancel_job", {"job_id": job_id})
    status = _wait(job_id)
    assert status["status"] == "cancelled"
    assert status["exit_code"] == -signal.SIGTERM
    assert not (markers / "flushed").exists()


def test_a_second_zotero_writer_is_refused_while_one_runs(project: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "hang")
    markers = Path(os.environ["FAKE_MARKERS"])
    first = _call("start_import", {"collection": "Review"})["job_id"]
    _wait_for_file(markers / "started")
    with pytest.raises(server.ToolInputError, match=first):
        _call("start_enrich_abstracts")
    # A dry run writes nothing, so it may run alongside.
    second = _call("start_enrich_abstracts", {"dry_run": True})["job_id"]
    for job_id in (first, second):
        _call("cancel_job", {"job_id": job_id})
        assert _wait(job_id)["status"] == "cancelled"


def test_list_jobs_newest_first(project: Path) -> None:
    ids = []
    for _ in range(2):
        ids.append(_call("start_search")["job_id"])
        _wait(ids[-1])
    listed = _call("list_jobs")
    assert listed["total"] == 2
    assert {j["job_id"] for j in listed["jobs"]} == set(ids)
    assert all(j["status"] == "succeeded" for j in listed["jobs"])
    starts = [j["started_at"] for j in listed["jobs"]]
    assert starts == sorted(starts, reverse=True)


def test_artifacts_written_during_the_job_are_reported(project: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "hang")
    markers = Path(os.environ["FAKE_MARKERS"])
    stale = project / "screening" / "abstract_screening.csv"
    stale.parent.mkdir()
    stale.write_text("old\n", encoding="utf-8")
    os.utime(stale, (time.time() - 3600, time.time() - 3600))
    job_id = _call("start_abstract_screen", {"collection": "ABCD1234",
                                             "dry_run": True})["job_id"]
    _wait_for_file(markers / "started")
    assert _call("job_status", {"job_id": job_id})["artifacts"] == []
    stale.write_text("new\n", encoding="utf-8")
    assert _call("job_status", {"job_id": job_id})["artifacts"] == [
        "screening/abstract_screening.csv"]
    _call("cancel_job", {"job_id": job_id})
    _wait(job_id)


# ---------------------------------------------------------------------------
# Refusals: nothing starts
# ---------------------------------------------------------------------------


def test_a_path_outside_the_project_is_refused(project: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.csv"
    outside.write_text("doi\n", encoding="utf-8")
    for value in (str(outside), "../outside.csv"):
        with pytest.raises(server.ToolInputError, match="outside the project"):
            _call("start_import", {"input": value})
    assert not (project / ".claude" / "jobs").exists()


@pytest.mark.parametrize("job_id", ["../../etc", "search-20261015-120000-zzzz",
                                    "search-20261015-120000-abcd/.."])
def test_job_ids_are_validated_before_touching_the_filesystem(project, job_id) -> None:
    with pytest.raises(server.ToolInputError):
        _call("job_status", {"job_id": job_id})


def test_an_unknown_job_is_reported(project: Path) -> None:
    with pytest.raises(server.ToolInputError, match="no job"):
        _call("job_status", {"job_id": "search-20261015-120000-abcd"})


def test_bad_arguments_start_nothing(project: Path) -> None:
    with pytest.raises(server.ToolInputError):
        _call("start_abstract_screen", {"collection": "not a key"})
    with pytest.raises(server.ToolInputError, match="cannot be combined"):
        _call("start_fulltext_code", {"collection": "ABCD1234", "rerun": True,
                                      "full_recode": True})
    assert not (project / ".claude" / "jobs").exists()


def test_a_vanished_runner_reads_as_failed(project: Path) -> None:
    job_dir = project / ".claude" / "jobs" / "search-20261015-120000-abcd"
    job_dir.mkdir(parents=True)
    job_runner.write_json(job_dir / "job.json", {
        "job_id": job_dir.name, "tool": "start_search",
        "started_at": "2026-10-15T12:00:00+00:00", "status": "running",
        "runner_pid": 2 ** 22 + 12345, "pid": None,
    })
    status = _call("job_status", {"job_id": job_dir.name})
    assert status["status"] == "failed"
    assert "without recording" in status["error"]


# ---------------------------------------------------------------------------
# Survival: the job outlives the server that started it
# ---------------------------------------------------------------------------


def _session(messages: list[dict], env: dict) -> list[dict]:
    proc = subprocess.run(
        [sys.executable, str(MCP_DIR / "server.py")],
        input="\n".join(json.dumps(m) for m in messages) + "\n",
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def _tool_call(id_: int, name: str, arguments: dict) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}


def test_a_job_survives_its_server_and_a_fresh_server_reads_it(
        project: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "hang")
    markers = Path(os.environ["FAKE_MARKERS"])
    env = dict(os.environ)
    [started] = _session([_tool_call(1, "start_search", {})], env)
    job_id = json.loads(started["result"]["content"][0]["text"])["job_id"]

    # That server process has exited; the job has not.
    _wait_for_file(markers / "started")
    [status] = _session([_tool_call(2, "job_status", {"job_id": job_id})], env)
    payload = json.loads(status["result"]["content"][0]["text"])
    assert payload["status"] == "running"
    assert status["result"]["isError"] is False

    [cancel] = _session([_tool_call(3, "cancel_job", {"job_id": job_id})], env)
    assert json.loads(cancel["result"]["content"][0]["text"])["cancel_requested"]
    assert _wait(job_id)["status"] == "cancelled"


def test_the_pdf_plan_job_runs_beside_a_zotero_writer(project: Path, monkeypatch) -> None:
    """Planning never writes to Zotero, so the one-writer rule does not
    hold it back — and it gets the resolver sweep's argv."""
    monkeypatch.setenv("FAKE_MODE", "hang")
    markers = Path(os.environ["FAKE_MARKERS"])
    writer = _call("start_import", {"collection": "Review"})["job_id"]
    _wait_for_file(markers / "started")
    plan = _call("start_pdf_plan", {"group": "12"})["job_id"]
    job = json.loads((project / ".claude" / "jobs" / plan / "job.json")
                     .read_text(encoding="utf-8"))
    argv = job["argv"]
    assert argv[argv.index("--plan-strategy"):argv.index("--progress-json")] == [
        "--plan-strategy", "--plan-lookups", "all", "--group", "12"]
    for job_id in (writer, plan):
        _call("cancel_job", {"job_id": job_id})
        assert _wait(job_id)["status"] == "cancelled"
