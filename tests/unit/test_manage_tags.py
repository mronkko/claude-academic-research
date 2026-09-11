"""Tests for the tag listing / pruning tool.

The whole tool rests on the namespace: it enumerates and deletes by
`<prefix>/` so it cannot reach another review's tags or the user's own.
Most of these tests are about what it refuses to touch.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import manage_tags as mt
import pytest
import tag_prefix
import zotero_io

NS = tag_prefix.namespace("test-review")
OTHER_NS = tag_prefix.namespace("other-review")


def _item(key: str, *tags: str) -> dict:
    return {"key": key, "data": {"tags": [{"tag": t} for t in tags]}}


ITEMS = [
    _item("A", f"{NS}fulltext:include", f"{NS}research-design:panel"),
    _item("B", f"{NS}fulltext:include", f"{NS}research-design:survey"),
    _item("C", f"{NS}fulltext:exclude", f"{OTHER_NS}research-design:panel"),
    _item("D", "predatory:flag", "my own tag", f"{NS}research-design:panel"),
]


# ---------------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------------


def test_only_this_reviews_tags_are_counted():
    counts = mt.tags_in_namespace(ITEMS, NS)
    assert counts == {
        f"{NS}fulltext:include": 2,
        f"{NS}fulltext:exclude": 1,
        f"{NS}research-design:panel": 2,
        f"{NS}research-design:survey": 1,
    }


def test_another_reviews_tags_are_invisible():
    counts = mt.tags_in_namespace(ITEMS, NS)
    assert not any(t.startswith(OTHER_NS) for t in counts)


def test_the_users_own_tags_and_global_flags_are_invisible():
    counts = mt.tags_in_namespace(ITEMS, NS)
    assert "predatory:flag" not in counts
    assert "my own tag" not in counts


def test_an_empty_library_counts_nothing():
    assert mt.tags_in_namespace([], NS) == {}


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


def test_tags_group_by_family():
    families = mt.group_by_family(mt.tags_in_namespace(ITEMS, NS), NS)
    assert sorted(families) == ["fulltext", "research-design"]
    assert [t for t, _ in families["research-design"]] == [
        f"{NS}research-design:panel",
        f"{NS}research-design:survey",
    ]


def test_a_hyphenated_qa_tag_buckets_under_its_whole_name():
    """The QA family spells itself with a hyphen, not a colon."""
    counts = {f"{NS}qa-adjudicated-include": 3}
    assert list(mt.group_by_family(counts, NS)) == ["qa-adjudicated-include"]


# ---------------------------------------------------------------------------
# What counts as a decision family
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "family", ["abstract", "fulltext", "qa", "qa-adjudicated-include", "qa-flag"],
)
def test_decision_families_are_recognised(family):
    assert mt.is_decision_family(family)


@pytest.mark.parametrize(
    "family", ["research-design", "causal-strength", "search", "abstraction"],
)
def test_coded_and_unrelated_families_are_not_decision_families(family):
    assert not mt.is_decision_family(family)


# ---------------------------------------------------------------------------
# Family resolution
# ---------------------------------------------------------------------------


def test_a_family_resolves_from_the_coding_field_name():
    assert mt.resolve_family("research_design", NS) == f"{NS}research-design:"


def test_a_family_resolves_from_the_slugged_form_too():
    """The user reads one form in the config and the other in Zotero."""
    assert mt.resolve_family("research-design", NS) == f"{NS}research-design:"


# ---------------------------------------------------------------------------
# End to end through main()
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_zot(monkeypatch):
    client = MagicMock()
    client.describe_library.return_value = "group 12345"
    client.collection_items.return_value = ITEMS
    client.journal_articles.return_value = ITEMS
    client.batch_update_tags.return_value = {
        "applied": 3, "unchanged": 0, "failed": 0,
    }
    monkeypatch.setattr(
        zotero_io.ZoteroClient, "from_args",
        classmethod(lambda cls, *a, **kw: client),
    )
    return client


def _argv(monkeypatch, *extra):
    import sys
    monkeypatch.setattr(sys, "argv", [
        "manage_tags.py", "--group", "12345",
        "--tag-prefix", NS.rstrip("/"),
        "--collection", "COLL1", *extra,
    ])


def test_list_reports_families_and_counts(fake_zot, monkeypatch, capsys):
    _argv(monkeypatch, "--list")
    assert mt.main() == 0
    out = capsys.readouterr().out
    assert "research-design" in out
    assert f"{NS}research-design:panel" in out
    assert "2 item(s)" in out
    fake_zot.batch_update_tags.assert_not_called()


def test_list_does_not_show_another_reviews_tags(fake_zot, monkeypatch, capsys):
    _argv(monkeypatch, "--list")
    mt.main()
    assert OTHER_NS not in capsys.readouterr().out


def test_prune_is_a_dry_run_by_default(fake_zot, monkeypatch, capsys):
    _argv(monkeypatch, "--prune", "research_design")
    assert mt.main() == 0
    out = capsys.readouterr().out
    assert "Would remove" in out
    assert "Dry run" in out
    fake_zot.batch_update_tags.assert_not_called()


def test_prune_with_apply_writes_the_removal(fake_zot, monkeypatch, capsys):
    _argv(monkeypatch, "--prune", "research_design", "--apply")
    assert mt.main() == 0
    fake_zot.batch_update_tags.assert_called_once()
    updates = fake_zot.batch_update_tags.call_args[0][0]
    assert {k for k, _ in updates} == {"A", "B", "D"}
    for _, op in updates:
        assert op == {"remove_prefixed": [f"{NS}research-design:"]}


def test_prune_does_not_touch_the_item_holding_only_another_reviews_tag(
    fake_zot, monkeypatch,
):
    """Item C carries `other-review/research-design:panel` and nothing of
    ours in that family — it must not appear in the write at all."""
    _argv(monkeypatch, "--prune", "research_design", "--apply")
    mt.main()
    updates = fake_zot.batch_update_tags.call_args[0][0]
    assert "C" not in {k for k, _ in updates}


def test_pruning_a_decision_family_is_refused(fake_zot, monkeypatch):
    _argv(monkeypatch, "--prune", "fulltext", "--apply")
    with pytest.raises(SystemExit) as exc:
        mt.main()
    assert "screening decisions" in str(exc.value)
    fake_zot.batch_update_tags.assert_not_called()


def test_a_decision_family_can_be_pruned_with_force(fake_zot, monkeypatch):
    _argv(monkeypatch, "--prune", "fulltext", "--apply", "--force")
    assert mt.main() == 0
    fake_zot.batch_update_tags.assert_called_once()


def test_pruning_an_absent_family_writes_nothing(fake_zot, monkeypatch, capsys):
    _argv(monkeypatch, "--prune", "never_coded", "--apply")
    assert mt.main() == 0
    assert "Nothing to prune" in capsys.readouterr().out
    fake_zot.batch_update_tags.assert_not_called()


def test_no_collection_scans_the_whole_library(fake_zot, monkeypatch):
    import sys
    monkeypatch.setattr(sys, "argv", [
        "manage_tags.py", "--group", "12345",
        "--tag-prefix", NS.rstrip("/"), "--list",
    ])
    assert mt.main() == 0
    fake_zot.journal_articles.assert_called_once()
    fake_zot.collection_items.assert_not_called()


def test_neither_list_nor_prune_is_an_error(fake_zot, monkeypatch):
    _argv(monkeypatch)
    with pytest.raises(SystemExit):
        mt.main()


def test_a_failed_batch_exits_nonzero(fake_zot, monkeypatch):
    fake_zot.batch_update_tags.return_value = {
        "applied": 1, "unchanged": 0, "failed": 2,
    }
    _argv(monkeypatch, "--prune", "research_design", "--apply")
    assert mt.main() == 1
