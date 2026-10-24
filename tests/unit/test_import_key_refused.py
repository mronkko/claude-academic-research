"""import_to_zotero stops when Zotero will not grant a local write key.

Every write goes to Zotero Desktop's local API. A missing or revoked key
makes `zotero_io` open Zotero's consent dialog; Deny, a one-time "Allow",
or the endpoint's rate limit raise `zotero_io.LocalKeyRefused`. That is
not a transient failure to count and continue past — every later write
would hit the same refusal — so it ends the import with the message
saying what the user has to do, and no traceback.
"""

from __future__ import annotations

import csv
import sys
from unittest.mock import MagicMock

import import_to_zotero as imp
import pytest
import zotero_io

DENIED = ("Zotero write access was denied in its dialog. Run the command "
          "again and choose 'Always Allow'.")


# ---------------------------------------------------------------------------
# _exit_if_key_refused
# ---------------------------------------------------------------------------


def test_a_refused_key_exits_with_its_message() -> None:
    with pytest.raises(SystemExit) as info:
        imp._exit_if_key_refused(zotero_io.LocalKeyRefused(DENIED))
    message = str(info.value.code)
    assert message.startswith("ERROR: Zotero write access was denied")
    assert "Always Allow" in message
    assert message.endswith("Nothing was imported.")


@pytest.mark.parametrize("exc", [RuntimeError("HTTP 503"), ValueError("bad")])
def test_other_errors_do_not_exit(exc: BaseException) -> None:
    assert imp._exit_if_key_refused(exc) is None


# ---------------------------------------------------------------------------
# _create_new_items
# ---------------------------------------------------------------------------


class _CreateZot:
    def __init__(self, script):
        self.script = list(script)
        self.batches = 0

    def create_items(self, batch):
        self.batches += 1
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return {"success": {str(i): f"K{self.batches}{i}" for i in range(len(batch))},
                "failed": {}}


@pytest.fixture
def _one_per_batch(monkeypatch):
    monkeypatch.setattr(imp, "BATCH_SIZE", 1)
    monkeypatch.setattr(imp.time, "sleep", lambda _s: None)


def _items(n: int) -> list[dict]:
    return [{"itemType": "journalArticle", "title": str(i)} for i in range(n)]


@pytest.mark.usefixtures("_one_per_batch")
def test_a_refused_key_ends_the_create_loop() -> None:
    """Not counted as a failed batch: the next batch would only open the
    dialog again (or be refused without one)."""
    zot = _CreateZot(["ok", zotero_io.LocalKeyRefused(DENIED), "ok"])
    created_keys: list[str] = []
    with pytest.raises(zotero_io.LocalKeyRefused):
        imp._create_new_items(_items(3), zot, created_keys)
    assert zot.batches == 2, "no batch may be sent after the refusal"
    assert created_keys == ["K10"], "the keys already created stay recorded"


@pytest.mark.usefixtures("_one_per_batch")
def test_an_ordinary_batch_failure_is_counted_and_the_rest_run() -> None:
    zot = _CreateZot(["ok", RuntimeError("HTTP 503"), "ok"])
    created, failed, keys = imp._create_new_items(_items(3), zot)
    assert (created, failed) == (2, 1)
    assert zot.batches == 3
    assert keys == ["K10", "K30"]


# ---------------------------------------------------------------------------
# _fetch_existing_items — a read needs no key, so its failures degrade
# ---------------------------------------------------------------------------


def test_other_failures_still_degrade_in_a_dry_run() -> None:
    zot = MagicMock()
    zot.journal_articles.side_effect = RuntimeError("Zotero is not running")
    try:
        assert imp._fetch_existing_items(zot, True) == ({}, {})
        assert imp._existing_lookup_failed is True
    finally:
        imp._existing_lookup_failed = False


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


@pytest.fixture
def run_main(tmp_path, monkeypatch):
    def _run(zot, *extra) -> tuple[object, dict]:
        csv_path = tmp_path / "in.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["doi", "title"])
            w.writeheader()
            w.writerow({"doi": "10.1016/j.example.2020.01.001", "title": "A"})
        seen: dict = {}

        def from_args(cls, args, **kw):
            seen.update(kw)
            return zot

        monkeypatch.setattr(imp.zotero_io.ZoteroClient, "from_args",
                            classmethod(from_args))
        monkeypatch.setattr(imp.screening_common, "load_tag_namespace",
                            lambda *a, **k: "rev")
        # Stop the metadata fill from reaching Crossref; these tests are
        # about the collection step, which happens before it.
        monkeypatch.setattr(
            imp, "build_item",
            lambda row, coll, **kw: ({"itemType": "journalArticle",
                                      "title": row["title"], "DOI": row["doi"]},
                                     imp.BUILD_SOURCE))
        monkeypatch.setattr(sys, "argv", ["import_to_zotero.py", "--group", "1",
                                          "--input", str(csv_path), *extra])
        try:
            return imp.main(), seen
        except SystemExit as exc:
            return exc, seen
    return _run


def test_a_refused_key_while_creating_the_collection_ends_the_import(run_main) -> None:
    """Creating the collection is the import's first write, so it is where
    a refused key normally surfaces."""
    zot = MagicMock()
    zot.find_collection.side_effect = zotero_io.LocalKeyRefused(DENIED)
    result, _seen = run_main(zot, "--collection", "AI SLR")
    assert isinstance(result, SystemExit)
    assert "Always Allow" in str(result.code)
    assert "Nothing was imported." in str(result.code)
    zot.journal_articles.assert_not_called()
    zot.create_items.assert_not_called()


def test_other_collection_errors_are_not_swallowed(run_main) -> None:
    zot = MagicMock()
    zot.find_collection.side_effect = RuntimeError("HTTP 500")
    with pytest.raises(RuntimeError, match="HTTP 500"):
        run_main(zot, "--collection", "AI SLR")


def test_the_client_is_built_without_a_key_argument(run_main) -> None:
    """The local key is the client's own business (config or a fresh
    grant); the import passes nothing that could override it."""
    zot = MagicMock()
    zot.journal_articles.return_value = []
    try:
        _result, seen = run_main(zot, "--dry-run")
    finally:
        imp._existing_lookup_failed = False
    assert seen == {}


def test_a_refused_key_while_patching_ends_the_import(run_main) -> None:
    """With no collection to create, the first write may be a patch of an
    item already in the library. A refusal there used to be counted as one
    failed patch, after which every later write got a plain 401."""
    zot = MagicMock()
    zot.journal_articles.return_value = [{
        "key": "OLD1", "version": 3,
        "data": {"key": "OLD1", "DOI": "10.1016/j.example.2020.01.001",
                 "title": "A", "abstractNote": "", "tags": []},
    }]
    zot.find_collection.return_value = ("ABCD1234", "key")
    zot.update_fields.side_effect = zotero_io.LocalKeyRefused(DENIED)
    result, _seen = run_main(zot, "--collection", "ABCD1234")
    assert isinstance(result, SystemExit)
    assert "Always Allow" in str(result.code)
    assert "No new items were created" in str(result.code)
    zot.create_items.assert_not_called()


def test_a_refused_key_while_creating_ends_the_import_cleanly(run_main) -> None:
    zot = MagicMock()
    zot.journal_articles.return_value = []
    zot.create_items.side_effect = zotero_io.LocalKeyRefused(DENIED)
    result, _seen = run_main(zot)
    assert isinstance(result, SystemExit)
    assert "Always Allow" in str(result.code)
    assert "0 item(s) were created before it" in str(result.code)
