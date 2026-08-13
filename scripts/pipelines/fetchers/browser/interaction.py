"""How the browser pass asks the user a question.

The browser cascade is interactive by design: a human has to solve a
Cloudflare challenge or an SSO login in a real window, and only a human
can say whether the PDF is actually reachable from the page in front of
them. That part is not automatable and this module does not try.

What *was* accidentally coupled to it is the **transport**. Both prompt
helpers read `/dev/tty` directly, so "can we ask the user" collapsed
into "is there a controlling terminal". An agent driving the pipeline
from a Bash subprocess has no controlling terminal, so
`_has_interactive_surface()` returned False and the run bailed out with
a command for the user to paste into their own terminal — even though
the user was right there in the conversation, able to answer, and the
Chromium window would have appeared on their screen either way.

So: keep the human, replace the wire. A channel answers two questions —
"press Enter when done" and "type an answer" — and there are three
implementations:

    TtyChannel          read /dev/tty (what a real terminal does)
    ControlFileChannel  write the prompt to a file, poll for a reply
    AutoSkipChannel     never ask; answer with the configured default

`ControlFileChannel` is what makes the pass agent-drivable. The script
writes `{"state": "awaiting_user", "prompt": …}` and waits; the agent
watching that file relays the question into the conversation, the user
answers there, and the agent writes the reply back. No TTY anywhere, and
the human is still the one deciding.

The channel is process-global rather than threaded through every handler
signature: `PublisherHandler.setup()` is a public extension point that
downstream handlers override, and adding a parameter to it would break
them all for a concern none of them should know about.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path

#: How often `ControlFileChannel` looks for a reply, and how long it
#: waits before giving up. The timeout is generous because the thing
#: being waited on is a human solving a Cloudflare challenge, possibly
#: after walking back to their desk.
POLL_INTERVAL_S = 1.0
DEFAULT_TIMEOUT_S = 1800.0


class InteractionChannel(ABC):
    """Somewhere to put a question and get an answer back."""

    #: False when this channel cannot actually reach a human. The
    #: orchestrator's pre-flight uses it to decide whether to run at all.
    interactive: bool = True

    @abstractmethod
    def wait_for_user(self, prompt: str) -> None:
        """Block until the user acknowledges (the Enter keystroke)."""

    @abstractmethod
    def read_line(self, prompt: str) -> str:
        """Block until the user types an answer; return it stripped."""

    def progress(self, event: dict) -> None:  # noqa: B027 — optional hook
        """Report structured progress. No-op unless the channel wants it.

        Deliberately concrete-and-empty rather than abstract: only
        `ControlFileChannel` has anywhere to put this, and forcing the
        other two to implement a no-op would be ceremony. Exists so a
        driving agent can follow a run without scraping stdout, which is
        formatted for a person and changes freely.
        """


class TtyChannel(InteractionChannel):
    """Read the controlling terminal — the original behaviour.

    `/dev/tty` first so a piped stdin cannot auto-consume the prompt,
    falling back to stdin where there is no `/dev/tty` (Windows).
    """

    def wait_for_user(self, prompt: str) -> None:
        sys.stdout.write(prompt)
        sys.stdout.flush()
        try:
            with open("/dev/tty") as tty:
                tty.readline()
        except Exception:
            sys.stdin.readline()

    def read_line(self, prompt: str) -> str:
        sys.stdout.write(prompt)
        sys.stdout.flush()
        try:
            with open("/dev/tty") as tty:
                return tty.readline().strip()
        except Exception:
            return sys.stdin.readline().strip()


class AutoSkipChannel(InteractionChannel):
    """Never ask; return a fixed answer. Backs `--no-prompt`.

    `interactive = False` because nothing here reaches a human — an
    unattended run that hits a challenge it cannot solve should skip the
    publisher and say so, not pretend a user approved it.
    """

    interactive = False

    def __init__(self, answer: str = "skip") -> None:
        self.answer = answer

    def wait_for_user(self, prompt: str) -> None:
        print(f"{prompt}\n[--no-prompt: not waiting]", flush=True)

    def read_line(self, prompt: str) -> str:
        print(f"{prompt}\n[--no-prompt: answering {self.answer!r}]", flush=True)
        return self.answer


class ControlFileTimeout(RuntimeError):
    """No reply arrived before the timeout elapsed."""


class ControlFileChannel(InteractionChannel):
    """Ask through a file, so the questioner needs no terminal.

    Protocol, deliberately small enough to drive by hand:

      1. The script writes the control file:

             {"state": "awaiting_user", "kind": "line" | "ack",
              "prompt": "...", "asked_at": 1234567890.0, "seq": 3}

      2. The agent reads it, relays `prompt` into the conversation, and
         writes the user's answer to `<control-file>.reply`:

             {"seq": 3, "answer": "y"}

      3. The script consumes the reply, deletes it, and rewrites the
         control file as `{"state": "running"}`.

    `seq` is what makes this safe to poll: a stale reply left over from
    an earlier question has an older `seq` and is ignored rather than
    silently answering the next one. Writes are atomic (temp file plus
    `os.replace`) so a reader never sees half a JSON document.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        poll_interval_s: float = POLL_INTERVAL_S,
    ) -> None:
        self.path = Path(path)
        self.reply_path = self.path.with_suffix(self.path.suffix + ".reply")
        self.timeout_s = timeout_s
        self.poll_interval_s = poll_interval_s
        self._seq = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write({"state": "running", "seq": 0})

    # -- file plumbing -------------------------------------------------

    def _write(self, payload: dict) -> None:
        """Atomic write, so a polling reader never sees a partial file."""
        fd, tmp = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
                fh.write("\n")
            os.replace(tmp, self.path)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _read_reply(self) -> str | None:
        """The answer to the current question, or None if not there yet."""
        if not self.reply_path.is_file():
            return None
        try:
            data = json.loads(self.reply_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None            # mid-write; try again next tick
        if not isinstance(data, dict):
            return None
        # A reply for an earlier question is stale. Answering the current
        # one with it would look like the user replying instantly — the
        # exact bug that would make a half-automated handshake untrustworthy.
        if int(data.get("seq", -1)) != self._seq:
            return None
        self.reply_path.unlink(missing_ok=True)
        return str(data.get("answer", "")).strip()

    def _ask(self, prompt: str, kind: str) -> str:
        self._seq += 1
        self._write({
            "state": "awaiting_user",
            "kind": kind,
            "prompt": prompt,
            "asked_at": time.time(),
            "seq": self._seq,
        })
        print(prompt, flush=True)
        print(
            f"[waiting for a reply in {self.reply_path} (seq={self._seq})]",
            flush=True,
        )

        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            answer = self._read_reply()
            if answer is not None:
                self._write({"state": "running", "seq": self._seq})
                return answer
            time.sleep(self.poll_interval_s)

        self._write({
            "state": "timeout", "seq": self._seq, "prompt": prompt,
        })
        raise ControlFileTimeout(
            f"No reply to the prompt after {self.timeout_s:.0f}s. Write "
            f'{{"seq": {self._seq}, "answer": "..."}} to {self.reply_path} '
            f"to answer it."
        )

    # -- InteractionChannel -------------------------------------------

    def wait_for_user(self, prompt: str) -> None:
        self._ask(prompt, kind="ack")

    def read_line(self, prompt: str) -> str:
        return self._ask(prompt, kind="line")

    def progress(self, event: dict) -> None:
        self._write({"state": "running", "seq": self._seq, "progress": event})


class JsonlProgressFile:
    """Append-only JSONL sink — the second place progress events go.

    `ControlFileChannel.progress` already publishes these, but only for a
    run being driven through a control file, and only ever the latest
    one: the control file is a state document, so each event overwrites
    the last. A run started in the background without prompts has nothing
    to read but stdout, which is formatted for a person and reflows
    freely.

    So the same events also go here, one JSON object per line, and they
    accumulate. Following a run becomes a `tail`, and the history of a
    finished run survives to be read afterwards.

    The file is truncated when the sink is created: one file belongs to
    one run, and appending across runs would make "how far along is
    this?" unanswerable.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")

    def __call__(self, event: dict) -> None:
        record = {"ts": time.time(), **event}
        with self.path.open("a", encoding="utf-8") as fh:
            # One write per line so a reader polling mid-run sees whole
            # records; a partial final line is the reader's cue to wait.
            fh.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# Process-global current channel and progress sinks
# ---------------------------------------------------------------------------
#
# Same reasoning as the channel: `PublisherHandler.setup()` and the
# handler `download()` signatures are public extension points, and
# threading a reporter through them would make every downstream handler
# carry a concern none of them should know about.

_channel: InteractionChannel = TtyChannel()
_progress_sinks: list[Callable[[dict], None]] = []


def get_channel() -> InteractionChannel:
    return _channel


def set_channel(channel: InteractionChannel) -> InteractionChannel:
    """Install `channel`; return the one it replaced (for restoring)."""
    global _channel
    previous = _channel
    _channel = channel
    return previous


def add_progress_sink(sink: Callable[[dict], None]) -> None:
    """Send progress events to `sink` as well as to the channel."""
    _progress_sinks.append(sink)


def reset_progress_sinks() -> None:
    """Drop every extra sink. For tests, and for a second run in-process."""
    _progress_sinks.clear()


def report_progress(event: dict) -> None:
    """Publish one progress event to the channel and every extra sink.

    The single call site the pipeline uses. Failures are swallowed per
    sink: progress reporting is diagnostics, and a full disk or a
    read-only path must not take down a browser pass a human is sitting
    in front of.
    """
    for emit in (_channel.progress, *_progress_sinks):
        try:
            emit(event)
        except Exception:  # noqa: BLE001 — diagnostics must never sink the run
            pass
