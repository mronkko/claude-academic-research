# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""The academic-research plugin's MCP server: pipeline scripts as tools.

A stdio MCP server (newline-delimited JSON-RPC 2.0) implementing the
minimum an MCP host needs: `initialize`, `notifications/initialized`,
`ping`, `tools/list` and `tools/call`. Hand-rolled on the standard library
on purpose (BACKLOG P21): no SDK to resolve means instant start and
nothing to install beyond uv, which launches it:

    uv run --no-project --quiet <plugin>/mcp/server.py

The tools are declared in `tool_registry.py`. Each call builds an argv
list and runs the existing plugin script as a subprocess in the project
directory — no shell, and nothing from `scripts/` is imported here, so a
script behaves exactly as it does when run from a terminal. The server
never reads the plugin's config.toml; the scripts do, as they always have.

Short tools run the script to completion and return a summary. Job
tools (`start_*`) return at once with a job_id: the script runs under
`job_runner.py`, detached, and every job tool reads the job's directory
under `.claude/jobs/`, so the server itself holds no state and a job
outlives the server that started it. This loop handles one request at a
time, which is why nothing here ever waits on a job.

Project directory: `CLAUDE_PROJECT_DIR` when the host sets it (Claude
Code does; a stdio server's cwd is undocumented there), else the cwd.

Test hook: `ACADEMIC_RESEARCH_MCP_LAUNCHER`, a JSON array, replaces the
`uv run …` prefix so the protocol tests can run a stub instead of a real
pipeline. It is not a user setting.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import job_runner  # noqa: E402
import tool_registry as reg  # noqa: E402

PLUGIN_ROOT = _HERE.parent
SERVER_NAME = "academic-research"
LAUNCHER_ENV = "ACADEMIC_RESEARCH_MCP_LAUNCHER"

#: Newest first. An `initialize` asking for one of these gets it back;
#: anything else gets the newest, per the MCP version negotiation rule.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

#: Lines of combined output returned as `log_tail`, and a hard cap on its
#: size. Claude Code warns at 10k tokens of tool output and truncates at
#: 25k, so a result must stay far below either however chatty a script is.
LOG_TAIL_LINES = 40
LOG_TAIL_CHARS = 4000

#: Where each tool's full output from its last run is kept, relative to
#: the project. `.claude/` is the plugin's gitignored scratch area.
LOG_DIR = Path(".claude") / "mcp" / "logs"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class ToolInputError(ValueError):
    """An argument the server refuses before running anything."""


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

def plugin_version() -> str:
    try:
        manifest = PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
        return str(json.loads(manifest.read_text(encoding="utf-8"))["version"])
    except (OSError, ValueError, KeyError):
        return "0"


def project_dir() -> Path:
    env = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    if env and Path(env).is_dir():
        return Path(env).resolve()
    return Path.cwd().resolve()


def launcher() -> list[str]:
    """The argv prefix that runs a plugin script.

    Every script — the PEP 723 pipeline scripts and the stdlib setup ones
    alike — goes through `uv run`, so there is one launcher to reason
    about. `--no-project` keeps uv from discovering and syncing a
    pyproject.toml in the user's project; `--python >=3.11` makes the
    stdlib scripts, which carry no inline metadata, run on a Python the
    plugin supports rather than whatever is first on PATH.
    """
    override = os.environ.get(LAUNCHER_ENV, "").strip()
    if override:
        prefix = json.loads(override)
        if not (isinstance(prefix, list) and prefix
                and all(isinstance(p, str) for p in prefix)):
            raise RuntimeError(f"{LAUNCHER_ENV} must be a JSON array of strings")
        return prefix
    uv = shutil.which("uv")
    if not uv:
        raise FileNotFoundError(
            "uv was not found on PATH. The plugin's scripts run through uv: "
            "install it from https://docs.astral.sh/uv/ and restart the host."
        )
    return [uv, "run", "--no-project", "--quiet", "--python", ">=3.11"]


def confine(value: str, project: Path, name: str) -> str:
    """Resolve `value` against the project and refuse anything outside it.

    Symlinks are resolved first, so a link inside the project that points
    out of it is refused too. Returns an absolute path: the script runs
    with the project as cwd, but an absolute path needs no such agreement.
    """
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ToolInputError(f"{name}: expected a non-empty path")
    candidate = Path(value.strip()).expanduser()
    if not candidate.is_absolute():
        candidate = project / candidate
    resolved = candidate.resolve()
    if resolved != project and project not in resolved.parents:
        raise ToolInputError(
            f"{name}: {value!r} is outside the project directory {project}"
        )
    return str(resolved)


# ---------------------------------------------------------------------------
# Argument handling
# ---------------------------------------------------------------------------

def _check_type(param: reg.Param, value: object, *, allow_dash: bool = False) -> None:
    if param.kind == "repeat":
        ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
        want = "an array of strings"
    elif param.type == "boolean":
        ok, want = isinstance(value, bool), "a boolean"
    else:
        ok, want = isinstance(value, str), "a string"
    if not ok:
        raise ToolInputError(f"{param.name}: expected {want}")
    allowed = param.enum or tuple(param.choices)
    if allowed and value not in allowed:
        raise ToolInputError(f"{param.name}: must be one of {', '.join(allowed)}")
    if param.pattern:
        if not re.fullmatch(param.pattern, value):  # type: ignore[arg-type]
            raise ToolInputError(f"{param.name}: {value!r} does not match {param.pattern}")
    if (isinstance(value, str) and value.startswith("-") and not param.path
            and not allow_dash):
        # A value that argparse would read as a flag of its own.
        raise ToolInputError(f"{param.name}: may not start with '-'")


def build_argv(tool: reg.Tool, arguments: dict, project: Path) -> list[str]:
    """Validate `arguments` and turn them into the script's argv (no prefix)."""
    if not isinstance(arguments, dict):
        raise ToolInputError("arguments must be an object")
    known = {p.name for p in tool.params}
    unknown = sorted(set(arguments) - known)
    if unknown:
        raise ToolInputError(f"unknown argument(s): {', '.join(unknown)}")

    def is_set(name: str) -> bool:
        v = arguments.get(name)
        return v not in (None, False, "", [])

    for group in tool.exclusive:
        if sum(is_set(n) for n in group) > 1:
            raise ToolInputError(f"{' and '.join(group)} cannot be combined")
    for group in tool.one_of:
        if not any(is_set(n) for n in group):
            raise ToolInputError(f"one of {', '.join(group)} is required")

    argv = [str(project) if t == reg.PROJECT else t for t in tool.fixed]
    for param in tool.params:
        value = arguments.get(param.name)
        if value is None:
            if param.required:
                raise ToolInputError(f"{param.name} is required")
            if param.default is None:
                continue
            value = str(project) if param.default == reg.PROJECT else param.default
        _check_type(param, value)
        if param.path and isinstance(value, str):
            value = confine(value, project, param.name)
        if param.kind == "option":
            if value != "":
                argv += [param.flag, value]
        elif param.kind == "switch":
            if value:
                argv.append(param.flag)
        elif param.kind == "repeat":
            for v in value:
                if v.startswith("-"):
                    raise ToolInputError(f"{param.name}: may not start with '-'")
                argv += [param.flag, v]
        elif param.kind == "positional":
            argv.append(value)
        elif param.kind == "map":
            argv += list(param.choices[value])
        else:  # pragma: no cover — registry bug
            raise RuntimeError(f"unknown parameter kind {param.kind!r}")
    return argv


# ---------------------------------------------------------------------------
# Running scripts
# ---------------------------------------------------------------------------

def _tail(text: str) -> str:
    lines = text.rstrip().splitlines()[-LOG_TAIL_LINES:]
    tail = "\n".join(lines)
    if len(tail) > LOG_TAIL_CHARS:
        tail = "…" + tail[-LOG_TAIL_CHARS:]
    return tail


def _save_log(project: Path, tool: str, argv: list[str], text: str) -> str | None:
    """Keep the run's full output where the agent can read it if needed."""
    path = project / LOG_DIR / f"{tool}.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("$ " + " ".join(argv) + "\n\n" + text, encoding="utf-8")
    except OSError:
        return None
    return (LOG_DIR / f"{tool}.log").as_posix()


def run_script(script: str, args: list[str], project: Path, timeout: int) -> dict:
    """Run one plugin script; return exit code, combined output, argv."""
    argv = [*launcher(), str(PLUGIN_ROOT / script), *args]
    env = dict(os.environ)
    # Scripts print arrows and em dashes; Windows pipes default to cp1252.
    env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        proc = subprocess.run(
            argv, cwd=project, env=env,
            stdin=subprocess.DEVNULL,  # a TTY prompt sees EOF, never hangs
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return {"exit_code": None, "output": out, "argv": argv, "timed_out": True}
    return {"exit_code": proc.returncode, "output": proc.stdout or "", "argv": argv,
            "timed_out": False}


def _result(tool: reg.Tool, run: dict, project: Path, summary: dict,
            artifacts: list[str]) -> dict:
    result = {
        "ok": run["exit_code"] == 0,
        "exit_code": run["exit_code"],
        "summary": summary,
        "artifacts": artifacts,
        "log_tail": _tail(run["output"]),
        "log_path": _save_log(project, tool.name, run["argv"], run["output"]),
    }
    if run["timed_out"]:
        result["timed_out"] = True
        result["summary"] = {
            **summary,
            "error": f"stopped after {tool.timeout} s; run it from a terminal instead",
        }
    return result


def call_check_setup(tool: reg.Tool, arguments: dict, project: Path) -> dict:
    build_argv(tool, arguments, project)  # rejects any argument
    first = run_script(tool.script, [], project, tool.timeout)
    configured = "NOT CONFIGURED" not in first["output"] and first["exit_code"] == 0
    summary: dict = {"configured": configured, "project_dir": str(project)}
    run = first
    if configured:
        second = run_script(tool.extra_scripts[0], [], project, tool.timeout)
        databases = {}
        for line in second["output"].splitlines():
            name, sep, status = line.partition(": ")
            if sep and name.strip().isidentifier():
                databases[name.strip()] = status.strip()
        summary["databases"] = databases
        # The version of the Zotero MCP server the host launches, read from
        # the MCP config rather than this interpreter (BACKLOG P21).
        third = run_script(tool.extra_scripts[1], [], project, tool.timeout)
        servers = [line.partition(": ")[2] for line in third["output"].splitlines()
                   if line.startswith("zotero-mcp-server: ")]
        warnings = [line for line in third["output"].splitlines()
                    if line.startswith("WARNING")]
        summary["zotero_mcp_server"] = servers
        if warnings:
            summary["zotero_mcp_warnings"] = warnings
        run = {**second, "output": first["output"] + second["output"]
               + third["output"]}
    else:
        summary["next_step"] = (
            "Run the setup wizard in a terminal; API keys typed into chat "
            "reach the model provider and the transcript."
        )
    return _result(tool, run, project, summary, [])


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
#
# A job is a directory, `.claude/jobs/<job_id>/`, and nothing else:
#
#   job.json              tool, argv, started_at, pid, runner_pid, status,
#                         exit_code, finished_at (the runner keeps it current)
#   run.log               the script's stdout + stderr
#   progress.jsonl        the script's --progress-json events
#   control.json(.reply)  the ControlFileChannel handshake, for scripts
#                         that can ask the user something
#   cancel.json           written by cancel_job; the runner acts on it
#
# The server keeps no job state of its own, so every job tool works the
# same from a fresh server process — after a host restart, say — as from
# the one that started the job.

JOBS_DIR = Path(".claude") / "jobs"
#: Test hook: seconds the runner waits between cancel escalation steps.
GRACE_ENV = "ACADEMIC_RESEARCH_MCP_JOB_GRACE_S"
CANCEL_GRACE_S = 20.0
FINAL_STATUSES = ("succeeded", "failed", "cancelled")
LIST_JOBS_LIMIT = 20
#: How long a job may show no runner pid before it counts as never started.
RUNNER_START_WINDOW_S = 60
#: Progress events kept verbatim in a status reply.
PROGRESS_LAST_EVENTS = 3
PROGRESS_MAX_CHARS = 3000

#: Runner processes this server started. Polled so they are reaped when
#: they exit — a zombie would otherwise still look alive to `_pid_alive`.
_spawned: list[subprocess.Popen] = []


def _reap() -> None:
    for proc in list(_spawned):
        if proc.poll() is not None:
            _spawned.remove(proc)


def _pid_alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32":
        # os.kill(pid, 0) on Windows *terminates* the process; ask instead.
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _new_job_id(tool: reg.Tool) -> str:
    short = tool.name.removeprefix("start_")
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{short}-{stamp}-{secrets.token_hex(2)}"


def _job_dir(project: Path, job_id: str) -> Path:
    # job_id has already matched JOB_ID's pattern: no separators, no dots.
    job_dir = project / JOBS_DIR / job_id
    if not (job_dir / job_runner.JOB_FILE).is_file():
        raise ToolInputError(f"no job {job_id!r} in this project (see list_jobs)")
    return job_dir


def _rel(path: Path, project: Path) -> str:
    try:
        return path.relative_to(project).as_posix()
    except ValueError:
        return str(path)


def _spawn_detached(argv: list[str], cwd: Path) -> subprocess.Popen:
    """Start `argv` so it outlives this server and its host."""
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(
        argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True, **kwargs,
    )


def _iter_jobs(project: Path):
    root = project / JOBS_DIR
    if not root.is_dir():
        return
    for job_dir in root.iterdir():
        job = job_runner.read_json(job_dir / job_runner.JOB_FILE)
        if job:
            yield job_dir, job


def _job_state(job_dir: Path, job: dict) -> tuple[str, str]:
    """(status, note) from what is on disk and whether the runner lives."""
    status = job.get("status")
    if status in FINAL_STATUSES:
        return status, ""
    runner = job.get("runner_pid")
    if runner is None:
        try:
            started = datetime.fromisoformat(job.get("started_at", ""))
            age = (datetime.now(UTC) - started).total_seconds()
        except (TypeError, ValueError):
            age = RUNNER_START_WINDOW_S + 1
        if age > RUNNER_START_WINDOW_S:
            return "failed", "the job runner never started"
    elif not _pid_alive(runner):
        # It may have recorded the result between our two reads.
        job = job_runner.read_json(job_dir / job_runner.JOB_FILE)
        if job.get("status") in FINAL_STATUSES:
            return job["status"], ""
        return "failed", ("the job runner stopped without recording an exit "
                          "status (machine restart or killed); see run.log")
    control = job_runner.read_json(job_dir / "control.json")
    if control.get("state") == "awaiting_user":
        return "awaiting_user", ""
    return "running", ""


def _strip(event: dict) -> dict:
    return {k: v for k, v in event.items() if k != "ts"}


def summarise_progress(path: Path) -> dict:
    """The last few events and running counts — never the whole file."""
    events = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue  # a line still being written
        if isinstance(event, dict) and isinstance(event.get("event"), str):
            events.append(event)
    if not events:
        return {}
    out: dict = {"events": len(events)}
    starts = [e for e in events if e["event"] == "start"]
    if starts:
        out["start"] = _strip(starts[-1])
    items = [e for e in events if e["event"] == "item"]
    if items:
        last = items[-1]
        for key in ("phase", "done", "queued"):
            if key in last:
                out[key] = last[key]
        outcomes: dict[str, int] = {}
        for e in items:
            o = str(e.get("outcome", "?"))
            outcomes[o] = outcomes.get(o, 0) + 1
        out["outcomes"] = outcomes
    summaries = [e for e in events if e["event"] == "summary"]
    if summaries:
        out["summary"] = _strip(summaries[-1])
    done = [e for e in events if e["event"] == "done"]
    if done:
        out["finished"] = _strip(done[-1])
    out["last_events"] = [_strip(e) for e in events[-PROGRESS_LAST_EVENTS:]]
    ts = events[-1].get("ts")
    if isinstance(ts, (int, float)):
        out["updated_at"] = datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")
    if len(json.dumps(out, default=str)) > PROGRESS_MAX_CHARS:
        out.pop("last_events", None)
        if len(json.dumps(out, default=str)) > PROGRESS_MAX_CHARS:
            out["summary"] = {"keys": sorted(out.get("summary", {}))}
    return out


def _artifacts(tool: reg.Tool | None, job: dict, project: Path) -> list[str]:
    if tool is None:
        return []
    try:
        started = datetime.fromisoformat(job["started_at"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return []
    found = []
    for rel in tool.artifacts:
        path = project / rel
        try:
            if path.is_file() and path.stat().st_mtime >= started - 1:
                found.append(rel)
        except OSError:
            continue
    return found


def _live_jobs(project: Path, holds) -> list[str]:
    """Running jobs whose tool and arguments satisfy `holds`."""
    found = []
    for job_dir, job in _iter_jobs(project):
        tool = reg.TOOLS_BY_NAME.get(job.get("tool", ""))
        if (tool is not None and holds(tool, job.get("arguments") or {})
                and _job_state(job_dir, job)[0] in ("running", "awaiting_user")):
            found.append(job_dir.name)
    return found


def _live_zotero_writers(project: Path) -> list[str]:
    return _live_jobs(project, _writes_zotero)


def _uses_browser(tool: reg.Tool, arguments: dict) -> bool:
    # A dry run still opens Chromium, so it is not exempt here.
    return tool.browser


def _live_browser_jobs(project: Path) -> list[str]:
    return _live_jobs(project, _uses_browser)


def _writes_zotero(tool: reg.Tool, arguments: dict) -> bool:
    return (tool.effect in (reg.WRITES_ZOTERO, reg.COSTS_LLM, reg.COSTS_MONEY)
            and not arguments.get("dry_run"))


def start_job(tool: reg.Tool, arguments: dict, project: Path) -> dict:
    args = build_argv(tool, arguments, project)
    _reap()
    if _writes_zotero(tool, arguments):
        # One Zotero library, one writer: concurrent writers interleave
        # badly and the 412 retry masks rather than resolves it.
        busy = _live_zotero_writers(project)
        if busy:
            raise ToolInputError(
                f"job {busy[0]} is still writing to Zotero; wait for it "
                "(job_status) or stop it (cancel_job) before starting another"
            )
    if _uses_browser(tool, arguments):
        # One Playwright Chromium profile per project (under
        # output/pdf_cache/): a second browser job would open the same
        # profile directory, which Chromium refuses or corrupts, and the
        # user can attend only one challenge window anyway.
        busy = _live_browser_jobs(project)
        if busy:
            raise ToolInputError(
                f"job {busy[0]} is using the plugin's browser (one Playwright "
                "profile, one browser job at a time); wait for it "
                "(job_status) or stop it (cancel_job) before starting another"
            )
    job_id = _new_job_id(tool)
    job_dir = project / JOBS_DIR / job_id
    job_dir.mkdir(parents=True)
    script_argv = [*launcher(), str(PLUGIN_ROOT / tool.script), *args,
                   "--progress-json", str(job_dir / "progress.jsonl")]
    if tool.control:
        script_argv += ["--control-file", str(job_dir / "control.json")]
    try:
        grace = float(os.environ.get(GRACE_ENV, "") or CANCEL_GRACE_S)
    except ValueError:
        grace = CANCEL_GRACE_S
    job = {
        "job_id": job_id, "tool": tool.name, "arguments": arguments,
        "argv": script_argv, "started_at": job_runner.now_iso(),
        "status": "running", "pid": None, "runner_pid": None,
        "exit_code": None, "finished_at": None, "grace_s": grace,
    }
    job_runner.write_json(job_dir / job_runner.JOB_FILE, job)
    runner = [sys.executable, str(_HERE / "job_runner.py"), str(job_dir), "--",
              *script_argv]
    try:
        _spawned.append(_spawn_detached(runner, project))
    except OSError as e:
        job_runner.write_json(job_dir / job_runner.JOB_FILE, {
            **job, "status": "failed", "error": f"could not start the runner: {e}",
            "finished_at": job_runner.now_iso()})
        raise
    return {
        "ok": True, "job_id": job_id, "status": "running",
        "job_dir": _rel(job_dir, project),
        "next": "Call job_status with this job_id to follow it.",
    }


def call_job_status(arguments: dict, project: Path) -> dict:
    _reap()
    job_dir = _job_dir(project, arguments["job_id"])
    job = job_runner.read_json(job_dir / job_runner.JOB_FILE)
    status, note = _job_state(job_dir, job)
    tool = reg.TOOLS_BY_NAME.get(job.get("tool", ""))
    try:
        log = (job_dir / job_runner.LOG_FILE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        log = ""
    result: dict = {
        "ok": True, "job_id": job_dir.name, "tool": job.get("tool"),
        "status": status, "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"), "exit_code": job.get("exit_code"),
        "progress": summarise_progress(job_dir / "progress.jsonl"),
        "log_tail": _tail(log),
        "log_path": _rel(job_dir / job_runner.LOG_FILE, project),
        "artifacts": _artifacts(tool, job, project),
    }
    if note or job.get("error"):
        result["error"] = note or job.get("error")
    if (job_dir / job_runner.CANCEL_FILE).exists():
        result["cancel_requested"] = True
    if status == "awaiting_user":
        control = job_runner.read_json(job_dir / "control.json")
        result["prompt"] = control.get("prompt", "")
        result["prompt_kind"] = control.get("kind", "line")
        result["next"] = ("Show the prompt to the user and pass their answer "
                          "to job_answer; do not answer for them.")
    return result


def call_job_answer(arguments: dict, project: Path) -> dict:
    job_dir = _job_dir(project, arguments["job_id"])
    job = job_runner.read_json(job_dir / job_runner.JOB_FILE)
    status, _ = _job_state(job_dir, job)
    control_path = job_dir / "control.json"
    control = job_runner.read_json(control_path)
    if status != "awaiting_user":
        raise ToolInputError(f"job {job_dir.name} is not waiting for an answer "
                             f"(status: {status})")
    seq = control.get("seq")
    job_runner.write_json(control_path.with_name(control_path.name + ".reply"),
                          {"seq": seq, "answer": arguments["answer"]})
    return {"ok": True, "job_id": job_dir.name, "seq": seq, "delivered": True,
            "next": "Call job_status to follow the job."}


def call_cancel_job(arguments: dict, project: Path) -> dict:
    job_dir = _job_dir(project, arguments["job_id"])
    job = job_runner.read_json(job_dir / job_runner.JOB_FILE)
    status, _ = _job_state(job_dir, job)
    if status in FINAL_STATUSES:
        return {"ok": True, "job_id": job_dir.name, "status": status,
                "note": "the job had already finished"}
    job_runner.write_json(job_dir / job_runner.CANCEL_FILE,
                          {"requested_at": job_runner.now_iso()})
    if (sys.platform != "win32" and not _pid_alive(job.get("runner_pid"))
            and _pid_alive(job.get("pid"))):
        # No runner left to act on cancel.json: interrupt the group directly.
        try:
            os.killpg(job["pid"], signal.SIGINT)
        except OSError:
            pass
    return {"ok": True, "job_id": job_dir.name, "status": status,
            "cancel_requested": True,
            "next": ("The job is interrupted so it can save its progress, then "
                     "terminated if it does not stop; job_status shows when "
                     "it has ended.")}


def call_list_jobs(arguments: dict, project: Path) -> dict:
    _reap()
    jobs = []
    for job_dir, job in _iter_jobs(project):
        jobs.append({
            "job_id": job_dir.name, "tool": job.get("tool"),
            "status": _job_state(job_dir, job)[0],
            "started_at": job.get("started_at"),
            "finished_at": job.get("finished_at"),
        })
    jobs.sort(key=lambda j: j.get("started_at") or "", reverse=True)
    return {"ok": True, "jobs": jobs[:LIST_JOBS_LIMIT], "total": len(jobs)}


INTERNAL_HANDLERS = {
    "job_status": call_job_status,
    "job_answer": call_job_answer,
    "cancel_job": call_cancel_job,
    "list_jobs": call_list_jobs,
}


def check_internal_arguments(tool: reg.Tool, arguments: dict) -> dict:
    """Validate a server-side tool's arguments the way build_argv does."""
    if not isinstance(arguments, dict):
        raise ToolInputError("arguments must be an object")
    unknown = sorted(set(arguments) - {p.name for p in tool.params})
    if unknown:
        raise ToolInputError(f"unknown argument(s): {', '.join(unknown)}")
    for param in tool.params:
        value = arguments.get(param.name)
        if value is None:
            if param.required:
                raise ToolInputError(f"{param.name} is required")
            continue
        _check_type(param, value, allow_dash=True)
    return arguments


def call_tool(name: str, arguments: dict | None) -> dict:
    """Run tool `name`; return the summary dict that becomes the result."""
    tool = reg.TOOLS_BY_NAME[name]
    project = project_dir()
    arguments = arguments or {}
    if tool.internal:
        return INTERNAL_HANDLERS[tool.name](
            check_internal_arguments(tool, arguments), project)
    if tool.job:
        return start_job(tool, arguments, project)
    if tool is reg.CHECK_SETUP:
        return call_check_setup(tool, arguments, project)
    args = build_argv(tool, arguments, project)
    run = run_script(tool.script, args, project, tool.timeout)
    summary, artifacts = ({}, [])
    if tool.summarise and not run["timed_out"]:
        try:
            summary, artifacts = tool.summarise(run["output"], run["exit_code"], project)
        except Exception as e:  # noqa: BLE001 — a parser bug must not lose the run
            summary = {"summary_error": f"{type(e).__name__}: {e}"}
    return _result(tool, run, project, summary, artifacts)


# ---------------------------------------------------------------------------
# JSON-RPC
# ---------------------------------------------------------------------------

def _error(id_: object, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _ok(id_: object, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _initialize(params: dict) -> dict:
    requested = params.get("protocolVersion")
    version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": plugin_version()},
        "instructions": (
            "Tools that run the academic-research plugin's scripts in the "
            "current project. Each description starts with its effect class. "
            "Results are short JSON summaries; the full output of a tool's "
            "last run is at log_path. Long runs (start_*) are background "
            "jobs: they return a job_id at once; poll job_status, relay any "
            "question the job asks to the user and send their reply with "
            "job_answer, stop one with cancel_job. Jobs keep running if "
            "this server restarts. Every tool has a terminal equivalent "
            "(the script it runs), which stays available."
        ),
    }


def _tools_call(params: dict) -> dict:
    name = params.get("name")
    if name not in reg.TOOLS_BY_NAME:
        raise LookupError(f"unknown tool: {name!r}")
    try:
        result = call_tool(name, params.get("arguments"))
    except (ValueError, OSError, RuntimeError) as e:
        # Reported as a tool result, not a protocol error, so the model
        # sees why and can correct the call.
        payload = {"ok": False, "exit_code": None, "summary": {"error": str(e)},
                   "artifacts": [], "log_tail": ""}
        return {"content": [{"type": "text", "text": json.dumps(payload)}],
                "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            "isError": not result["ok"]}


def handle(message: object) -> dict | None:
    """Answer one JSON-RPC message; None for a notification."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(None, INVALID_REQUEST, "not a JSON-RPC 2.0 message")
    method = message.get("method")
    is_request = "id" in message
    id_ = message.get("id")
    if not isinstance(method, str):
        # A response from the client (we send no requests) or garbage.
        return _error(id_, INVALID_REQUEST, "missing method") if is_request else None
    if not is_request:
        return None  # notifications/initialized, cancelled, …: nothing to say
    params = message.get("params") or {}
    if not isinstance(params, dict):
        return _error(id_, INVALID_PARAMS, "params must be an object")
    if method == "initialize":
        return _ok(id_, _initialize(params))
    if method == "ping":
        return _ok(id_, {})
    if method == "tools/list":
        return _ok(id_, {"tools": [t.to_mcp() for t in reg.TOOLS]})
    if method == "tools/call":
        try:
            return _ok(id_, _tools_call(params))
        except LookupError as e:
            return _error(id_, INVALID_PARAMS, str(e))
    return _error(id_, METHOD_NOT_FOUND, f"method not found: {method}")


def _safe_handle(message: object) -> dict | None:
    try:
        return handle(message)
    except Exception as e:  # noqa: BLE001 — one bad call must not end the session
        id_ = message.get("id") if isinstance(message, dict) else None
        return _error(id_, INTERNAL_ERROR, f"{type(e).__name__}: {e}")


def _dispatch(message: object) -> object:
    """One message, or a JSON-RPC batch (allowed by the 2025-03-26 spec)."""
    if isinstance(message, list):
        if not message:
            return _error(None, INVALID_REQUEST, "empty batch")
        replies = [r for r in (_safe_handle(m) for m in message) if r is not None]
        return replies or None
    return _safe_handle(message)


def serve(stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            reply: object = _error(None, PARSE_ERROR, "invalid JSON")
        else:
            reply = _dispatch(message)
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()


def main() -> int:
    # stdout carries the protocol and nothing else; make it UTF-8 with
    # "\n" line ends on every OS.
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", newline="\n")
    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
