"""The `/setup` wizard's review of stored automated-download consent.

Scratch HOME only; the real config.toml is never read or written.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
import tomllib
from pathlib import Path

import pytest

WIZARD = Path(__file__).resolve().parents[2] / "scripts" / "setup" / "wizard.py"


@pytest.fixture
def mod(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    spec = importlib.util.spec_from_file_location("wizard_consent_under_test", WIZARD)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    assert m.CONFIG_PATH.is_relative_to(home)
    return m


def _answers(monkeypatch, *replies: str) -> list[str]:
    asked: list[str] = []
    it = iter(replies)

    def fake_input(prompt=""):
        asked.append(prompt)
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", fake_input)
    return asked


STORED = {"publisher_consent": {
    "sage": {"automated": "allow", "confirmed": dt.date(2026, 1, 2)},
    "jstor": {"automated": "deny", "confirmed": dt.date(2026, 3, 4)},
}}


def test_non_interactive_changes_nothing(mod, monkeypatch) -> None:
    asked = _answers(monkeypatch)
    assert mod._offer_consent_review(False, STORED) == {}
    assert asked == []


def test_nothing_stored_asks_nothing_and_explains(mod, monkeypatch, capsys) -> None:
    asked = _answers(monkeypatch)
    assert mod._offer_consent_review(True, {}) == {}
    assert asked == []
    assert "asks once" in capsys.readouterr().out


def test_lists_and_keeps_on_enter(mod, monkeypatch, capsys) -> None:
    _answers(monkeypatch, "")
    assert mod._offer_consent_review(True, STORED) == {}
    out = capsys.readouterr().out
    assert "1. jstor: deny (confirmed 2026-03-04)" in out
    assert "2. sage: allow (confirmed 2026-01-02)" in out


def test_changing_an_answer_dates_it_today(mod, monkeypatch) -> None:
    _answers(monkeypatch, "2", "deny")
    got = mod._offer_consent_review(True, STORED)
    assert got == {"sage": {"automated": "deny", "confirmed": dt.date.today()}}


def test_the_change_is_written_and_everything_else_kept(mod, monkeypatch) -> None:
    existing = {**STORED, "zotero": {"api_key": "k"}}
    _answers(monkeypatch, "1", "allow")
    changes = mod._offer_consent_review(True, existing)
    merged = mod._merge_config(existing, {"publisher_consent": changes})
    mod._write_config(merged)
    cfg = tomllib.loads(mod.CONFIG_PATH.read_text(encoding="utf-8"))
    assert cfg["publisher_consent"]["jstor"]["automated"] == "allow"
    assert cfg["publisher_consent"]["jstor"]["confirmed"] == dt.date.today()
    assert cfg["publisher_consent"]["sage"] == {
        "automated": "allow", "confirmed": dt.date(2026, 1, 2)}
    assert cfg["zotero"]["api_key"] == "k"


def test_unparseable_selection_changes_nothing(mod, monkeypatch) -> None:
    _answers(monkeypatch, "sage")
    assert mod._offer_consent_review(True, STORED) == {}
