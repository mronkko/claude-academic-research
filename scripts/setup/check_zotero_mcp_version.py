#!/usr/bin/env python3
"""Report the version of the zotero-mcp-server the MCP configuration launches.

Prints one `zotero-mcp-server: <version> — <where>` line per MCP host that
registers a Zotero server, plus a WARNING line for each one below this
plugin's floor. The wizard installs zotero-mcp-server unpinned, so a stale
install can miss tool names the skills document, or the batched
`zotero_add_item` they rely on — this is the check that surfaces that before
a skill hits it mid-task.

**Why it reads the MCP configuration instead of importing `zotero_mcp`.**
The version that matters is the one in the server process Claude Code (or
Antigravity) starts, and that is whatever install the configured `command`
points at — typically `~/.local/bin/zotero-mcp`, a symlink into a uv tool
venv. Importing `zotero_mcp` here instead reported the copy in whichever
interpreter ran this script: in the plugin's own dev `.venv` that was 0.9.1
while the server Claude Code launched was 0.14.1.

Resolution, per configured server:

1. Find the entry. Claude Code: `projects[<project dir>].mcpServers` in
   `~/.claude.json` (local scope), then the project's `.mcp.json`, then
   `mcpServers` in `~/.claude.json` (user scope) — the first that has one
   wins, matching Claude Code's own precedence. Antigravity:
   `~/.gemini/config/mcp_config.json`, reported separately when present.
   Only `command` and `args` are read; `env` blocks can hold API keys and
   are never touched or printed, and neither are `args`.
2. Find the executable (`command` as a path, else on PATH).
3. Read the version without running anything: the
   `zotero_mcp_server-*.dist-info/METADATA` in the venv the executable
   lives in, or in the venv of the interpreter named in its shebang.
4. Otherwise ask that interpreter (`importlib.metadata`), then the
   executable itself (`zotero-mcp version`).

A server started through `uvx` / `uv tool run` has no fixed install to
inspect, and an entry that cannot be followed is reported as unresolved
with the reason. Exit status is always 0: this is a report, not a gate.

Stdlib only — `scripts/setup/` runs as a bare `python3` subprocess. The
floor and the install commands live in `zotero_mcp_floor.py`, shared with
`wizard.py`.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Running this file as a script already puts its directory on sys.path[0],
# but not when it is imported by path from a test. Be explicit.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from zotero_mcp_floor import (  # noqa: E402
    FLOOR_STR,
    PACKAGE,
    PIP_INSTALL_CMD,
    UV_INSTALL_CMD,
    is_below_floor,
)

SERVER_NAME = "zotero"
DIST_PREFIX = PACKAGE.replace("-", "_")  # zotero_mcp_server
SUBPROCESS_TIMEOUT = 20
_VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)?[0-9A-Za-z.+-]*)")


@dataclass
class ServerEntry:
    host: str          # "Claude Code" / "Antigravity"
    source: str        # human-readable config location, home shown as ~
    command: str
    args: list[str]


@dataclass
class Resolution:
    version: str | None
    executable: Path | None = None
    how: str = ""      # how the version was found, or why it was not


def _tilde(path: Path) -> str:
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _pick_server(servers: object) -> tuple[str, dict] | None:
    """The Zotero server entry: the key `zotero`, else any key whose
    command is a `zotero-mcp` executable. Returns (key, {command, args})."""
    if not isinstance(servers, dict):
        return None

    def fields(entry: object) -> dict | None:
        if not isinstance(entry, dict) or not isinstance(entry.get("command"), str):
            return None
        args = entry.get("args")
        args = [a for a in args if isinstance(a, str)] if isinstance(args, list) else []
        return {"command": entry["command"], "args": args}

    if SERVER_NAME in servers and (f := fields(servers[SERVER_NAME])):
        return SERVER_NAME, f
    for key, entry in servers.items():
        f = fields(entry)
        if f and Path(f["command"]).name.lower().startswith("zotero-mcp"):
            return str(key), f
    return None


def project_dir() -> Path:
    env = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    if env and Path(env).is_dir():
        return Path(env).resolve()
    return Path.cwd().resolve()


def claude_code_entry(project: Path) -> ServerEntry | None:
    """The Zotero server Claude Code would launch in `project`."""
    claude_json = Path.home() / ".claude.json"
    data = _load_json(claude_json)
    projects = data.get("projects") if isinstance(data.get("projects"), dict) else {}
    for d in (project, *project.parents):
        proj = projects.get(str(d)) or projects.get(d.as_posix())
        if isinstance(proj, dict) and (hit := _pick_server(proj.get("mcpServers"))):
            key, f = hit
            return ServerEntry("Claude Code", f"local scope, {_tilde(claude_json)} "
                               f"projects[{d}].mcpServers.{key}", **f)
    mcp_json = project / ".mcp.json"
    hit = _pick_server(_load_json(mcp_json).get("mcpServers"))
    if hit:
        key, f = hit
        return ServerEntry("Claude Code", f"project scope, {mcp_json} mcpServers.{key}", **f)
    hit = _pick_server(data.get("mcpServers"))
    if hit:
        key, f = hit
        return ServerEntry("Claude Code", f"user scope, {_tilde(claude_json)} "
                           f"mcpServers.{key}", **f)
    return None


def antigravity_entry() -> ServerEntry | None:
    path = Path.home() / ".gemini" / "config" / "mcp_config.json"
    hit = _pick_server(_load_json(path).get("mcpServers"))
    if not hit:
        return None
    key, f = hit
    return ServerEntry("Antigravity", f"{_tilde(path)} mcpServers.{key}", **f)


# ---------------------------------------------------------------------------
# Following a command to its install
# ---------------------------------------------------------------------------

def _find_executable(command: str) -> Path | None:
    cmd = os.path.expanduser(os.path.expandvars(command))
    if os.sep in cmd or (os.altsep and os.altsep in cmd):
        p = Path(cmd)
        return p if p.is_file() else None
    found = shutil.which(cmd)
    return Path(found) if found else None


def _metadata_version(venv: Path) -> str | None:
    """Version from `<venv>/lib/python*/site-packages/<dist>.dist-info`
    (or `Lib/site-packages` on Windows)."""
    patterns = (f"lib/python*/site-packages/{DIST_PREFIX}-*.dist-info/METADATA",
                f"Lib/site-packages/{DIST_PREFIX}-*.dist-info/METADATA")
    for pattern in patterns:
        for meta in sorted(venv.glob(pattern)):
            try:
                with meta.open(encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if line.startswith("Version:"):
                            return line.split(":", 1)[1].strip()
                        if not line.strip():
                            break  # end of the header block
            except OSError:
                continue
    return None


def _venv_of(executable: Path) -> Path | None:
    """`<venv>` for `<venv>/bin/x` or `<venv>/Scripts/x.exe`."""
    if executable.parent.name in ("bin", "Scripts"):
        return executable.parent.parent
    return None


def _shebang_interpreter(executable: Path) -> Path | None:
    try:
        with executable.open("rb") as fh:
            first = fh.readline(512)
    except OSError:
        return None
    if not first.startswith(b"#!"):
        return None
    parts = first[2:].decode("utf-8", "replace").strip().split()
    if not parts:
        return None
    if Path(parts[0]).name == "env":
        rest = [p for p in parts[1:] if not p.startswith("-")]
        found = shutil.which(rest[0]) if rest else None
        return Path(found) if found else None
    return Path(parts[0])


def _run_for_version(argv: list[str]) -> str | None:
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=SUBPROCESS_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    m = _VERSION_RE.search(proc.stdout or "")
    return m.group(1) if m else None


def _interpreter_version(interpreter: Path) -> str | None:
    code = ("import importlib.metadata as m\n"
            f"print(m.version({PACKAGE!r}))")
    return _run_for_version([str(interpreter), "-c", code])


def resolve(entry: ServerEntry) -> Resolution:
    name = Path(entry.command).name.lower()
    stem = name[:-4] if name.endswith(".exe") else name
    if stem in ("uvx", "uv", "pipx"):
        return Resolution(None, how=f"launched through `{entry.command}`, which "
                          "picks the version at launch; there is no fixed install "
                          "to inspect")
    exe = _find_executable(entry.command)
    if exe is None:
        return Resolution(None, how=f"command `{entry.command}` not found "
                          "(not a file, and not on PATH)")

    # `python -m zotero_mcp ...`: the command is the interpreter itself.
    if stem.startswith("python") or stem == "py":
        version = _metadata_version(_venv_of(exe.resolve()) or exe.parent) \
            or _interpreter_version(exe)
        return Resolution(version, exe, "the interpreter's installed package"
                          if version else "the interpreter has no "
                          f"{PACKAGE} installed")

    for candidate in dict.fromkeys(filter(None, (_venv_of(exe.resolve()), _venv_of(exe)))):
        if version := _metadata_version(candidate):
            return Resolution(version, exe, f"package metadata in {_tilde(candidate)}")

    interp = _shebang_interpreter(exe.resolve())
    if interp is not None:
        venv = _venv_of(interp)
        if venv and (version := _metadata_version(venv)):
            return Resolution(version, exe, f"package metadata in {_tilde(venv)}")
        if interp.is_file() and (version := _interpreter_version(interp)):
            return Resolution(version, exe, f"asked its interpreter {interp}")

    if version := _run_for_version([str(exe), "version"]):
        return Resolution(version, exe, f"`{exe.name} version`")
    return Resolution(None, exe, "could not read a version from its install "
                      f"or from `{exe.name} version`")


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _warning(version: str, host: str) -> str:
    return (
        f"WARNING: the zotero-mcp-server {host} launches is {version}, below "
        f"this plugin's floor ({FLOOR_STR}). Older installs miss tool names "
        "the skills document and add identifiers one at a time instead of "
        f"in batches. Upgrade: {UV_INSTALL_CMD} --force (or the PyPI alt: "
        f"{PIP_INSTALL_CMD} --upgrade), then restart {host}."
    )


def report(project: Path | None = None) -> list[str]:
    project = project or project_dir()
    entries = [e for e in (claude_code_entry(project), antigravity_entry()) if e]
    lines: list[str] = []
    warnings: list[str] = []
    if not entries:
        exe = _find_executable("zotero-mcp")
        where = ("no Zotero MCP server registered (checked ~/.claude.json, "
                 f"{project / '.mcp.json'}, ~/.gemini/config/mcp_config.json)")
        if exe is None:
            return [f"zotero-mcp-server: not installed — {where}"]
        res = resolve(ServerEntry("PATH", "PATH", str(exe), []))
        lines.append(f"zotero-mcp-server: {res.version or 'unknown'} — {where}; "
                     f"`zotero-mcp` on PATH is {exe}")
        if res.version and is_below_floor(res.version):
            warnings.append(_warning(res.version, "PATH"))
        return lines + warnings

    for entry in entries:
        res = resolve(entry)
        target = res.executable or entry.command
        if res.version:
            lines.append(f"zotero-mcp-server: {res.version} — {entry.host} "
                         f"({entry.source}) → {target} [{res.how}]")
            if is_below_floor(res.version):
                warnings.append(_warning(res.version, entry.host))
        else:
            lines.append(f"zotero-mcp-server: unknown — {entry.host} "
                         f"({entry.source}) → {target}: {res.how}. Run "
                         f"`{entry.command} version` in a terminal to check "
                         f"it is at least {FLOOR_STR}.")
    return lines + warnings


def main() -> int:
    for line in report():
        print(line)
    return 0


if __name__ == "__main__":
    # Windows takes stdout's encoding from the locale when output is
    # redirected — normally cp1252, which cannot encode the arrows and em
    # dashes printed below. Inline rather than via scripts/core/console.py
    # because this script must stand on its own.
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
