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


def test_prompt_skipped_entirely_on_non_tty(monkeypatch, capsys) -> None:
    stdin_mock = MagicMock()
    stdin_mock.isatty.return_value = False
    monkeypatch.setattr(enrich_pdfs.sys, "stdin", stdin_mock)

    result = _prompt_on_first_failure(_make_handler(), 1, _make_args())

    assert result == "skip"
    stdin_mock.readline.assert_not_called()
    assert capsys.readouterr().out == ""


def test_override_skips_prompt_entirely(monkeypatch, capsys) -> None:
    stdin_mock = MagicMock()
    monkeypatch.setattr(enrich_pdfs.sys, "stdin", stdin_mock)

    result = _prompt_on_first_failure(
        _make_handler(), 1, _make_args(on_first_failure="always_skip"),
    )

    assert result == "always_skip"
    stdin_mock.readline.assert_not_called()
