"""The browser pass must be able to ask a human without a terminal.

Both prompt helpers used to read `/dev/tty` directly, so "can we ask the
user" collapsed into "is there a controlling terminal". An agent driving
the pipeline from a Bash subprocess has no controlling terminal, so the
run bailed out with a command to paste into a real one — even though the
user was present in the conversation and the Chromium window would have
appeared on their screen anyway.

These tests pin the separation: the human still answers every question,
but the question can travel through a file instead of a TTY.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from fetchers.browser import interaction


@pytest.fixture(autouse=True)
def _restore_channel():
    """Never let a test leak its channel into the next one."""
    previous = interaction.get_channel()
    yield
    interaction.set_channel(previous)


def _reply(path: Path, seq: int, answer: str) -> None:
    path.write_text(json.dumps({"seq": seq, "answer": answer}), encoding="utf-8")


# ---------------------------------------------------------------------------
# Channel selection
# ---------------------------------------------------------------------------


def test_the_default_channel_is_the_tty() -> None:
    """A run from a real terminal must behave exactly as before."""
    assert isinstance(interaction.get_channel(), interaction.TtyChannel)


def test_set_channel_returns_the_previous_one() -> None:
    original = interaction.get_channel()
    replaced = interaction.set_channel(interaction.AutoSkipChannel())
    assert replaced is original
    assert isinstance(interaction.get_channel(), interaction.AutoSkipChannel)


def test_auto_skip_is_not_interactive() -> None:
    """It reaches no human, so an unattended run must not read its answer
    as user approval."""
    assert interaction.AutoSkipChannel().interactive is False
    assert interaction.TtyChannel().interactive is True


def test_auto_skip_answers_without_blocking(capsys) -> None:
    channel = interaction.AutoSkipChannel()
    channel.wait_for_user("press enter")
    assert channel.read_line("y/n?") == "skip"
    assert "not waiting" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# The control file
# ---------------------------------------------------------------------------


def test_a_prompt_is_published_and_the_reply_is_consumed(tmp_path) -> None:
    control = tmp_path / "browser.json"
    channel = interaction.ControlFileChannel(
        control, timeout_s=10, poll_interval_s=0.01,
    )

    def answer_when_asked() -> None:
        for _ in range(500):
            data = json.loads(control.read_text(encoding="utf-8"))
            if data.get("state") == "awaiting_user":
                _reply(channel.reply_path, data["seq"], "y")
                return
            time.sleep(0.01)

    thread = threading.Thread(target=answer_when_asked)
    thread.start()
    answer = channel.read_line("Can you see the PDF?")
    thread.join()

    assert answer == "y"
    assert json.loads(control.read_text(encoding="utf-8"))["state"] == "running"
    assert not channel.reply_path.exists(), "the reply must be consumed"


def test_the_published_prompt_carries_the_question_and_kind(tmp_path) -> None:
    """The agent relays `prompt` verbatim into the conversation, so it
    has to be the real text the user would have seen on a terminal."""
    control = tmp_path / "browser.json"
    channel = interaction.ControlFileChannel(
        control, timeout_s=0.2, poll_interval_s=0.01,
    )
    with pytest.raises(interaction.ControlFileTimeout):
        channel.read_line("Solve the Cloudflare challenge, then answer y/n")

    data = json.loads(control.read_text(encoding="utf-8"))
    assert "Cloudflare challenge" in data["prompt"]
    assert data["state"] == "timeout"


def test_a_stale_reply_does_not_answer_the_next_question(tmp_path) -> None:
    """The guard that makes polling safe.

    A leftover reply from question 1 must not silently answer question 2
    — that would look like the user replying instantly and would let a
    run proceed past a challenge nobody solved.
    """
    control = tmp_path / "browser.json"
    channel = interaction.ControlFileChannel(
        control, timeout_s=0.2, poll_interval_s=0.01,
    )
    _reply(channel.reply_path, seq=1, answer="stale-answer")

    # This is seq=1, so the pre-written reply legitimately answers it.
    assert channel.read_line("first question") == "stale-answer"

    # Re-write the *same* seq and ask again: seq is now 2, so it is stale.
    _reply(channel.reply_path, seq=1, answer="stale-answer")
    with pytest.raises(interaction.ControlFileTimeout):
        channel.read_line("second question")


def test_a_malformed_reply_is_ignored_rather_than_crashing(tmp_path) -> None:
    """The agent writes this file; a half-written or bad JSON document
    must not take down a run mid-Cloudflare."""
    control = tmp_path / "browser.json"
    channel = interaction.ControlFileChannel(
        control, timeout_s=0.2, poll_interval_s=0.01,
    )
    channel.reply_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(interaction.ControlFileTimeout):
        channel.read_line("a question")


def test_the_timeout_message_says_how_to_answer(tmp_path) -> None:
    """Whoever hits this is the person who could have answered it."""
    control = tmp_path / "browser.json"
    channel = interaction.ControlFileChannel(
        control, timeout_s=0.1, poll_interval_s=0.01,
    )
    with pytest.raises(interaction.ControlFileTimeout) as excinfo:
        channel.wait_for_user("press enter when done")
    message = str(excinfo.value)
    assert "seq" in message
    assert str(channel.reply_path) in message


def test_the_control_file_is_written_atomically(tmp_path) -> None:
    """A polling reader must never see a partial JSON document."""
    control = tmp_path / "browser.json"
    channel = interaction.ControlFileChannel(
        control, timeout_s=0.1, poll_interval_s=0.01,
    )
    for _ in range(20):
        channel.progress({"publisher": "sage", "done": 3})
        json.loads(control.read_text(encoding="utf-8"))   # must always parse
    assert not list(tmp_path.glob(".*tmp")), "no temp files left behind"


def test_progress_is_reported_without_a_prompt(tmp_path) -> None:
    control = tmp_path / "browser.json"
    channel = interaction.ControlFileChannel(
        control, timeout_s=0.1, poll_interval_s=0.01,
    )
    channel.progress({"publisher": "sage", "attached": 12})

    data = json.loads(control.read_text(encoding="utf-8"))
    assert data["state"] == "running"
    assert data["progress"]["attached"] == 12


def test_creating_the_channel_makes_the_parent_directory(tmp_path) -> None:
    """`.claude/audit/` may not exist yet on a fresh project."""
    control = tmp_path / "nested" / "dir" / "browser.json"
    interaction.ControlFileChannel(control, timeout_s=0.1)
    assert control.is_file()


# ---------------------------------------------------------------------------
# The prompt helpers route through the channel
# ---------------------------------------------------------------------------


def test_browser_base_helpers_use_the_installed_channel() -> None:
    """The handlers call these two functions; swapping the channel has to
    change where their prompts go, without touching any handler."""
    from fetchers.browser import base

    class _Recording(interaction.InteractionChannel):
        def __init__(self):
            self.asked: list[str] = []

        def wait_for_user(self, prompt: str) -> None:
            self.asked.append(prompt)

        def read_line(self, prompt: str) -> str:
            self.asked.append(prompt)
            return "A"

    recorder = _Recording()
    interaction.set_channel(recorder)

    base._wait_for_user("press enter")
    assert base._read_user_line("y/n/A?") == "A"
    assert recorder.asked == ["press enter", "y/n/A?"]


# ---------------------------------------------------------------------------
# enrich_pdfs wiring
# ---------------------------------------------------------------------------


def _args(**kw):
    import argparse

    defaults = {
        "control_file": "", "control_timeout": 1800.0, "no_prompt": False,
    }
    return argparse.Namespace(**{**defaults, **kw})


def test_a_control_file_installs_the_control_file_channel(tmp_path) -> None:
    import enrich_pdfs

    enrich_pdfs._install_interaction_channel(
        _args(control_file=str(tmp_path / "c.json")),
    )
    assert isinstance(interaction.get_channel(), interaction.ControlFileChannel)


def test_no_prompt_installs_the_auto_skip_channel() -> None:
    import enrich_pdfs

    enrich_pdfs._install_interaction_channel(_args(no_prompt=True))
    assert isinstance(interaction.get_channel(), interaction.AutoSkipChannel)


def test_a_control_file_beats_no_prompt(tmp_path) -> None:
    """Both given means the caller wants a human reachable through the
    file — auto-skip would answer every challenge with "skip" instead."""
    import enrich_pdfs

    enrich_pdfs._install_interaction_channel(
        _args(control_file=str(tmp_path / "c.json"), no_prompt=True),
    )
    assert isinstance(interaction.get_channel(), interaction.ControlFileChannel)


def test_plain_args_leave_the_tty_channel_alone() -> None:
    import enrich_pdfs

    enrich_pdfs._install_interaction_channel(_args())
    assert isinstance(interaction.get_channel(), interaction.TtyChannel)


def test_auto_publishers_reads_the_audits_retry_set(tmp_path) -> None:
    """The audit already decided which items a browser pass recovers.
    Re-deriving it here would be a second implementation of the triage."""
    import enrich_pdfs

    stem = tmp_path / "audit"
    (tmp_path / "audit.retry.browser.keys").write_text("AAA\nBBB\n")
    (tmp_path / "audit.retry.browser.sage.keys").write_text("AAA\n")
    (tmp_path / "audit.retry.browser.aom.keys").write_text("BBB\n")

    keys_file, publishers = enrich_pdfs._auto_publisher_keys(str(stem))

    assert Path(keys_file).name == "audit.retry.browser.keys"
    assert publishers == ["aom", "sage"]


def test_auto_publishers_reports_nothing_when_the_audit_has_not_run(
    tmp_path,
) -> None:
    """Silently falling back to the whole library is the difference
    between 76 targeted items and 1,500."""
    import enrich_pdfs

    keys_file, publishers = enrich_pdfs._auto_publisher_keys(
        str(tmp_path / "audit"),
    )
    assert keys_file == ""
    assert publishers == []


def test_the_combined_file_is_not_mistaken_for_a_publisher(tmp_path) -> None:
    """`audit.retry.browser.keys` and `audit.retry.browser.sage.keys`
    share a prefix; only the second names a publisher."""
    import enrich_pdfs

    (tmp_path / "audit.retry.browser.keys").write_text("AAA\n")
    _keys, publishers = enrich_pdfs._auto_publisher_keys(str(tmp_path / "audit"))
    assert publishers == []
