"""Where footnotes end up in text recovered from Elsevier's XML.

Elsevier places each `<ce:footnote>` **at the point its marker
appears**, so the document-order text walk `_extract_xml_body` used to
do spliced the whole footnote — citation, URL, access date — between a
clause and its continuation. Every annotated sentence came out broken
in two, and nothing downstream could tell: the recovered PDF is
well-formed, passes size and text-length checks, and reads as a normal
article to anything that does not look closely.

Measured on Wachter, Mittelstadt & Russell (2021), *Computer Law &
Security Review* — a law review carrying 301 `<ce:footnote>` elements
in its body — 35 % of the extracted text was footnote material
interleaved into the prose. In the study that found this, 121 of 130
open PDF-identity questions were recovered files whose title-similarity
scores were depressed by exactly this, and two articles were
misclassified as the wrong article on that basis. Both were correct.

The XML for that article is not checked in — it is publisher full text
— so the fixtures below reproduce its structure exactly: a
`<ce:cross-ref>` marker, then the `<ce:footnote>` subtree, and the
sentence's continuation carried in the footnote's **tail**.
"""

from __future__ import annotations

from fetchers.sciencedirect import (
    _assemble_body,
    _extract_xml_body,
    _extract_xml_parts,
    _recovery_note,
)

# The shape Elsevier actually emits, from the article above: the
# sentence continues in the footnote's tail, not in a sibling node.
FOOTNOTE_XML = b"""<?xml version="1.0"?>
<ns0:article xmlns:ns0="http://www.elsevier.com/xml/common/dtd">
  <ns0:body><ns0:section><ns0:para>In this paper we will focus on the \
four non-discrimination directives of the EU :<ns0:cross-ref refid="cit_41">\
<ns0:sup>41</ns0:sup></ns0:cross-ref><ns0:footnote id="cit_41">\
<ns0:label>41</ns0:label><ns0:note-para>European Commission, \
'Non-Discrimination' accessed 2 March 2020.</ns0:note-para></ns0:footnote> \
the Racial Equality Directive (2000/43/EC), and the rest follow.\
</ns0:para></ns0:section></ns0:body>
</ns0:article>
"""

NO_FOOTNOTE_XML = b"""<?xml version="1.0"?>
<ns0:article xmlns:ns0="http://www.elsevier.com/xml/common/dtd">
  <ns0:body><ns0:section><ns0:para>A plain paragraph with no \
annotations.</ns0:para></ns0:section></ns0:body>
</ns0:article>
"""

TABLE_FOOTNOTE_XML = b"""<?xml version="1.0"?>
<ns0:article xmlns:ns0="http://www.elsevier.com/xml/common/dtd">
  <ns0:body><ns0:section><ns0:table><ns0:tbody><ns0:row><ns0:entry>Cell \
value<ns0:table-footnote><ns0:label>a</ns0:label><ns0:note-para>Standard \
errors in parentheses.</ns0:note-para></ns0:table-footnote> and more \
text.</ns0:entry></ns0:row></ns0:tbody></ns0:table></ns0:section></ns0:body>
</ns0:article>
"""


# ---------------------------------------------------------------------------
# The reported defect
# ---------------------------------------------------------------------------


def test_footnote_text_is_not_spliced_into_the_sentence() -> None:
    """The exact symptom: footnote 41's content landed mid-clause."""
    prose, _ = _extract_xml_parts(FOOTNOTE_XML)
    assert "European Commission" not in prose
    assert (
        "of the EU : 41 the Racial Equality Directive (2000/43/EC), "
        "and the rest follow." in prose
    )


def test_the_sentence_continuation_survives() -> None:
    """The continuation lives in the footnote's *tail*. Dropping the
    subtree and its tail together would weld the words either side of
    the marker — one corruption traded for another."""
    prose, _ = _extract_xml_parts(FOOTNOTE_XML)
    assert "the Racial Equality Directive (2000/43/EC)" in prose


def test_the_in_text_marker_is_kept() -> None:
    """A reader still has to be able to match marker to note."""
    prose, _ = _extract_xml_parts(FOOTNOTE_XML)
    assert "EU : 41 the Racial" in prose


# ---------------------------------------------------------------------------
# Relocation, not deletion
# ---------------------------------------------------------------------------


def test_the_footnote_is_kept_as_an_endnote() -> None:
    """Dropping footnotes would lose real content — in the law reviews
    this fallback is most used on, they carry much of the argument."""
    _, notes = _extract_xml_parts(FOOTNOTE_XML)
    assert len(notes) == 1
    assert "European Commission" in notes[0]
    assert notes[0].startswith("41"), "the note keeps its own label"


def test_notes_are_appended_under_a_heading() -> None:
    body = _extract_xml_body(FOOTNOTE_XML)
    prose, notes = _extract_xml_parts(FOOTNOTE_XML)
    assert body.index("Footnotes") > body.index("Racial Equality")
    assert body.endswith(notes[-1])


def test_nothing_is_lost_overall() -> None:
    """Every character of the old spliced output is still present —
    reordered, not discarded."""
    body = _extract_xml_body(FOOTNOTE_XML)
    assert "European Commission" in body
    assert "Racial Equality Directive" in body


# ---------------------------------------------------------------------------
# Scope of the skip set
# ---------------------------------------------------------------------------


def test_table_footnotes_are_relocated_too() -> None:
    """Same shape, one level down: an annotation anchored at a marker
    inside a table cell."""
    prose, notes = _extract_xml_parts(TABLE_FOOTNOTE_XML)
    assert "Standard errors" not in prose
    assert "Cell value and more text." in prose
    assert any("Standard errors" in n for n in notes)


def test_a_nested_annotation_is_not_rendered_twice() -> None:
    nested = b"""<?xml version="1.0"?>
    <ns0:article xmlns:ns0="http://x">
      <ns0:body><ns0:para>Prose<ns0:footnote><ns0:label>1</ns0:label>\
<ns0:note-para>Outer note<ns0:footnote><ns0:note-para>Inner note\
</ns0:note-para></ns0:footnote></ns0:note-para></ns0:footnote> \
continues.</ns0:para></ns0:body>
    </ns0:article>
    """
    _, notes = _extract_xml_parts(nested)
    assert len(notes) == 1
    assert notes[0].count("Inner note") == 1


# ---------------------------------------------------------------------------
# Unannotated articles are unaffected
# ---------------------------------------------------------------------------


def test_an_article_without_footnotes_gets_no_heading() -> None:
    body = _extract_xml_body(NO_FOOTNOTE_XML)
    assert body == "A plain paragraph with no annotations."
    assert "Footnotes" not in body


def test_assemble_body_is_empty_for_nothing() -> None:
    assert _assemble_body("", []) == ""


def test_malformed_xml_still_yields_nothing() -> None:
    assert _extract_xml_parts(b"<not><well>formed") == ("", [])
    assert _extract_xml_body(b"<not><well>formed") == ""


def test_no_body_element_yields_nothing() -> None:
    assert _extract_xml_parts(b"<article><coredata>x</coredata></article>") == ("", [])


# ---------------------------------------------------------------------------
# The provenance stamp
# ---------------------------------------------------------------------------


def test_recovery_note_names_the_version_and_the_count() -> None:
    """Every recovery ever made carries the same `-tdm-recovered`
    suffix, so without a stamp a corpus mixes silently-corrupted text
    with corrected text and no file says which it is."""
    note = _recovery_note(302)
    assert "claude-academic-research" in note
    assert "302 footnote(s) were moved" in note
    assert "not the publisher's PDF" in note


def test_recovery_note_handles_an_article_with_no_footnotes() -> None:
    note = _recovery_note(0)
    assert "No footnotes were present" in note
    assert "moved" not in note


def test_recovery_note_reports_a_real_version() -> None:
    from fetchers.sciencedirect import _plugin_version
    version = _plugin_version()
    assert version != "unknown", "plugin.json should be readable from the repo"
    assert version[0].isdigit()
