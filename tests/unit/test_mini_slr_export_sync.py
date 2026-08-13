"""Export-stage sync wait for the live mini-SLR.

The live run at 20260813T182322Z coded one item `include` and wrote
`coded_papers.csv` with 0 rows. Nothing was wrong with the coding or the
tagging: `fulltext_code.py` tags through the Zotero Web API, but
`export_coded_includes.py` selects on those tags through a client that
prefers the local Zotero server, so exporting straight after coding read
the library before the tag arrived. Re-running the identical export
afterwards returned the missing row, which is what identified this as a
race rather than a lost write.

`stage_code` already carries the same wait for `abstract:*` tags; this
guards the `fulltext:*` half, which was missing.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def mini_slr():
    """Load `scripts/dev/mini_slr.py` without executing its CLI.

    It lives outside the package tree and carries PEP 723 deps, so it is
    loaded by path rather than imported.
    """
    path = REPO_ROOT / "scripts" / "dev" / "mini_slr.py"
    name = "mini_slr_export_under_test"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # Register before exec: the module defines a @dataclass, and
    # dataclasses resolves annotations via `sys.modules[cls.__module__]`.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        del sys.modules[name]
        raise
    return module


def _write_log(run_dir: Path, rows: list[tuple[str, str]]) -> None:
    """Write a minimal fulltext log; only item_key and decision are read."""
    log = run_dir / "screening" / "fulltext_screening.csv"
    log.parent.mkdir(parents=True, exist_ok=True)
    lines = ["item_key,decision"] + [f"{k},{d}" for k, d in rows]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _ctx(mini_slr, run_dir: Path):
    return mini_slr.Ctx(run_dir=run_dir, group_id="1",
                        state={"collection_key": "COLL1"})


class _FakeClient:
    """Yields one library snapshot per call, last snapshot repeating."""

    def __init__(self, snapshots: list[list[str]]) -> None:
        self.snapshots = snapshots
        self.calls = 0

    def collection_items(self, _key: str, item_type: str = "") -> list[dict]:
        idx = min(self.calls, len(self.snapshots) - 1)
        self.calls += 1
        return [
            {"key": k, "data": {"tags": [{"tag": "fulltext:include"}]}}
            for k in self.snapshots[idx]
        ]


# --- expected-count arithmetic ----------------------------------------

def test_only_tagged_decisions_are_expected(mini_slr, tmp_path) -> None:
    """error / no_pdf stay untagged so a re-run picks them up, so waiting
    on them would hang until the timeout on every run with a missing PDF."""
    _write_log(tmp_path, [("A", "include"), ("B", "exclude"),
                          ("C", "error"), ("D", "no_pdf")])
    assert mini_slr._expected_fulltext_tags(_ctx(mini_slr, tmp_path)) == 2


def test_last_row_per_item_wins(mini_slr, tmp_path) -> None:
    """A re-coded item appends a second row; the stale one must not count."""
    _write_log(tmp_path, [("A", "error"), ("A", "include")])
    assert mini_slr._expected_fulltext_tags(_ctx(mini_slr, tmp_path)) == 1


def test_missing_log_expects_nothing(mini_slr, tmp_path) -> None:
    assert mini_slr._expected_fulltext_tags(_ctx(mini_slr, tmp_path)) == 0


# --- the regression ---------------------------------------------------

def test_export_waits_for_tags_to_sync(mini_slr, tmp_path, monkeypatch) -> None:
    """The live failure: export ran while Zotero still showed 0 tagged
    items and silently wrote an empty CSV."""
    _write_log(tmp_path, [("A", "include")])
    client = _FakeClient([[], [], ["A"]])
    monkeypatch.setattr(mini_slr, "_local_client", lambda _g: client)
    monkeypatch.setattr(mini_slr, "SYNC_INTERVAL_S", 0)

    exported_when: list[int] = []
    monkeypatch.setattr(
        mini_slr, "_uv_run",
        lambda *a, **k: exported_when.append(client.calls),
    )

    mini_slr.stage_export(_ctx(mini_slr, tmp_path))

    assert exported_when == [3], (
        "export must run only after the tag is visible locally, not before"
    )


def test_export_skips_wait_when_nothing_tagged(mini_slr, tmp_path,
                                               monkeypatch) -> None:
    """A run where every item errored has no tags coming; it must still
    export (a header-only CSV) rather than block for SYNC_TIMEOUT_S."""
    _write_log(tmp_path, [("A", "error")])

    def _no_client(_g):
        raise AssertionError("must not poll Zotero when no tags are expected")

    monkeypatch.setattr(mini_slr, "_local_client", _no_client)
    ran = []
    monkeypatch.setattr(mini_slr, "_uv_run", lambda *a, **k: ran.append(1))

    mini_slr.stage_export(_ctx(mini_slr, tmp_path))

    assert ran == [1]


def test_export_fails_loudly_when_sync_never_lands(mini_slr, tmp_path,
                                                   monkeypatch) -> None:
    """Timing out must not fall through to an export that writes a short
    CSV — that is the silent-truncation bug this wait exists to stop."""
    _write_log(tmp_path, [("A", "include")])
    monkeypatch.setattr(mini_slr, "_local_client", lambda _g: _FakeClient([[]]))
    monkeypatch.setattr(mini_slr, "SYNC_INTERVAL_S", 0)
    monkeypatch.setattr(mini_slr, "SYNC_TIMEOUT_S", 0)
    monkeypatch.setattr(
        mini_slr, "_uv_run",
        lambda *a, **k: pytest.fail("must not export after a sync timeout"),
    )

    with pytest.raises(SystemExit) as exc:
        mini_slr.stage_export(_ctx(mini_slr, tmp_path))

    assert "fulltext:* tags timed out" in str(exc.value)
