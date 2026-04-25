"""Tests for apply_qa_adjudications (T2-2).

Replaces the user's downstream `apply_qa_adjudications.py` (4 edits in
the SLR session log) and specifically the pyzotero footgun that
showed up there: calling `add_tags()` with a stub item dict silently
dropped writes. This script routes through `zotero_io.batch_update_tags`,
which constructs the full payload with the right version field per
item.

Tests focus on the pure data layer (decision validation, op
construction) plus an integration test that mocks
`zotero_io.batch_update_tags` to confirm the right ops are dispatched.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import apply_qa_adjudications as apply
import pytest

# ---------------------------------------------------------------------------
# load_decisions — validation
# ---------------------------------------------------------------------------


def test_load_decisions_returns_normalised_list(tmp_path: Path) -> None:
    decisions_path = tmp_path / "decisions.json"
    decisions_path.write_text(json.dumps([
        {"item_key": "ABCD0001", "verdict": "include", "reason": "scope ok"},
        {"item_key": "WXYZ9999", "verdict": "EXCLUDE", "flip_fulltext": True},
    ]), encoding="utf-8")
    out = apply.load_decisions(decisions_path)
    assert out == [
        {"item_key": "ABCD0001", "verdict": "include",
         "reason": "scope ok", "flip_fulltext": False},
        {"item_key": "WXYZ9999", "verdict": "exclude",
         "reason": "", "flip_fulltext": True},
    ]


def test_load_decisions_exits_on_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        apply.load_decisions(tmp_path / "nope.json")
    assert "not found" in str(exc.value)


def test_load_decisions_exits_on_invalid_json(tmp_path: Path) -> None:
    decisions_path = tmp_path / "decisions.json"
    decisions_path.write_text("not json at all", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        apply.load_decisions(decisions_path)
    assert "not valid JSON" in str(exc.value)


def test_load_decisions_exits_when_payload_not_list(tmp_path: Path) -> None:
    decisions_path = tmp_path / "decisions.json"
    decisions_path.write_text(json.dumps({"item_key": "X"}), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        apply.load_decisions(decisions_path)
    assert "JSON array" in str(exc.value)


def test_load_decisions_rejects_unknown_verdict(tmp_path: Path) -> None:
    decisions_path = tmp_path / "decisions.json"
    decisions_path.write_text(json.dumps([
        {"item_key": "A", "verdict": "maybe"},
    ]), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        apply.load_decisions(decisions_path)
    msg = str(exc.value)
    assert "verdict" in msg
    assert "maybe" in msg


def test_load_decisions_rejects_missing_item_key(tmp_path: Path) -> None:
    decisions_path = tmp_path / "decisions.json"
    decisions_path.write_text(json.dumps([
        {"verdict": "include"},
    ]), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        apply.load_decisions(decisions_path)
    assert "missing item_key" in str(exc.value)


# ---------------------------------------------------------------------------
# _build_op — tag operation construction
# ---------------------------------------------------------------------------


def test_build_op_include_adds_adjudicated_include_and_strips_qa() -> None:
    op = apply._build_op({
        "item_key": "A", "verdict": "include", "flip_fulltext": False,
    })
    assert op["add"] == ["qa-adjudicated-include"]
    assert op["remove_prefixed"] == ["qa-"]


def test_build_op_with_flip_fulltext_also_replaces_stage_tag() -> None:
    op = apply._build_op({
        "item_key": "A", "verdict": "include", "flip_fulltext": True,
    })
    assert "qa-adjudicated-include" in op["add"]
    assert "fulltext:include" in op["add"]
    assert "qa-" in op["remove_prefixed"]
    assert "fulltext:" in op["remove_prefixed"]


def test_build_op_borderline_does_not_flip_stage_tag() -> None:
    """The flip only makes sense when the verdict picks include / exclude.
    Borderline doesn't choose a fulltext bucket."""
    op = apply._build_op({
        "item_key": "A", "verdict": "borderline", "flip_fulltext": True,
    })
    assert "qa-adjudicated-borderline" in op["add"]
    assert not any(t.startswith("fulltext:") for t in op["add"])


def test_build_op_exclude_with_flip_adds_fulltext_exclude() -> None:
    op = apply._build_op({
        "item_key": "A", "verdict": "exclude", "flip_fulltext": True,
    })
    assert "qa-adjudicated-exclude" in op["add"]
    assert "fulltext:exclude" in op["add"]


# ---------------------------------------------------------------------------
# main() — dry run + Zotero dispatch
# ---------------------------------------------------------------------------


def test_main_dry_run_does_not_call_zotero(tmp_path: Path, monkeypatch, capsys) -> None:
    decisions_path = tmp_path / "decisions.json"
    decisions_path.write_text(json.dumps([
        {"item_key": "DRY1", "verdict": "include"},
    ]), encoding="utf-8")

    # Patch ZoteroClient.from_args to fail loudly if called.
    import zotero_io
    monkeypatch.setattr(
        zotero_io.ZoteroClient, "from_args",
        classmethod(lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("Zotero must NOT be touched on dry-run"),
        )),
    )

    import sys as _sys
    monkeypatch.setattr(_sys, "argv", [
        "apply_qa_adjudications.py",
        "--user",
        "--decisions", str(decisions_path),
        "--dry-run",
    ])
    rc = apply.main()
    assert rc == 0
    captured = capsys.readouterr().out
    assert "[DRY RUN]" in captured
    assert "DRY1" in captured
    assert "qa-adjudicated-include" in captured


def test_main_dispatches_decisions_via_batch_update_tags(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    """Happy path: decisions → batch_update_tags called with the
    right (item_key, op) tuples. No raw pyzotero `add_tags` ever
    runs (which is the bug this script replaces)."""
    decisions_path = tmp_path / "decisions.json"
    decisions_path.write_text(json.dumps([
        {"item_key": "INC1", "verdict": "include", "reason": "in scope",
         "flip_fulltext": True},
        {"item_key": "EXC1", "verdict": "exclude", "reason": "wrong context"},
    ]), encoding="utf-8")
    log_path = tmp_path / "log.csv"

    fake_client = MagicMock()
    fake_client.describe_library.return_value = "user 5591 (personal library)"
    fake_client.batch_update_tags.return_value = {
        "applied": 2, "unchanged": 0, "failed": 0,
    }
    import zotero_io
    monkeypatch.setattr(
        zotero_io.ZoteroClient, "from_args",
        classmethod(lambda *a, **kw: fake_client),
    )

    import sys as _sys
    monkeypatch.setattr(_sys, "argv", [
        "apply_qa_adjudications.py",
        "--user",
        "--decisions", str(decisions_path),
        "--log", str(log_path),
    ])
    rc = apply.main()
    assert rc == 0
    fake_client.batch_update_tags.assert_called_once()
    updates = fake_client.batch_update_tags.call_args.args[0]
    assert len(updates) == 2
    keys = [u[0] for u in updates]
    assert keys == ["INC1", "EXC1"]
    # Verify the include op carries the flip-fulltext additions.
    inc_op = updates[0][1]
    assert "fulltext:include" in inc_op["add"]
    assert "qa-adjudicated-include" in inc_op["add"]
    # The exclude op did NOT request flip_fulltext, so no fulltext: tags.
    exc_op = updates[1][1]
    assert not any(t.startswith("fulltext:") for t in exc_op["add"])
    assert "qa-adjudicated-exclude" in exc_op["add"]
    # Apply log was written.
    assert log_path.is_file()
    content = log_path.read_text(encoding="utf-8")
    assert "INC1" in content
    assert "EXC1" in content


def test_main_returns_nonzero_when_any_failures(
    tmp_path: Path, monkeypatch,
) -> None:
    decisions_path = tmp_path / "decisions.json"
    decisions_path.write_text(json.dumps([
        {"item_key": "FAIL1", "verdict": "include"},
    ]), encoding="utf-8")
    log_path = tmp_path / "log.csv"

    fake_client = MagicMock()
    fake_client.describe_library.return_value = "test"
    fake_client.batch_update_tags.return_value = {
        "applied": 0, "unchanged": 0, "failed": 1,
    }
    import zotero_io
    monkeypatch.setattr(
        zotero_io.ZoteroClient, "from_args",
        classmethod(lambda *a, **kw: fake_client),
    )

    import sys as _sys
    monkeypatch.setattr(_sys, "argv", [
        "apply_qa_adjudications.py",
        "--user",
        "--decisions", str(decisions_path),
        "--log", str(log_path),
    ])
    rc = apply.main()
    assert rc == 2  # non-zero exit on partial failure


def test_main_uses_batch_update_tags_not_raw_pyzotero_add_tags(
    tmp_path: Path, monkeypatch,
) -> None:
    """Pyzotero footgun guard: the bug surfaced in the user's session
    was calling `pyzotero.Zotero.add_tags()` with a stub item dict
    (no `data` key, no version), which silently drops the write.
    `batch_update_tags` reads each item's full payload first, so the
    PATCH carries the right version. This test pins that the script
    never reaches for the raw add_tags path."""
    decisions_path = tmp_path / "decisions.json"
    decisions_path.write_text(json.dumps([
        {"item_key": "K", "verdict": "include"},
    ]), encoding="utf-8")
    log_path = tmp_path / "log.csv"

    fake_client = MagicMock()
    fake_client.describe_library.return_value = "test"
    fake_client.batch_update_tags.return_value = {
        "applied": 1, "unchanged": 0, "failed": 0,
    }
    # If the script reached for the legacy method, this would be the
    # MagicMock's auto-generated attribute; assertion below catches it.
    fake_client.add_tags.side_effect = AssertionError(
        "apply_qa_adjudications.py must never call raw pyzotero.add_tags"
    )
    import zotero_io
    monkeypatch.setattr(
        zotero_io.ZoteroClient, "from_args",
        classmethod(lambda *a, **kw: fake_client),
    )

    import sys as _sys
    monkeypatch.setattr(_sys, "argv", [
        "apply_qa_adjudications.py",
        "--user",
        "--decisions", str(decisions_path),
        "--log", str(log_path),
    ])
    apply.main()
    fake_client.batch_update_tags.assert_called_once()
    fake_client.add_tags.assert_not_called()
