"""Run one MCP job to completion and record how it ended.

    python job_runner.py <job_dir> -- <argv...>

The MCP server starts this detached (its own session / process group)
and returns at once; the runner outlives the server, so a job survives
the host restarting or the server being replaced. It is the only writer
of `job.json` after the server creates it:

- starts `argv` with the job's `run.log` as stdout and stderr, in its own
  process group so a cancel reaches the script and any child it spawned
  (`uv run` → python);
- records the child's pid, waits, and records `exit_code`, `status`
  (`succeeded` / `failed` / `cancelled`) and `finished_at`;
- watches for `cancel.json` (written by the server's `cancel_job`) and
  escalates: SIGINT first, so the scripts' KeyboardInterrupt paths flush
  their CSVs and tag buffers; SIGTERM after `grace_s`; SIGKILL after
  twice that. Windows: CTRL_BREAK_EVENT, then terminate(), then kill().

The escalation lives here, not in the server, because the server answers
one request at a time and a tool call must return immediately.

Stdlib only, like the server.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

JOB_FILE = "job.json"
LOG_FILE = "run.log"
CANCEL_FILE = "cancel.json"
POLL_S = 0.25
DEFAULT_GRACE_S = 20.0

IS_WINDOWS = sys.platform == "win32"


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def write_json(path: Path, payload: dict) -> None:
    """Atomic write, so a reader never sees half a document."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _update(job_dir: Path, **fields) -> None:
    path = job_dir / JOB_FILE
    write_json(path, {**read_json(path), **fields})


def _child_group_kwargs() -> dict:
    if IS_WINDOWS:
        # Its own group so CTRL_BREAK_EVENT targets it alone; no window,
        # since the runner itself has no console to share.
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def _signal(child: subprocess.Popen, stage: int) -> None:
    """Stage 1 interrupt, 2 terminate, 3 kill — the whole group on POSIX."""
    try:
        if IS_WINDOWS:
            if stage == 1:
                child.send_signal(signal.CTRL_BREAK_EVENT)
            elif stage == 2:
                child.terminate()
            else:
                child.kill()
            return
        sig = {1: signal.SIGINT, 2: signal.SIGTERM, 3: signal.SIGKILL}[stage]
        os.killpg(child.pid, sig)
    except (OSError, ValueError):
        pass  # already gone, or (Windows) no console to deliver the break to


def run(job_dir: Path, argv: list[str]) -> int:
    job = read_json(job_dir / JOB_FILE)
    grace = float(job.get("grace_s") or DEFAULT_GRACE_S)
    env = dict(os.environ)
    # Stream the log as it is written, and in UTF-8 on every OS: the
    # scripts print arrows and em dashes and not every print flushes.
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    with (job_dir / LOG_FILE).open("ab") as log:
        try:
            child = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                env=env, **_child_group_kwargs(),
            )
        except OSError as e:
            log.write(f"job_runner: could not start {argv[0]!r}: {e}\n".encode())
            _update(job_dir, status="failed", exit_code=None,
                    error=f"could not start: {e}", finished_at=now_iso(),
                    runner_pid=os.getpid())
            return 1
        _update(job_dir, pid=child.pid, runner_pid=os.getpid())

        cancelled_at: float | None = None
        stage = 0
        while True:
            try:
                rc = child.wait(timeout=POLL_S)
                break
            except subprocess.TimeoutExpired:
                pass
            if cancelled_at is None:
                if (job_dir / CANCEL_FILE).exists():
                    cancelled_at = time.monotonic()
                    stage = 1
                    _signal(child, stage)
            elif stage < 3 and time.monotonic() - cancelled_at > grace * stage:
                stage += 1
                _signal(child, stage)

    if cancelled_at is not None:
        status = "cancelled"
    else:
        status = "succeeded" if rc == 0 else "failed"
    _update(job_dir, status=status, exit_code=rc, finished_at=now_iso())
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 4 or argv[2] != "--":
        print("usage: job_runner.py <job_dir> -- <argv...>", file=sys.stderr)
        return 2
    return run(Path(argv[1]), argv[3:])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
