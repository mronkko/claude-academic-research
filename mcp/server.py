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
import shutil
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

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

def _check_type(param: reg.Param, value: object) -> None:
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
    if isinstance(value, str) and value.startswith("-") and not param.path:
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


def call_tool(name: str, arguments: dict | None) -> dict:
    """Run tool `name`; return the summary dict that becomes the result."""
    tool = reg.TOOLS_BY_NAME[name]
    project = project_dir()
    arguments = arguments or {}
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
            "last run is at log_path. Every tool has a terminal equivalent "
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
