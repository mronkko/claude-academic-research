"""The `/setup` wizard's three `[pdf]` retrieval-policy questions.

Scratch HOME only (the `home` fixture), every probe stubbed; the real
config.toml is never read or written.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tomllib
from pathlib import Path

import pytest

WIZARD = Path(__file__).resolve().parents[2] / "scripts" / "setup" / "wizard.py"


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for name in list(os.environ):
        if name.endswith(("_API_KEY", "_TOKEN", "_BASE_URL", "_APP_PASSWORD")) or name in (
            "ACADEMIC_RESEARCH_PROVIDER", "LOCALAPPDATA", "CROSSREF_MAILTO",
            "UNPAYWALL_EMAIL",
        ):
            monkeypatch.delenv(name, raising=False)
    return home


@pytest.fixture
def mod(home):
    spec = importlib.util.spec_from_file_location("wizard_pdf_policy_under_test", WIZARD)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    assert m.CONFIG_PATH.is_relative_to(home)
    return m


def _answers(monkeypatch, *replies: str) -> list[str]:
    """Feed `replies` to input(); record the prompts that were asked."""
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


_KEY = {"openalex": {"api_key": "k"}}


def test_interactive_answers_are_written_and_paid_is_mirrored(mod, monkeypatch) -> None:
    asked = _answers(monkeypatch, "published", "y", "off")
    out = mod._prompt_pdf_policy(True, {}, _KEY)
    assert out["pdf"] == {"min_version": "published", "reconstructed": True, "paid": "off"}
    # enrich_abstracts reads only the old key; "never pay" must hold there.
    assert out["openalex"] == {"use_paid_content_api": False}
    assert len(asked) == 3


def test_defaults_on_enter_and_the_paid_recommendation_is_shown(mod, monkeypatch, capsys) -> None:
    _answers(monkeypatch, "", "", "")
    out = mod._prompt_pdf_policy(True, {}, _KEY)
    assert out["pdf"] == {"min_version": "accepted", "reconstructed": False, "paid": "prefer"}
    assert out["openalex"] == {"use_paid_content_api": True}
    text = " ".join(capsys.readouterr().out.split())
    assert "pay before spending your own time on manual downloads" in text


def test_choices_by_number_and_a_bad_answer_is_asked_again(mod, monkeypatch) -> None:
    asked = _answers(monkeypatch, "vor", "3", "n", "3")
    out = mod._prompt_pdf_policy(True, {}, _KEY)
    assert out["pdf"]["min_version"] == "preprint"
    assert out["pdf"]["paid"] == "last_resort"
    assert len(asked) == 4


def test_no_openalex_key_skips_the_paid_question(mod, monkeypatch) -> None:
    asked = _answers(monkeypatch, "", "")
    out = mod._prompt_pdf_policy(
        True, {"openalex": {"use_paid_content_api": False}}, {},
    )
    assert len(asked) == 2
    assert out["pdf"]["paid"] == "off", "the stored answer stays"
    assert "openalex" not in out


def test_rerun_offers_the_stored_answers_as_defaults(mod, monkeypatch) -> None:
    _answers(monkeypatch, "", "", "")
    existing = {
        "pdf": {"min_version": "published", "reconstructed": True, "paid": "last_resort"},
        "openalex": {"api_key": "k"},
    }
    assert mod._prompt_pdf_policy(True, existing, {})["pdf"] == existing["pdf"]


def test_end_of_input_keeps_the_stored_answers(mod, monkeypatch) -> None:
    _answers(monkeypatch)
    existing = {"pdf": {"min_version": "preprint"}, "elsevier": {"render_xml_to_pdf": True}}
    out = mod._prompt_pdf_policy(True, existing, _KEY)
    assert out["pdf"] == {"min_version": "preprint", "reconstructed": True, "paid": "prefer"}


def test_an_invalid_stored_value_falls_back_to_the_default(mod) -> None:
    out = mod._prompt_pdf_policy(False, {"pdf": {"min_version": "vor", "paid": 3}}, {})
    assert out["pdf"]["min_version"] == "accepted"
    assert out["pdf"]["paid"] == "prefer"


def _run(mod, monkeypatch) -> int:
    monkeypatch.setenv("ZOTERO_API_KEY", "dummy-zotero-key")
    monkeypatch.setattr(sys, "argv", ["wizard.py", "--non-interactive", "--skip-verify"])
    monkeypatch.setattr(mod, "_check_zotero_local", lambda *a, **k: ("not_running", "stub"))
    monkeypatch.setattr(mod, "_check_zotero_cli", lambda: ("ok", "stub"))
    monkeypatch.setattr(mod, "_check_mcp_servers", lambda: {})
    monkeypatch.setattr(mod, "_agy_available", lambda: False)
    monkeypatch.setattr(mod, "_maybe_add_claude_to_gitignore", lambda *a, **k: None)
    return mod.main()


def test_non_interactive_run_writes_defaults(mod, monkeypatch) -> None:
    _run(mod, monkeypatch)
    cfg = tomllib.loads(mod.CONFIG_PATH.read_text(encoding="utf-8"))
    assert cfg["pdf"] == {"min_version": "accepted", "reconstructed": False, "paid": "prefer"}


def test_non_interactive_rerun_keeps_answers_and_other_keys(mod, monkeypatch) -> None:
    mod.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    mod.CONFIG_PATH.write_text(
        '[pdf]\nmin_version = "published"\n\n'
        '[openalex]\napi_key = "k"\nuse_paid_content_api = false\n',
        encoding="utf-8",
    )
    _run(mod, monkeypatch)
    cfg = tomllib.loads(mod.CONFIG_PATH.read_text(encoding="utf-8"))
    assert cfg["pdf"] == {"min_version": "published", "reconstructed": False, "paid": "off"}
    assert cfg["openalex"]["use_paid_content_api"] is False
    assert cfg["openalex"]["api_key"] == "k"
