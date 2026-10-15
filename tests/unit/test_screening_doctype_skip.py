"""Screening skips doctype:*-tagged items and says how many, per class.

Supplements, front matter and notices are imported (and kept) but are not
research articles. Neither screening stage may spend a model call on them,
and the count is printed every run because PRISMA reports them as removed
before screening.
"""

from __future__ import annotations

import ast
from pathlib import Path

import abstract_screen
import fulltext_code
import pytest
import screening_common


def _item(key, title="A study", *tags, doi=""):
    return {"key": key, "data": {"key": key, "title": title, "DOI": doi,
                                 "creators": [],
                                 "tags": [{"tag": t} for t in tags]}}


def test_tagged_items_are_dropped_and_counted(capsys):
    items = [
        _item("A"),
        _item("S1", "Supplemental Material for X", "doctype:supplement"),
        _item("S2", "Supplemental Material for Y", "doctype:supplement"),
        _item("F1", "Editorial Board", "doctype:front-matter"),
        _item("N1", "Publication Notice", "doctype:notice"),
    ]
    kept, skipped = screening_common.drop_doctype_items(
        items, stage="abstract screening",
    )
    assert [i["key"] for i in kept] == ["A"]
    assert skipped == {"supplement": 2, "front-matter": 1, "notice": 1}
    out = capsys.readouterr().out
    assert "Skipped before abstract screening" in out
    assert "4 (supplement 2, front-matter 1, notice 1)" in out


def test_untagged_lookalikes_stay_in_but_are_warned_about(capsys):
    items = [_item("A"), _item("E", "Editorial Board"),
             _item("S", "", doi="10.1037/apl0000550.supp")]
    kept, skipped = screening_common.drop_doctype_items(
        items, stage="abstract screening",
    )
    assert len(kept) == 3 and skipped == {}
    out = capsys.readouterr().out
    assert "2 untagged item(s)" in out
    assert "tag_doctypes.py" in out


def test_quiet_prints_nothing(capsys):
    screening_common.drop_doctype_items(
        [_item("S", "x", "doctype:supplement"), _item("E", "Editorial Board")],
        stage="x", quiet=True,
    )
    assert capsys.readouterr().out == ""


def test_an_ordinary_collection_prints_nothing(capsys):
    kept, skipped = screening_common.drop_doctype_items(
        [_item("A"), _item("B")], stage="abstract screening",
    )
    assert len(kept) == 2 and skipped == {}
    assert capsys.readouterr().out == ""


# --- wiring: every selection path is narrowed ---------------------------

def _calls(module, func_name: str, callee: str) -> int:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == func_name)
    return sum(
        1 for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and getattr(n.func, "attr", getattr(n.func, "id", None)) == callee
    )


def test_abstract_screen_selection_drops_doctype_items():
    assert _calls(abstract_screen, "main", "drop_doctype_items") == 1


@pytest.mark.parametrize("module", [fulltext_code])
def test_fulltext_every_eligibility_pass_drops_doctype_items(module):
    """Every abstract-stage narrowing is followed by the doctype drop: a
    fresh enumeration that skipped it would re-admit supplements.
    (`--full-recode` now works on the already-narrowed list rather than
    re-reading the collection.)"""
    n_eligible = _calls(module, "main", "_abstract_stage_eligible")
    assert n_eligible >= 1
    assert _calls(module, "main", "drop_doctype_items") == n_eligible
