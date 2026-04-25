"""Tests for the interactive-surface guard in enrich_pdfs (T4-2).

Surfaces the "browser cascade silently hangs in Bash subprocess"
failure mode the user hit in the SLR session log
("The browser did not open. Do I need to run it in a proper terminal?").
The fix: enrich_pdfs._has_interactive_surface() returns False in
the absence of /dev/tty and a TTY-shaped stdin, and the cascade
exits with a paste-in command instead of starting and hanging.
"""

from __future__ import annotations

import argparse
import sys

import enrich_pdfs as enrich_module
import pytest


@pytest.fixture
def enrich():
    return enrich_module


def test_has_interactive_surface_returns_false_when_neither_tty_nor_stdin(
    enrich, monkeypatch,
) -> None:
    """The Bash-subprocess shape: no /dev/tty (controlling terminal),
    and stdin.isatty() is False. The guard must return False so the
    caller can fail fast instead of starting and hanging on the first
    `_wait_for_user()` prompt."""
    # Force open("/dev/tty") to fail.

    def _no_tty(*_a, **_kw):
        raise OSError("no /dev/tty in test env")
    monkeypatch.setattr("builtins.open", _no_tty)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert enrich._has_interactive_surface() is False


def test_has_interactive_surface_falls_back_to_stdin_isatty_true(
    enrich, monkeypatch,
) -> None:
    """When /dev/tty isn't openable but stdin IS a TTY (Windows /
    odd shell config), still return True — the script can prompt."""
    def _no_tty(*_a, **_kw):
        raise OSError("no /dev/tty")
    monkeypatch.setattr("builtins.open", _no_tty)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    assert enrich._has_interactive_surface() is True


def test_exit_no_interactive_surface_includes_copy_paste_command(
    enrich,
) -> None:
    """The error message must include a paste-in command for a fresh
    terminal — the user shouldn't have to invent the invocation."""
    args = argparse.Namespace(publisher="", filter_keys_file="")
    with pytest.raises(SystemExit) as exc:
        enrich._exit_no_interactive_surface(args)
    msg = str(exc.value)
    assert "interactive terminal" in msg
    assert "${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/enrich_pdfs.py" in msg
    assert "--browser" in msg
    assert "--no-prompt" in msg


def test_exit_no_interactive_surface_threads_publisher_filter(
    enrich,
) -> None:
    """When the original invocation specified --publisher / --filter-keys-file,
    propagate them in the suggested re-run command so the user doesn't
    lose state."""
    args = argparse.Namespace(
        publisher="sage",
        filter_keys_file="output/missing_pdf.keys",
    )
    with pytest.raises(SystemExit) as exc:
        enrich._exit_no_interactive_surface(args)
    msg = str(exc.value)
    assert "--publisher sage" in msg
    assert "--filter-keys-file output/missing_pdf.keys" in msg
