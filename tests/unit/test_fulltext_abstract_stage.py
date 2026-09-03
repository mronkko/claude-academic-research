"""Full-text coding must not process items the abstract stage excluded.

The skill states this as doctrine, under the tag conventions:
"Filtering downstream stages. `fulltext_code.py` processes items tagged
`abstract:include` OR `abstract:borderline`." The script's own docstring
says the same. The code did neither: it enumerated every journalArticle
in the collection and filtered only on `fulltext:*` resume tags, so the
word `abstract:` appeared nowhere outside that one line of prose.

Stage tags are applied in place, in one collection, so nothing else
narrowed the population either. A downstream review ran a dry run that
reported 614 of 615 items eligible where 439 had passed abstract
screening.

Wasted spend is the smaller half. `fulltext_code.py` writes
`fulltext:include` as an authoritative Zotero tag, and
`export_coded_includes.py` selects on that tag with no cross-check
against the abstract stage — so an abstract-excluded item that coded as
include would enter the final corpus and the PRISMA counts, wearing a tag
indistinguishable from a legitimate one.

The filter auto-detects rather than applying unconditionally: a
collection that was never abstract-screened has no `abstract:*` tags at
all, and filtering it would silently yield nothing — the same class of
bug in the opposite direction.
"""

from __future__ import annotations

import fulltext_code


def _item(key: str, *tags: str) -> dict:
    return {"key": key, "data": {"title": key,
                                 "tags": [{"tag": t} for t in tags]}}


# ---------------------------------------------------------------------------
# The filter
# ---------------------------------------------------------------------------


def test_abstract_includes_and_borderlines_are_kept() -> None:
    items = [_item("A", "abstract:include"), _item("B", "abstract:borderline")]
    kept, report = fulltext_code._abstract_stage_eligible(items)
    assert [i["key"] for i in kept] == ["A", "B"]
    assert report is not None


def test_abstract_excludes_are_dropped() -> None:
    """The 176 items that cost a downstream review a manual workaround."""
    items = [_item("A", "abstract:include"), _item("X", "abstract:exclude")]
    kept, _ = fulltext_code._abstract_stage_eligible(items)
    assert [i["key"] for i in kept] == ["A"]


def test_an_untagged_item_is_dropped_when_the_collection_was_screened() -> None:
    """Screened collection, no abstract verdict on this item: it was never
    judged, so full-text coding is not the place to start."""
    items = [_item("A", "abstract:include"), _item("U")]
    kept, _ = fulltext_code._abstract_stage_eligible(items)
    assert [i["key"] for i in kept] == ["A"]


def test_an_unscreened_collection_is_passed_through_whole() -> None:
    """No `abstract:*` tag anywhere means no abstract stage was run.
    Filtering here would return zero items and read as "nothing to do",
    which is this same bug pointing the other way."""
    items = [_item("A"), _item("B"), _item("C")]
    kept, report = fulltext_code._abstract_stage_eligible(items)
    assert [i["key"] for i in kept] == ["A", "B", "C"]
    assert report is not None
    assert "no abstract" in report.lower()


def test_the_unscreened_notice_names_the_screening_script() -> None:
    """Someone who meant to screen first needs to be told what to run."""
    _kept, report = fulltext_code._abstract_stage_eligible([_item("A")])
    assert "abstract_screen.py" in report


def test_the_report_accounts_for_every_item() -> None:
    """The count has to reconcile, because the operator is about to spend
    money against it."""
    items = [_item("A", "abstract:include"), _item("B", "abstract:borderline"),
             _item("X", "abstract:exclude"), _item("Y", "abstract:exclude"),
             _item("U")]
    kept, report = fulltext_code._abstract_stage_eligible(items)
    assert len(kept) == 2
    assert "5" in report and "2" in report and "3" in report


def test_a_fulltext_tag_does_not_confer_eligibility() -> None:
    """Resume is a separate question, handled downstream. An item that
    was abstract-excluded and somehow carries a fulltext tag is exactly
    the contamination case — it must not be re-admitted here."""
    items = [_item("X", "abstract:exclude", "fulltext:include")]
    kept, _ = fulltext_code._abstract_stage_eligible(items)
    assert kept == []


def test_an_empty_collection_reports_rather_than_crashing() -> None:
    kept, report = fulltext_code._abstract_stage_eligible([])
    assert kept == []
    assert report is not None


def test_other_abstract_prefixed_tags_do_not_pass() -> None:
    """Only the two documented verdicts admit an item. A hand-added
    `abstract:maybe` is not one of them."""
    items = [_item("M", "abstract:maybe")]
    kept, _ = fulltext_code._abstract_stage_eligible(items)
    assert kept == []


def test_the_documented_verdicts_are_the_ones_implemented() -> None:
    """Guard on the constant itself: the skill's tag table names exactly
    these two as proceeding to full text."""
    assert fulltext_code.ABSTRACT_PASS_VALUES == ("include", "borderline")
    assert fulltext_code.ABSTRACT_TAG_PREFIX == "abstract:"


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_every_collection_enumeration_is_narrowed() -> None:
    """The helper is only worth having if it is on every path that reads
    the collection, and there are two: the initial enumeration and the
    re-read after `--full-recode` clears the resume tags. The bug this
    fixes was precisely an enumeration nobody narrowed, so an AST check
    rather than a comment — the same guard style as
    tests/unit/test_searcher_backoff.py.
    """
    import ast
    from pathlib import Path

    source = Path(fulltext_code.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    main = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )

    def _is_call(node: ast.AST, name: str) -> bool:
        return (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == name)

    enumerations = [n for n in ast.walk(main)
                    if _is_call(n, "collection_items")]
    narrowings = [n for n in ast.walk(main)
                  if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Name)
                  and n.func.id == "_abstract_stage_eligible"]
    assert len(enumerations) >= 2, (
        "expected the initial enumeration and the --full-recode re-read"
    )
    assert len(narrowings) == len(enumerations), (
        f"{len(enumerations)} collection enumeration(s) in main() but "
        f"{len(narrowings)} call(s) to _abstract_stage_eligible. Every "
        f"read of the collection has to be narrowed, or --full-recode "
        f"quietly re-admits the abstract-excluded items."
    )
