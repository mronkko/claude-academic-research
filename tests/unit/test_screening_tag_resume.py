"""Unit tests for the tag-based resume helpers in `abstract_screen.py`
and `fulltext_code.py`. These ensure the Zotero-as-ground-truth resume
pattern correctly identifies already-screened items from their tags."""

from __future__ import annotations

import abstract_screen
import fulltext_code


def _item(key: str, tags: list[str]) -> dict:
    """Minimal Zotero item shape used by `_already_tagged`."""
    return {
        "key": key,
        "data": {"tags": [{"tag": t} for t in tags]},
    }


# ---------------------------------------------------------------------------
# abstract_screen._already_tagged — any `abstract:*` tag counts as done.
# ---------------------------------------------------------------------------


def test_abstract_already_tagged_detects_all_three_stage_tags() -> None:
    items = [
        _item("A", ["abstract:include"]),
        _item("B", ["abstract:exclude"]),
        _item("C", ["abstract:borderline"]),
        _item("D", []),
    ]
    assert abstract_screen._already_tagged(items) == {"A", "B", "C"}


def test_abstract_already_tagged_ignores_non_stage_tags() -> None:
    items = [
        _item("A", ["predatory:flag"]),
        _item("B", ["fulltext:include", "manually-flagged"]),
        _item("C", ["abstract:include", "fulltext:exclude"]),
    ]
    # Only C has an abstract:* tag; A and B shouldn't be skipped at the
    # abstract stage even though B has a fulltext tag.
    assert abstract_screen._already_tagged(items) == {"C"}


def test_abstract_already_tagged_handles_empty_items() -> None:
    assert abstract_screen._already_tagged([]) == set()


# ---------------------------------------------------------------------------
# fulltext_code._already_tagged — only `fulltext:include` / `fulltext:exclude`.
# ---------------------------------------------------------------------------


def test_fulltext_already_tagged_accepts_include_and_exclude() -> None:
    items = [
        _item("A", ["fulltext:include"]),
        _item("B", ["fulltext:exclude"]),
        _item("C", []),
    ]
    assert fulltext_code._already_tagged(items) == {"A", "B"}


def test_fulltext_already_tagged_rejects_non_stage_tags() -> None:
    items = [
        # An abstract tag alone doesn't count — the item is pending
        # full-text coding.
        _item("A", ["abstract:include"]),
        _item("B", ["predatory:flag", "qa-flag"]),
        # Borderline / error-state items are not part of fulltext's
        # stage-tag vocabulary.
        _item("C", ["fulltext:borderline"]),
    ]
    assert fulltext_code._already_tagged(items) == set()


def test_fulltext_already_tagged_handles_mixed_states() -> None:
    items = [
        _item("A", ["abstract:include", "fulltext:include"]),
        _item("B", ["abstract:include"]),
        _item("C", ["abstract:borderline", "fulltext:exclude"]),
    ]
    assert fulltext_code._already_tagged(items) == {"A", "C"}


# ---------------------------------------------------------------------------
# fulltext_code._build_slr_coding_note_html — produces the HTML body that
# upsert_child_note writes. Must start with the marker for round-trip.
# ---------------------------------------------------------------------------


def test_note_html_starts_with_marker_for_roundtrip() -> None:
    """Critical contract: if the note body doesn't start with the
    SLR_CODING_NOTE_MARKER, upsert_child_note can't find and update it
    on the next run and we'll leak duplicate notes."""
    row = {
        "item_key": "X1",
        "decision": "include",
        "model": "claude-sonnet-4-6",
        "timestamp": "2026-04-23T10:00:00+00:00",
        "reason": "",
        "key_findings": "Motivation predicts growth.",
    }
    fields = [{"name": "key_findings"}]
    html = fulltext_code._build_slr_coding_note_html(row, fields, "v1")
    assert html.startswith(fulltext_code.SLR_CODING_NOTE_MARKER)


def test_note_html_includes_decision_and_coding_fields() -> None:
    row = {
        "decision": "include",
        "model": "claude-sonnet-4-6",
        "timestamp": "2026-04-23T10:00:00+00:00",
        "reason": "Meets criteria 1-3.",
        "key_findings": "Growth motivation correlates with firm size.",
        "sample": "245 UK SMEs.",
        "method": "",  # Empty fields are skipped.
    }
    fields = [
        {"name": "key_findings"},
        {"name": "sample"},
        {"name": "method"},
    ]
    html = fulltext_code._build_slr_coding_note_html(row, fields, "v1-2026")

    assert "Decision:</strong> include" in html
    assert "Reason:</strong> Meets criteria 1-3." in html
    assert "<h2>Key Findings</h2>" in html
    assert "Growth motivation correlates" in html
    assert "<h2>Sample</h2>" in html
    assert "245 UK SMEs." in html
    # Empty method field should not appear.
    assert "<h2>Method</h2>" not in html
    # Provenance footer.
    assert "model=claude-sonnet-4-6" in html
    assert "prompt_version=v1-2026" in html


def test_note_html_escapes_html_in_coded_values() -> None:
    """LLM output may contain angle brackets, ampersands, etc. The
    visible HTML portion of the note must escape them so Zotero's
    renderer doesn't execute unintended markup. The trailing
    SLR_CODING_DATA comment carries the raw values in JSON, which is
    safe because HTML comments aren't rendered."""
    row = {
        "decision": "include",
        "model": "m",
        "timestamp": "t",
        "reason": "",
        "key_findings": "A < B & C > D; the <script> tag",
    }
    fields = [{"name": "key_findings"}]
    html = fulltext_code._build_slr_coding_note_html(row, fields, "v1")

    # Split at the data comment boundary — the VISIBLE HTML portion
    # must have no raw markup; the JSON comment may contain anything.
    visible, _, data_block = html.partition("<!--")
    assert "&lt;script&gt;" in visible
    assert "A &lt; B &amp; C &gt; D" in visible
    assert "<script>" not in visible
    # The data block is a comment, so its contents are hidden from the
    # renderer; confirm it's there and starts with SLR_CODING_DATA.
    assert data_block.lstrip().startswith("SLR_CODING_DATA:")
