"""New-run pre-flight against a dirty Zotero group.

Run 20260813T184419Z lost a whole pipeline to this. Its group still held
the previous run's 8 items, `import_to_zotero.py` deduplicates by DOI, so
the new collection got zero new items and inherited every stale
`abstract:*` / `fulltext:include` tag. `abstract_screen.py` then found
nothing left to screen and wrote no log at all, and the operator saw only
five `verify` failures — a missing `abstract_screening.csv`, two count
mismatches — none of which mention the earlier run.

The cost of missing it is a full run (search through code, several
minutes and real API spend); the cost of the check is one library read
before anything is spent.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def mini_slr():
    """Load `scripts/dev/mini_slr.py` without executing its CLI."""
    path = REPO_ROOT / "scripts" / "dev" / "mini_slr.py"
    name = "mini_slr_preflight_under_test"
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


def _item(key: str, *tags: str) -> dict:
    return {"key": key, "data": {"tags": [{"tag": t} for t in tags]}}


class _Library:
    def __init__(self, items: list[dict]) -> None:
        self._items = items

    def journal_articles(self) -> list[dict]:
        return self._items


def _install_library(mini_slr, monkeypatch, items: list[dict],
                     seen: list[dict] | None = None) -> None:
    def _from_config(*, group_id, prefer_local=True):
        if seen is not None:
            seen.append({"group_id": group_id, "prefer_local": prefer_local})
        return _Library(items)

    monkeypatch.setattr(mini_slr.zotero_io.ZoteroClient, "from_config",
                        staticmethod(_from_config))


def _write_run(root: Path, run_id: str, *, created: list[str],
               torn_down: bool = False) -> None:
    d = root / run_id
    d.mkdir(parents=True, exist_ok=True)
    state = {"run_id": run_id, "created_item_keys": created}
    if torn_down:
        state["torn_down"] = True
    (d / ".mini_slr_state.json").write_text(json.dumps(state), encoding="utf-8")


def _ctx(mini_slr, tmp_path):
    return mini_slr.Ctx(run_dir=tmp_path, group_id="6637302", state={})


# --- clean groups start ------------------------------------------------

def test_empty_group_is_clean(mini_slr, tmp_path, monkeypatch) -> None:
    _install_library(mini_slr, monkeypatch, [])
    mini_slr._preflight_clean_group(_ctx(mini_slr, tmp_path))


def test_untagged_items_are_clean(mini_slr, tmp_path, monkeypatch) -> None:
    """Items alone are harmless — the pipeline re-uses them happily. It is
    the stage tags that make screening skip everything."""
    _install_library(mini_slr, monkeypatch, [_item("AAA"), _item("BBB")])
    mini_slr._preflight_clean_group(_ctx(mini_slr, tmp_path))


def test_unrelated_tags_do_not_trip_it(mini_slr, tmp_path, monkeypatch) -> None:
    _install_library(mini_slr, monkeypatch,
                     [_item("AAA", "toread", "fulltext-ish")])
    mini_slr._preflight_clean_group(_ctx(mini_slr, tmp_path))


# --- the regression ----------------------------------------------------

def test_dirty_group_names_the_run_to_tear_down(mini_slr, tmp_path,
                                                monkeypatch) -> None:
    """The whole point: turn five misleading verify failures into one
    message carrying the command that fixes it."""
    _install_library(mini_slr, monkeypatch, [
        _item("UVTAUBKF", "abstract:include", "fulltext:include"),
        _item("X9EBB68U", "abstract:exclude"),
        _item("CLEAN001"),
    ])
    monkeypatch.setattr(mini_slr, "OUTPUT_E2E_ROOT", tmp_path / "e2e")
    _write_run(tmp_path / "e2e", "20260813T182322Z",
               created=["UVTAUBKF", "X9EBB68U"])

    with pytest.raises(SystemExit) as exc:
        mini_slr._preflight_clean_group(_ctx(mini_slr, tmp_path))

    msg = str(exc.value)
    assert "2 item(s)" in msg, "counts only the tagged items, not the clean one"
    assert "--stage teardown --run-id 20260813T182322Z" in msg, (
        "must hand over the exact command, not just describe the problem"
    )


def test_torn_down_runs_are_not_offered(mini_slr, tmp_path,
                                        monkeypatch) -> None:
    """A run already torn down cannot be torn down again; suggesting it
    would send the operator in a circle."""
    _install_library(mini_slr, monkeypatch,
                     [_item("UVTAUBKF", "abstract:include")])
    monkeypatch.setattr(mini_slr, "OUTPUT_E2E_ROOT", tmp_path / "e2e")
    _write_run(tmp_path / "e2e", "20260813T182322Z",
               created=["UVTAUBKF"], torn_down=True)

    with pytest.raises(SystemExit) as exc:
        mini_slr._preflight_clean_group(_ctx(mini_slr, tmp_path))

    msg = str(exc.value)
    assert "--run-id 20260813T182322Z" not in msg, (
        "offering to tear down an already-torn-down run sends the "
        "operator in a circle"
    )
    assert "Delete them in Zotero" in msg


def test_unclaimed_items_say_teardown_will_not_help(mini_slr, tmp_path,
                                                    monkeypatch) -> None:
    _install_library(mini_slr, monkeypatch,
                     [_item("MYSTERY1", "fulltext:include")])
    monkeypatch.setattr(mini_slr, "OUTPUT_E2E_ROOT", tmp_path / "e2e")
    (tmp_path / "e2e").mkdir()

    with pytest.raises(SystemExit) as exc:
        mini_slr._preflight_clean_group(_ctx(mini_slr, tmp_path))

    assert "will not remove them" in str(exc.value)


# --- fail open ---------------------------------------------------------

def test_unreadable_group_does_not_block_the_run(mini_slr, tmp_path,
                                                 monkeypatch, capsys) -> None:
    """A Zotero blip is not evidence of contamination. Blocking on it
    would cost more runs than the race it guards."""
    def _boom(**_kw):
        raise RuntimeError("Zotero API 503")

    monkeypatch.setattr(mini_slr.zotero_io.ZoteroClient, "from_config",
                        staticmethod(_boom))
    mini_slr._preflight_clean_group(_ctx(mini_slr, tmp_path))

    assert "WARNING" in capsys.readouterr().out


def test_it_reads_the_cloud_not_the_local_library(mini_slr, tmp_path,
                                                  monkeypatch) -> None:
    """Teardown deletes through the Web API, so the cloud is clean the
    moment it returns while Zotero Desktop may still show the items. A
    local read would block a clean run and point at a run that no longer
    exists — the one failure this check must never produce."""
    seen: list[dict] = []
    _install_library(mini_slr, monkeypatch, [], seen=seen)

    mini_slr._preflight_clean_group(_ctx(mini_slr, tmp_path))

    assert seen and seen[0]["prefer_local"] is False
    assert seen[0]["group_id"] == "6637302"
