"""`--progress-json`: machine-readable progress for a run nobody is watching.

A pipeline script started as an MCP job (BACKLOG P21) runs detached, and
the agent following it has nothing to read but stdout, which is written
for a person and reflows freely. With `--progress-json PATH` the script
also appends one JSON object per line to PATH:

    {"ts": 1760000000.0, "event": "start",   "script": "...", "queued": 120}
    {"ts": ...,          "event": "item",    "key": "ABCD1234",
                         "outcome": "include", "done": 1, "queued": 120}
    {"ts": ...,          "event": "summary", "counts": {...}}
    {"ts": ...,          "event": "done",    "exit_code": 0, "ok": true}

The record shape is `enrich_pdfs`'s (`fetchers/browser/interaction.py`'s
`JsonlProgressFile`): a `ts`, an `event` name, and for per-item events
`done` / `queued` counters, so one reader handles every script. That
class is not imported here because importing it loads the whole fetcher
registry; this module is stdlib-only and safe for any script.

`done` is written by `reports_done`, which wraps `main()`, so it is the
last line on every exit path — a normal return, `sys.exit("ERROR: …")`,
Ctrl-C or a crash. A file without it means the process was killed.

The sink is process-global, like `interaction.report_progress`: threading
a reporter through every helper would make each of them carry a concern
none of them has. Without `--progress-json`, `emit` is a no-op and the
script's terminal behaviour is unchanged.
"""

from __future__ import annotations

import functools
import json
import threading
import time
from collections.abc import Callable
from pathlib import Path

FLAG = "--progress-json"

_path: Path | None = None
_lock = threading.Lock()


def add_argument(parser) -> None:
    """Add `--progress-json PATH` to an argparse parser."""
    parser.add_argument(
        "--progress-json", default="", metavar="PATH",
        help="Append one JSON object per line to PATH as the run "
             "progresses (start / item / summary / done events), so a "
             "caller can follow it without parsing stdout. The file is "
             "truncated when the run starts.",
    )


def configure(path: str | Path | None) -> None:
    """Start writing events to `path` (truncating it); empty disables."""
    global _path
    if not path:
        _path = None
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("", encoding="utf-8")
    _path = target


def active() -> bool:
    return _path is not None


def reset() -> None:
    """Stop writing. For tests and for a second in-process run."""
    global _path
    _path = None


def emit(event: str, **fields) -> None:
    """Append one event. Never raises: progress is diagnostics, and a full
    disk must not take down a paid screening run."""
    path = _path
    if path is None:
        return
    record = {"ts": time.time(), "event": event, **fields}
    try:
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        with _lock, path.open("a", encoding="utf-8") as fh:
            # One write per record so a reader polling mid-run sees whole
            # lines; a partial last line is its cue to wait.
            fh.write(line)
    except Exception:  # noqa: BLE001 — see docstring
        pass


def _exit_code(code: object) -> tuple[int, str]:
    """`SystemExit.code` as (exit code, message) the way Python reports it."""
    if code is None:
        return 0, ""
    if isinstance(code, int):
        return code, ""
    return 1, str(code)


def reports_done(main: Callable[[], int | None]) -> Callable[[], int | None]:
    """Decorate a script's `main()` so a `done` event closes every run."""

    @functools.wraps(main)
    def wrapper(*args, **kwargs):
        try:
            rc = main(*args, **kwargs)
        except SystemExit as e:
            code, message = _exit_code(e.code)
            fields = {"error": message[-1000:]} if message else {}
            emit("done", exit_code=code, ok=code == 0, **fields)
            reset()
            raise
        except KeyboardInterrupt:
            emit("done", exit_code=130, ok=False, interrupted=True)
            reset()
            raise
        except BaseException as e:
            emit("done", exit_code=1, ok=False,
                 error=f"{type(e).__name__}: {e}"[-1000:])
            reset()
            raise
        code, _ = _exit_code(rc)
        emit("done", exit_code=code, ok=code == 0)
        reset()
        return rc

    return wrapper
