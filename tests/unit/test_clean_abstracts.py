"""The one-off repair for abstracts already stored in Zotero."""

from __future__ import annotations

import clean_abstracts


def _item(key, abstract, doi="10.1/x"):
    return {"key": key, "version": 1,
            "data": {"abstractNote": abstract, "DOI": doi, "title": "T"}}


def test_plan_lists_only_items_that_change_and_says_why() -> None:
    items = [
        _item("A", "© 2021 Springer Nature.Given recent concerns we test it."),
        _item("B", "Firms &amp; markets are studied here in detail."),
        _item("C", "AbstractWe study labor unions."),
        _item("D", "A clean abstract about firms."),
        _item("E", ""),
    ]
    plan = {r.key: r for r in clean_abstracts.plan_repairs(items)}
    assert set(plan) == {"A", "B", "C"}
    assert plan["A"].after == "Given recent concerns we test it."
    assert plan["A"].reasons == ["copyright"]
    assert plan["B"].reasons == ["entities"]
    assert plan["C"].reasons == ["heading"]


def test_an_abstract_that_would_become_empty_is_not_planned() -> None:
    """Emptying a field is a deletion, which a clean-up must not do; the
    item is left for a human."""
    items = [_item("A", "© 2006 Elsevier B.V. All rights reserved.")]
    assert clean_abstracts.plan_repairs(items) == []


def test_the_default_is_a_dry_run() -> None:
    args = clean_abstracts._build_parser().parse_args([])
    assert args.write is False
