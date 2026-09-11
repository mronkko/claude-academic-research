"""Unit tests for the tag-based resume helpers in `abstract_screen.py`
and `fulltext_code.py`. These ensure the Zotero-as-ground-truth resume
pattern correctly identifies already-screened items from their tags."""

from __future__ import annotations

import abstract_screen
import fulltext_code
import tag_prefix
import zotero_io

#: This review's namespace. Every stage tag is read and written under it,
#: so a second review's tags on the same item must not register as ours —
#: that is what `OTHER_NS` below exists to prove.
NS = tag_prefix.namespace("test-review")
OTHER_NS = tag_prefix.namespace("other-review")

ABSTRACT = tag_prefix.family(NS, "abstract")
FULLTEXT = tag_prefix.family(NS, "fulltext")


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
        _item("A", [f"{ABSTRACT}include"]),
        _item("B", [f"{ABSTRACT}exclude"]),
        _item("C", [f"{ABSTRACT}borderline"]),
        _item("D", []),
    ]
    assert abstract_screen._already_tagged(items, ABSTRACT) == {"A", "B", "C"}


def test_abstract_already_tagged_ignores_non_stage_tags() -> None:
    items = [
        _item("A", ["predatory:flag"]),
        _item("B", [f"{FULLTEXT}include", "manually-flagged"]),
        _item("C", [f"{ABSTRACT}include", f"{FULLTEXT}exclude"]),
    ]
    # Only C has an abstract:* tag; A and B shouldn't be skipped at the
    # abstract stage even though B has a fulltext tag.
    assert abstract_screen._already_tagged(items, ABSTRACT) == {"C"}


def test_abstract_already_tagged_handles_empty_items() -> None:
    assert abstract_screen._already_tagged([], ABSTRACT) == set()


# ---------------------------------------------------------------------------
# fulltext_code._already_tagged — only `fulltext:include` / `fulltext:exclude`.
# ---------------------------------------------------------------------------


def test_fulltext_already_tagged_accepts_include_and_exclude() -> None:
    items = [
        _item("A", [f"{FULLTEXT}include"]),
        _item("B", [f"{FULLTEXT}exclude"]),
        _item("C", []),
    ]
    assert fulltext_code._already_tagged(items, FULLTEXT) == {"A", "B"}


def test_fulltext_already_tagged_rejects_non_stage_tags() -> None:
    items = [
        # An abstract tag alone doesn't count — the item is pending
        # full-text coding.
        _item("A", [f"{ABSTRACT}include"]),
        _item("B", ["predatory:flag", "qa-flag"]),
        # Borderline / error-state items are not part of fulltext's
        # stage-tag vocabulary.
        _item("C", [f"{FULLTEXT}borderline"]),
    ]
    assert fulltext_code._already_tagged(items, FULLTEXT) == set()


def test_fulltext_already_tagged_handles_mixed_states() -> None:
    items = [
        _item("A", [f"{ABSTRACT}include", f"{FULLTEXT}include"]),
        _item("B", [f"{ABSTRACT}include"]),
        _item("C", [f"{ABSTRACT}borderline", f"{FULLTEXT}exclude"]),
    ]
    assert fulltext_code._already_tagged(items, FULLTEXT) == {"A", "C"}


# ---------------------------------------------------------------------------
# fulltext_code._build_slr_coding_note_html — produces the HTML body that
# upsert_child_note writes. Must start with the marker for round-trip.
# ---------------------------------------------------------------------------


def test_note_html_starts_with_marker_for_roundtrip() -> None:
    """Critical contract: if the note body doesn't start with this
    review's marker, upsert_child_note can't find and update it on the
    next run and we'll leak duplicate notes."""
    row = {
        "item_key": "X1",
        "decision": "include",
        "model": "claude-sonnet-4-6",
        "timestamp": "2026-04-23T10:00:00+00:00",
        "reason": "",
        "key_findings": "Motivation predicts growth.",
    }
    fields = [{"name": "key_findings"}]
    html = fulltext_code._build_slr_coding_note_html(row, fields, "v1", NS)
    assert html.startswith(zotero_io.slr_coding_marker(NS))


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
    html = fulltext_code._build_slr_coding_note_html(row, fields, "v1-2026", NS)

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
    html = fulltext_code._build_slr_coding_note_html(row, fields, "v1", NS)

    # Split at the data comment boundary — the VISIBLE HTML portion
    # must have no raw markup; the JSON comment may contain anything.
    visible, _, data_block = html.partition("<!--")
    assert "&lt;script&gt;" in visible
    assert "A &lt; B &amp; C &gt; D" in visible
    assert "<script>" not in visible
    # The data block is a comment, so its contents are hidden from the
    # renderer; confirm it's there and starts with SLR_CODING_DATA.
    assert data_block.lstrip().startswith("SLR_CODING_DATA:")


# ---------------------------------------------------------------------------
# Cross-review isolation — the reason the namespace exists.
#
# One Zotero library commonly holds several reviews. Before prefixes, a
# second review's `abstract:include` on a shared item made this review skip
# an item it had never screened, and a `--full-recode` sweep cleared the
# other review's decisions. These tests pin both directions shut.
# ---------------------------------------------------------------------------


def test_another_reviews_abstract_tag_does_not_count_as_screened() -> None:
    items = [
        _item("A", [f"{OTHER_NS}abstract:include"]),
        _item("B", [f"{OTHER_NS}abstract:exclude"]),
        _item("C", [f"{ABSTRACT}include"]),
    ]
    assert abstract_screen._already_tagged(items, ABSTRACT) == {"C"}


def test_another_reviews_fulltext_tag_does_not_count_as_coded() -> None:
    items = [
        _item("A", [f"{OTHER_NS}fulltext:include"]),
        _item("B", [f"{FULLTEXT}include"]),
    ]
    assert fulltext_code._already_tagged(items, FULLTEXT) == {"B"}


def test_an_item_can_carry_both_reviews_verdicts_independently() -> None:
    """The same paper included here and excluded there is not a conflict."""
    items = [
        _item("A", [f"{ABSTRACT}include", f"{OTHER_NS}abstract:exclude"]),
    ]
    assert abstract_screen._already_tagged(items, ABSTRACT) == {"A"}
    assert abstract_screen._already_tagged(items, f"{OTHER_NS}abstract:") == {"A"}


def test_a_stage_tag_write_clears_only_this_reviews_prior_tag() -> None:
    """`remove_prefixed` is a plain startswith — this is the real guard."""
    op = abstract_screen._stage_tag_op(ABSTRACT, "include")
    assert op["add"] == [f"{ABSTRACT}include"]
    assert op["remove_prefixed"] == [ABSTRACT]
    # Nothing in the removal reaches the other review's namespace.
    assert not f"{OTHER_NS}abstract:exclude".startswith(ABSTRACT)


def test_the_coding_note_marker_differs_per_review() -> None:
    """Two reviews coding one paper get a note each, not one overwritten."""
    assert zotero_io.slr_coding_marker(NS) != zotero_io.slr_coding_marker(OTHER_NS)
    assert "test-review" in zotero_io.slr_coding_marker(NS)
