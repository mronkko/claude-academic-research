"""Tests for the Beall's list predatory-journal check."""

from __future__ import annotations

from pathlib import Path

from sources import predatory

FIXTURES = Path(__file__).parent / "fixtures" / "predatory"


def _redirect_data_dir(monkeypatch, fixture_name: str) -> None:
    """Point the module at a fixture directory and bust the caches."""
    monkeypatch.setattr(predatory, "DATA_DIR", str(FIXTURES / fixture_name))
    predatory._publisher_patterns.cache_clear()
    predatory._standalone_patterns.cache_clear()
    predatory._issn_set.cache_clear()


def test_empty_snapshot_flags_nothing(monkeypatch) -> None:
    _redirect_data_dir(monkeypatch, "empty")
    r = predatory.check_predatory(journal="Journal of Business Venturing", issn="0883-9026")
    assert not r.is_predatory
    assert r.source == ""


def test_issn_match_flags(monkeypatch) -> None:
    _redirect_data_dir(monkeypatch, "sample")
    r = predatory.check_predatory(journal="Any Title", issn="9999-0001")
    assert r.is_predatory
    assert r.source == "beall_issn"


def test_standalone_substring_match(monkeypatch) -> None:
    _redirect_data_dir(monkeypatch, "sample")
    r = predatory.check_predatory(journal="International Journal of Fake Research", issn=None)
    assert r.is_predatory
    assert r.source == "beall_standalone"


def test_publisher_substring_match(monkeypatch) -> None:
    _redirect_data_dir(monkeypatch, "sample")
    r = predatory.check_predatory(journal="Scirp Journal of Something", issn=None)
    assert r.is_predatory
    assert r.source == "beall_publisher"


def test_issn_normalisation(monkeypatch) -> None:
    """ISSN match should be robust to formatting differences."""
    _redirect_data_dir(monkeypatch, "sample")
    r = predatory.check_predatory(journal="x", issn="9999 0001")
    assert r.is_predatory
    r2 = predatory.check_predatory(journal="x", issn="99990001")
    assert r2.is_predatory


def test_comments_and_placeholder_ignored(monkeypatch) -> None:
    """Lines starting with # and the literal `_placeholder` are skipped."""
    _redirect_data_dir(monkeypatch, "sample")
    # _placeholder and # comments must not be treated as match patterns
    r = predatory.check_predatory(journal="_placeholder", issn=None)
    assert not r.is_predatory
    r2 = predatory.check_predatory(journal="# a comment", issn=None)
    assert not r2.is_predatory
