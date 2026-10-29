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
        self.note_html = ""

    def upsert_child_note(self, key, *, marker, note_html):
        self.calls.append("note")
        self.note_html = note_html
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


def test_an_exclude_writes_its_note_before_its_tag(tmp_path) -> None:
    zot = _Zot()
    row = {**_row("exclude"), "exclusion_code": "FE1",
           "reason": "cited only", "sample": "placeholder"}
    _apply(zot, row, tmp_path)
    assert zot.calls == ["note", "tag"]
    assert "FE1" in zot.note_html and "cited only" in zot.note_html
    # Coding fields stay out of an exclude's note, visible part and payload.
    assert "placeholder" not in zot.note_html


def test_a_failed_note_leaves_the_exclude_untagged(tmp_path) -> None:
    zot = _Zot(note_fails=True)
    row = _row("exclude")
    _apply(zot, row, tmp_path)
    assert zot.calls == ["note"]
    assert "NOTE WRITE FAILED" in row["reason"]


def test_rendered_prompt_has_single_braces_around_the_json_example() -> None:
    import fulltext_code

    tpl = "OUT:\n{{\n  {coding_fields_json_placeholder}\n}}\n"
    out = fulltext_code._render_prompt(tpl, [{"name": "method"}])
    assert "{{" not in out and "}}" not in out
    assert out.startswith("OUT:\n{\n")
    assert '"method": "<...>"' in out


def test_draw_sample_is_reproducible_and_pdf_only() -> None:
    import fulltext_code

    items = [{"key": f"K{i:02d}"} for i in range(20)]
    has_pdf = lambda it: int(it["key"][1:]) % 2 == 0  # noqa: E731
    a, info = fulltext_code.draw_sample(items, 4, 7, has_pdf=has_pdf)
    b, _ = fulltext_code.draw_sample(list(reversed(items)), 4, 7, has_pdf=has_pdf)
    assert [i["key"] for i in a] == [i["key"] for i in b]      # order-independent
    assert info["pool_size"] == 10 and info["seed"] == 7
    assert all(has_pdf(i) for i in a) and len(a) == 4
    assert info["keys"] == [i["key"] for i in a]
