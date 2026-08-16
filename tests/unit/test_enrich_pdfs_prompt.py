"""Unit tests for `_prompt_on_first_failure` in enrich_pdfs.py.

Regression coverage: the function reads stdin directly (no `/dev/tty`
detour) because it's only reached after the caller has already
confirmed `sys.stdin.isatty()`. An earlier version opened `/dev/tty`
inside a try/except and only wrote the "> " prompt on the successful
branch, so on platforms without `/dev/tty` (Windows) the prompt never
printed while the script silently waited on the stdin fallback.
"""

from __future__ import annotations

import argparse
from unittest.mock import MagicMock

import enrich_pdfs
from enrich_pdfs import _prompt_on_first_failure


def _make_args(**overrides) -> argparse.Namespace:
    defaults = {"on_first_failure": ""}
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _make_handler() -> MagicMock:
    handler = MagicMock()
    handler.display_name = "Elsevier"
    handler.name = "elsevier"
    return handler


def test_prompt_shows_and_reads_stdin_directly(monkeypatch, capsys) -> None:
    stdin_mock = MagicMock()
    stdin_mock.isatty.return_value = True
    stdin_mock.readline.return_value = "k\n"
    monkeypatch.setattr(enrich_pdfs.sys, "stdin", stdin_mock)

    result = _prompt_on_first_failure(_make_handler(), 3, _make_args())

    assert result == "keep"
    assert "> " in capsys.readouterr().out
    stdin_mock.readline.assert_called_once()


def test_prompt_defaults_to_skip_on_empty_answer(monkeypatch, capsys) -> None:
    stdin_mock = MagicMock()
    stdin_mock.isatty.return_value = True
    stdin_mock.readline.return_value = "\n"
    monkeypatch.setattr(enrich_pdfs.sys, "stdin", stdin_mock)

    result = _prompt_on_first_failure(_make_handler(), 1, _make_args())

    assert result == "skip"


def test_prompt_skipped_entirely_when_no_channel_can_reach_a_human(
    monkeypatch, capsys,
) -> None:
    """Supersedes an assertion that a non-TTY stdin means skip.

    That conflated "can we ask a human" with "is stdin a terminal" — the
    coupling `fetchers.browser.interaction` exists to undo. Reachability is
    now the channel's answer, not stdin's. A non-interactive channel still
    skips, still without asking and without printing.
    """
    from fetchers.browser import interaction

    class Unreachable(interaction.InteractionChannel):
        interactive = False

        def wait_for_user(self, prompt):
            raise AssertionError("must not ask when nobody is reachable")

        def read_line(self, prompt):
            raise AssertionError("must not ask when nobody is reachable")

    monkeypatch.setattr(interaction, "get_channel", lambda: Unreachable())
    stdin_mock = MagicMock()
    stdin_mock.isatty.return_value = False
    monkeypatch.setattr(enrich_pdfs.sys, "stdin", stdin_mock)

    result = _prompt_on_first_failure(_make_handler(), 1, _make_args())

    assert result == "skip"
    stdin_mock.readline.assert_not_called()
    assert capsys.readouterr().out == ""


def test_a_non_tty_run_still_asks_when_a_control_file_can_reach_the_user(
    monkeypatch,
) -> None:
    """The regression: stdin is not a terminal, but the user is reachable
    through the control file and must be asked. Silently skipping here is
    how reinert_2025_sgr lost its second APA article -- never attempted,
    and indistinguishable in the log from one nobody had a route to."""
    from fetchers.browser import interaction

    asked = []

    class ViaControlFile(interaction.InteractionChannel):
        interactive = True

        def wait_for_user(self, prompt):
            asked.append(prompt)

        def read_line(self, prompt):
            asked.append(prompt)
            return "k"

    monkeypatch.setattr(interaction, "get_channel", lambda: ViaControlFile())
    stdin_mock = MagicMock()
    stdin_mock.isatty.return_value = False
    monkeypatch.setattr(enrich_pdfs.sys, "stdin", stdin_mock)

    assert _prompt_on_first_failure(_make_handler(), 1, _make_args()) == "keep"
    assert asked, "a reachable user was never asked"


def test_override_skips_prompt_entirely(monkeypatch, capsys) -> None:
    stdin_mock = MagicMock()
    monkeypatch.setattr(enrich_pdfs.sys, "stdin", stdin_mock)

    result = _prompt_on_first_failure(
        _make_handler(), 1, _make_args(on_first_failure="always_skip"),
    )

    assert result == "always_skip"
    stdin_mock.readline.assert_not_called()
