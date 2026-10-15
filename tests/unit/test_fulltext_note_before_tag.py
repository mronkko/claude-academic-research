"""The coding note is written before the include tag (D4).

The tag marks an item done. Written first, a failed note write left an
include that no re-run would touch (it is tagged) and that
`export_coded_includes.py` could not export (it has no note).
"""

from __future__ import annotations

import threading

import fulltext_code as fc
import tag_prefix
from log_schemas import fulltext_screening_fields

NS = tag_prefix.namespace("test-review")
FIELDS = [{"name": "sample", "description": "Who."}]
COLS = fulltext_screening_fields(["sample"])


class _Zot:
    def __init__(self, note_fails: bool = False):
        self.calls: list[str] = []
        self.note_fails = note_fails

    def upsert_child_note(self, key, *, marker, note_html):
        self.calls.append("note")
        if self.note_fails:
            raise RuntimeError("HTTP 503")

    def update_tags(self, key, add=(), remove_prefixed=()):
        self.calls.append("tag")


def _row(decision: str) -> dict:
    return {"item_key": "K1", "title": "T", "decision": decision,
            "exclusion_code": "", "reason": "fits", "sample": "N=10"}


def _apply(zot, row, tmp_path):
    return fc.apply_coded_row(
        zot, row, fields=FIELDS, prompt_version="v",
        output_path=tmp_path / "log.csv", csv_columns=COLS,
        log_lock=threading.Lock(), ns=NS,
    )


def test_an_include_writes_its_note_before_its_tag(tmp_path) -> None:
    zot = _Zot()
    _apply(zot, _row("include"), tmp_path)
    assert zot.calls == ["note", "tag"]


def test_a_failed_note_leaves_the_include_untagged(tmp_path) -> None:
    zot = _Zot(note_fails=True)
    row = _row("include")
    _apply(zot, row, tmp_path)
    assert zot.calls == ["note"]           # no tag: a re-run codes it again
    assert "NOTE WRITE FAILED" in row["reason"]
    assert (tmp_path / "log.csv").exists()  # the CSV row is still kept


def test_an_exclude_is_tagged_without_a_note(tmp_path) -> None:
    zot = _Zot()
    _apply(zot, _row("exclude"), tmp_path)
    assert zot.calls == ["tag"]
