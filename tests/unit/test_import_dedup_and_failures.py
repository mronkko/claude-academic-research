"""import_to_zotero: dedup never merges different DOIs; failures are loud.

B7  Title+first-author matching was applied even when both sides carried
    a DOI and the DOIs differed (a paper and its erratum, a conference and
    a journal version), and an empty title still formed a key (`"|smith"`,
    `"|"`), collapsing unrelated untitled rows into one item.
B8  `POST /items` was replayed on 5xx/read errors — a batch applied
    before the server failed to answer was created twice. Creates now go
    through `ZoteroClient.create_items` and are never resent; a failed
    batch is counted and the rest still run.
B9  Failed creates still exited 0; an exception mid-run lost
    `--created-keys-out`; two rows resolving to one existing item sent a
    second PATCH with a superseded version, got 412, and aborted.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import import_to_zotero as imp
import pytest


def test_empty_title_forms_no_key() -> None:
    assert imp._title_author_key("", "Smith, J") == ""
    assert imp._title_author_key("   ", "") == ""
    assert imp._title_author_key("A Paper", "Smith, J") == "a paper|smith"


# ---------------------------------------------------------------------------
# Driving main() with an in-memory Zotero
# ---------------------------------------------------------------------------

_FIELDS = ["doi", "title", "authors", "year", "source", "issn", "abstract"]


class _Zot:
    """In-memory Zotero; answers each `create_items` batch from `script`
    (`"ok"`, an HTTP status to fail with, or an exception to raise)."""

    def __init__(self, existing: list[dict], script=()):
        self.existing = existing
        self.patches: list[dict] = []
        self.script = list(script)
        self.posts: list[list[dict]] = []

    def describe_library(self):
        return "group 1"

    def journal_articles(self):
        return self.existing

    def create_items(self, batch):
        self.posts.append(batch)
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        if step != "ok":
            raise RuntimeError(f"HTTP {step}")
        return {"success": {str(i): f"K{len(self.posts)}{i}"
                            for i in range(len(batch))},
                "failed": {}}

    def update_fields(self, item_key, fields):
        # Like ZoteroClient.update_fields: writes at the item's current
        # version, so two patches to one item cannot 412 each other.
        for it in self.existing:
            if it["key"] == item_key:
                it["version"] += 1
        self.patches.append({"key": item_key, **fields})
        return True


@pytest.fixture
def run_import(tmp_path, monkeypatch):
    def _run(rows, *, existing=(), script=("ok",) * 10, extra=()):
        csv_path = tmp_path / "in.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=_FIELDS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in _FIELDS})
        zot = _Zot([dict(e) for e in existing], script)
        monkeypatch.setattr(imp.zotero_io.ZoteroClient, "from_args",
                            classmethod(lambda cls, *a, **k: zot))
        monkeypatch.setattr(imp.screening_common, "load_tag_namespace",
                            lambda *a, **k: "rev")
        monkeypatch.setattr(imp.time, "sleep", lambda _s: None)
        monkeypatch.setattr(
            imp, "build_item",
            lambda row, coll, **kw: ({"itemType": "journalArticle",
                                      "title": row["title"],
                                      "DOI": row["doi"],
                                      "abstractNote": row.get("abstract", "")},
                                     imp.BUILD_SOURCE),
        )
        monkeypatch.setattr(sys, "argv", [
            "import_to_zotero.py", "--input", str(csv_path), *extra,
        ])
        rc = imp.main()
        return rc, zot, zot
    return _run


def _created_titles(zot) -> list[str]:
    return [it["DOI"] for batch in zot.posts for it in batch]


def _lib_item(key, doi, title, last="Smith", version=1):
    return {"key": key, "version": version,
            "data": {"key": key, "DOI": doi, "title": title,
                     "creators": [{"lastName": last}], "collections": []}}


def test_rows_with_different_dois_are_not_merged_within_the_batch(run_import) -> None:
    rc, _zot, session = run_import([
        {"doi": "10.1000/paper", "title": "Same Title", "authors": "Smith, J"},
        {"doi": "10.1000/erratum", "title": "Same Title", "authors": "Smith, J"},
    ])
    assert rc == 0
    assert sorted(_created_titles(session)) == ["10.1000/erratum", "10.1000/paper"]


def test_a_row_is_not_merged_into_a_library_item_with_another_doi(run_import) -> None:
    rc, zot, session = run_import(
        [{"doi": "10.1000/erratum", "title": "Same Title", "authors": "Smith, J"}],
        existing=[_lib_item("EXIST001", "10.1000/paper", "Same Title")],
    )
    assert rc == 0
    assert _created_titles(session) == ["10.1000/erratum"]
    assert zot.patches == []


def test_a_doi_less_row_still_merges_by_title(run_import) -> None:
    rc, _zot, session = run_import([
        {"doi": "10.1000/paper", "title": "Same Title", "authors": "Smith, J"},
        {"doi": "", "title": "Same Title", "authors": "Smith, J"},
    ])
    assert rc == 0
    assert _created_titles(session) == ["10.1000/paper"]


def test_untitled_rows_do_not_collapse_into_one(run_import) -> None:
    rc, _zot, session = run_import([
        {"doi": "", "title": "", "authors": "Smith, J"},
        {"doi": "", "title": "", "authors": "Smith, J"},
    ])
    assert rc == 0
    assert len(_created_titles(session)) == 2


def test_failed_creates_exit_non_zero(run_import, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(imp, "BATCH_SIZE", 1)
    keys_out = tmp_path / "keys.txt"
    rc, _zot, session = run_import(
        [{"doi": "10.1000/a", "title": "A"}, {"doi": "10.1000/b", "title": "B"},
         {"doi": "10.1000/c", "title": "C"}],
        script=["ok", 503, "ok"],
        extra=["--created-keys-out", str(keys_out)],
    )
    assert rc == 1
    assert len(session.posts) == 3, "a failed batch must not stop the rest"
    assert len(keys_out.read_text().split()) == 2


def test_created_keys_survive_a_mid_run_exception(run_import, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(imp, "BATCH_SIZE", 1)
    keys_out = tmp_path / "keys.txt"
    with pytest.raises(KeyboardInterrupt):
        run_import(
            [{"doi": "10.1000/a", "title": "A"}, {"doi": "10.1000/b", "title": "B"}],
            script=["ok", KeyboardInterrupt()],
            extra=["--created-keys-out", str(keys_out)],
        )
    assert Path(keys_out).read_text().split() == ["K10"]


def test_two_rows_hitting_one_existing_item_patch_it_once(run_import) -> None:
    rc, zot, _session = run_import(
        [{"doi": "10.1000/paper", "title": "T", "abstract": "First abstract."},
         {"doi": "", "title": "Paper Title", "authors": "Smith, J",
          "abstract": "An abstract."}],
        existing=[_lib_item("EXIST001", "10.1000/paper", "Paper Title")],
    )
    assert rc == 0
    assert len(zot.patches) == 1
    assert zot.patches[0]["abstractNote"] == "First abstract."
