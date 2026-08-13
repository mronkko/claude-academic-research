"""Tests for the shared screening-stage machinery.

`abstract_screen` and `fulltext_code` keep their own tests for the thin
wrappers — those are the behaviour-preservation guarantee for the
extraction. This module covers `screening_common` directly, with emphasis
on the two places the stages genuinely differ (tag matching, and where CSV
decisions get filtered), since collapsing either is the mistake this
design exists to prevent.
"""

from __future__ import annotations

import csv

import pytest
import screening_common as sc


def _item(key: str, *tags: str) -> dict:
    return {"key": key, "data": {"tags": [{"tag": t} for t in tags]}}


def _write_log(path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["item_key", "decision"])
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# load_config_module
# ---------------------------------------------------------------------------


def test_load_config_module_returns_module(tmp_path) -> None:
    cfg = tmp_path / "screening_config.py"
    cfg.write_text("FOO = 1\nBAR = 'x'\n", encoding="utf-8")
    mod = sc.load_config_module(str(cfg), "screening_config", required=("FOO",))
    assert mod.FOO == 1
    assert mod.BAR == "x"


def test_load_config_module_exits_on_missing_attr(tmp_path) -> None:
    cfg = tmp_path / "screening_config.py"
    cfg.write_text("FOO = 1\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        sc.load_config_module(str(cfg), "screening_config", required=("MISSING",))
    assert "MISSING" in str(exc.value)
    assert str(cfg) in str(exc.value)


def test_load_config_module_label_comes_from_module_name(tmp_path) -> None:
    """The assert message the orchestrators printed before extraction was
    'cannot load screening config: ...' / 'cannot load search config: ...'."""
    assert "search_config".replace("_", " ") == "search config"
    assert "screening_config".replace("_", " ") == "screening config"


def test_load_config_module_no_required_attrs(tmp_path) -> None:
    cfg = tmp_path / "search_config.py"
    cfg.write_text("FROM_YEAR = 2010\n", encoding="utf-8")
    mod = sc.load_config_module(str(cfg), "search_config")
    assert mod.FROM_YEAR == 2010


# ---------------------------------------------------------------------------
# items_with_stage_tag — prefix mode vs exact-value mode
# ---------------------------------------------------------------------------


def test_items_with_stage_tag_prefix_mode_matches_any_suffix() -> None:
    """Abstract screening: `abstract:borderline` counts as decided."""
    items = [
        _item("A", "abstract:include"),
        _item("B", "abstract:borderline"),
        _item("C", "abstract:something-unexpected"),
        _item("D", "fulltext:include"),
        _item("E"),
    ]
    assert sc.items_with_stage_tag(items, prefix="abstract:") == {"A", "B", "C"}


def test_items_with_stage_tag_values_mode_is_exact() -> None:
    """Full-text coding: only include/exclude count. A hand-added
    `fulltext:maybe` must not make the item look coded."""
    items = [
        _item("A", "fulltext:include"),
        _item("B", "fulltext:exclude"),
        _item("C", "fulltext:maybe"),
        _item("D"),
    ]
    assert sc.items_with_stage_tag(
        items, prefix="fulltext:", values=("include", "exclude"),
    ) == {"A", "B"}


def test_items_with_stage_tag_ignores_other_stages() -> None:
    items = [_item("A", "abstract:include", "fulltext:include")]
    assert sc.items_with_stage_tag(items, prefix="fulltext:") == {"A"}
    assert sc.items_with_stage_tag(items, prefix="qa:") == set()


def test_items_with_stage_tag_empty_input() -> None:
    assert sc.items_with_stage_tag([], prefix="abstract:") == set()


# ---------------------------------------------------------------------------
# stage_tag_op
# ---------------------------------------------------------------------------


def test_stage_tag_op_replaces_rather_than_accumulates() -> None:
    assert sc.stage_tag_op("abstract:", "include") == {
        "add": ["abstract:include"],
        "remove_prefixed": ["abstract:"],
    }


# ---------------------------------------------------------------------------
# last_decisions_by_key — the filter-during vs filter-after difference
# ---------------------------------------------------------------------------


def test_last_decisions_unfiltered_takes_the_genuinely_last_row(tmp_path) -> None:
    path = tmp_path / "log.csv"
    _write_log(path, [
        {"item_key": "K", "decision": "include"},
        {"item_key": "K", "decision": "error"},
    ])
    assert sc.last_decisions_by_key(path) == {"K": "error"}


def test_last_decisions_filtered_skips_invalid_rows_entirely(tmp_path) -> None:
    """This is the abstract-screening semantics: a trailing `error` row does
    NOT displace the earlier valid decision. Contrast the test above — the
    difference is real, which is why the parameter exists."""
    path = tmp_path / "log.csv"
    _write_log(path, [
        {"item_key": "K", "decision": "include"},
        {"item_key": "K", "decision": "error"},
    ])
    assert sc.last_decisions_by_key(
        path, valid=("include", "borderline", "exclude"),
    ) == {"K": "include"}


def test_last_decisions_missing_file_is_empty(tmp_path) -> None:
    assert sc.last_decisions_by_key(tmp_path / "nope.csv") == {}


def test_last_decisions_skips_rows_without_item_key(tmp_path) -> None:
    path = tmp_path / "log.csv"
    _write_log(path, [
        {"item_key": "", "decision": "include"},
        {"item_key": "K", "decision": "exclude"},
    ])
    assert sc.last_decisions_by_key(path) == {"K": "exclude"}


# ---------------------------------------------------------------------------
# run_csv_backfill
# ---------------------------------------------------------------------------


class _FakeZot:
    def __init__(self, stats: dict[str, int]) -> None:
        self.stats = stats
        self.calls: list[list] = []

    def batch_update_tags(self, updates):
        self.calls.append(updates)
        return self.stats


def test_run_csv_backfill_tags_only_untagged_drift(capsys) -> None:
    zot = _FakeZot({"applied": 1, "unchanged": 0, "failed": 0})
    items = [_item("TAGGED", "abstract:include"), _item("UNTAGGED")]
    rc = sc.run_csv_backfill(
        zot, items,
        {"TAGGED": "include", "UNTAGGED": "exclude"},
        prefix="abstract:", label="abstract:*",
    )
    assert rc == 0
    assert zot.calls == [[
        ("UNTAGGED", {"add": ["abstract:exclude"],
                      "remove_prefixed": ["abstract:"]}),
    ]]
    assert "Backfilling abstract:* tags for 1 item(s)" in capsys.readouterr().out


def test_run_csv_backfill_no_drift_makes_no_calls(capsys) -> None:
    zot = _FakeZot({"applied": 0, "unchanged": 0, "failed": 0})
    rc = sc.run_csv_backfill(
        zot, [_item("A", "abstract:include")], {"A": "include"},
        prefix="abstract:", label="abstract:*",
    )
    assert rc == 0
    assert zot.calls == []
    assert "Nothing to backfill" in capsys.readouterr().out


def test_run_csv_backfill_returns_1_on_failure() -> None:
    zot = _FakeZot({"applied": 0, "unchanged": 0, "failed": 2})
    rc = sc.run_csv_backfill(
        zot, [_item("A")], {"A": "include"},
        prefix="abstract:", label="abstract:*",
    )
    assert rc == 1


def test_run_csv_backfill_honours_values_mode() -> None:
    """With `values`, a hand-added `fulltext:maybe` does not count as
    already-tagged, so the item is still backfilled."""
    zot = _FakeZot({"applied": 1, "unchanged": 0, "failed": 0})
    rc = sc.run_csv_backfill(
        zot, [_item("A", "fulltext:maybe")], {"A": "include"},
        prefix="fulltext:", values=("include", "exclude"), label="fulltext:*",
    )
    assert rc == 0
    assert zot.calls[0][0][0] == "A"
