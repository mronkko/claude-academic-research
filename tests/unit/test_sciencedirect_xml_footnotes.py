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
    _document_flow,
    _extract_article,
    _extract_xml_blocks,
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
    assert _assemble_body([], []) == ""


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


# ---------------------------------------------------------------------------
# Document structure
# ---------------------------------------------------------------------------

STRUCTURED_XML = b"""<?xml version="1.0"?>
<ns0:article xmlns:ns0="http://www.elsevier.com/xml/common/dtd">
  <ns0:body><ns0:sections>
    <ns0:section><ns0:section-title>Introduction</ns0:section-title>
      <ns0:para>Opening prose.</ns0:para>
      <ns0:section><ns0:section-title>Background</ns0:section-title>
        <ns0:para>Nested prose.<ns0:list><ns0:list-item><ns0:label>&#8226;</ns0:label>\
<ns0:para>First bullet.</ns0:para></ns0:list-item><ns0:list-item>\
<ns0:para>Second bullet.</ns0:para></ns0:list-item></ns0:list></ns0:para>
        <ns0:displayed-quote><ns0:simple-para>A quoted passage.\
</ns0:simple-para></ns0:displayed-quote>
      </ns0:section>
    </ns0:section>
  </ns0:sections></ns0:body>
</ns0:article>
"""

TABLE_XML = b"""<?xml version="1.0"?>
<ns0:article xmlns:ns0="http://www.elsevier.com/xml/common/dtd">
  <ns0:body><ns0:section><ns0:para>Text before.<ns0:table><ns0:tgroup>\
<ns0:tbody><ns0:row><ns0:entry><ns0:bold>Measure</ns0:bold></ns0:entry>\
<ns0:entry>D &gt; A</ns0:entry></ns0:row><ns0:row><ns0:entry>Dominance\
</ns0:entry><ns0:entry>D &gt; 50% &gt; A</ns0:entry></ns0:row></ns0:tbody>\
</ns0:tgroup></ns0:table></ns0:para></ns0:section></ns0:body>
</ns0:article>
"""


def test_section_titles_become_headings() -> None:
    """They used to be concatenated into the first sentence of their own
    section, so every recovered article opened "Introduction Fairness
    and discrimination…" and ran on as one undifferentiated block."""
    blocks, _ = _extract_xml_blocks(STRUCTURED_XML)
    assert ("h1", "Introduction") in blocks
    assert ("h2", "Background") in blocks


def test_headings_nest_by_section_depth() -> None:
    blocks, _ = _extract_xml_blocks(STRUCTURED_XML)
    kinds = {text: kind for kind, text in blocks}
    assert kinds["Introduction"] == "h1"
    assert kinds["Background"] == "h2"


def test_paragraphs_are_separate_blocks() -> None:
    blocks, _ = _extract_xml_blocks(STRUCTURED_XML)
    paras = [t for k, t in blocks if k == "p"]
    assert "Opening prose." in paras
    assert "Nested prose." in paras


def test_list_items_are_their_own_blocks() -> None:
    """A list nested inside its introducing paragraph must not dissolve
    into that paragraph's sentence."""
    blocks, _ = _extract_xml_blocks(STRUCTURED_XML)
    items = [t for k, t in blocks if k == "li"]
    assert items == ["First bullet.", "Second bullet."]
    assert "First bullet." not in dict.fromkeys(
        t for k, t in blocks if k == "p"
    )


def test_block_quotes_are_marked_as_such() -> None:
    blocks, _ = _extract_xml_blocks(STRUCTURED_XML)
    assert ("quote", "A quoted passage.") in blocks


def test_table_rows_survive_as_rows() -> None:
    """Cells hold inline markup rather than `<ce:para>`, so a generic
    block walk finds nothing to emit and the table vanishes silently."""
    blocks, _ = _extract_xml_blocks(TABLE_XML)
    rows = [t for k, t in blocks if k == "row"]
    assert rows == ["Measure  |  D > A", "Dominance  |  D > 50% > A"]


def test_table_text_is_not_glued_into_the_paragraph() -> None:
    blocks, _ = _extract_xml_blocks(TABLE_XML)
    paras = [t for k, t in blocks if k == "p"]
    assert paras == ["Text before."]


def test_flattened_parts_still_read_as_text() -> None:
    """`_extract_xml_parts` keeps its contract for the length check and
    any text-only consumer."""
    prose, notes = _extract_xml_parts(STRUCTURED_XML)
    assert "Introduction" in prose
    assert "Opening prose." in prose
    assert notes == []


def test_assembled_body_appends_the_footnotes_heading() -> None:
    blocks, notes = _extract_xml_blocks(FOOTNOTE_XML)
    body = _assemble_body(blocks, notes)
    assert body.index("Footnotes") < body.index("European Commission")


def test_document_flow_renders_notes_after_a_heading() -> None:
    blocks, notes = _extract_xml_blocks(FOOTNOTE_XML)
    flow = _document_flow(blocks, notes)
    kinds = [k for k, _ in flow]
    assert kinds[-2:] == ["h1", "note"]
    assert flow[-2] == ("h1", "Footnotes")


def test_document_flow_omits_the_heading_when_there_are_no_notes() -> None:
    blocks, notes = _extract_xml_blocks(NO_FOOTNOTE_XML)
    assert _document_flow(blocks, notes) == blocks


# ---------------------------------------------------------------------------
# Front matter
# ---------------------------------------------------------------------------

COREDATA_XML = b"""<?xml version="1.0"?>
<ns0:response xmlns:ns0="http://www.elsevier.com/xml/svapi/article/dtd"
              xmlns:ns1="http://purl.org/dc/elements/1.1/">
  <ns0:coredata>
    <ns1:title>Why fairness cannot be automated</ns1:title>
    <ns0:publicationName>Computer Law &amp; Security Review</ns0:publicationName>
    <ns1:creator>Wachter, Sandra</ns1:creator>
    <ns1:creator>Mittelstadt, Brent</ns1:creator>
    <ns0:volume>41</ns0:volume>
    <ns0:pageRange>105567</ns0:pageRange>
    <ns0:coverDisplayDate>July 2021</ns0:coverDisplayDate>
    <ns0:doi>10.1016/j.clsr.2021.105567</ns0:doi>
    <ns0:issn>2212473X</ns0:issn>
    <ns1:description>An abstract about discrimination.</ns1:description>
    <ns0:subject>Fairness</ns0:subject>
    <ns0:subject>Bias</ns0:subject>
  </ns0:coredata>
  <ns0:originalText><ns0:author-group>
    <ns0:affiliation><ns0:label>a</ns0:label>
      <ns0:textfn>Oxford Internet Institute, University of Oxford</ns0:textfn>
    </ns0:affiliation>
  </ns0:author-group>
  <ns0:body><ns0:section><ns0:para>Body text.</ns0:para></ns0:section></ns0:body>
  </ns0:originalText>
</ns0:response>
"""


def test_front_matter_is_extracted() -> None:
    """None of this used to reach the recovered file, so it opened cold
    on the first sentence with no title, authors or DOI anywhere in it."""
    meta, _blocks, _notes = _extract_article(COREDATA_XML)
    assert meta["title"] == "Why fairness cannot be automated"
    assert meta["authors"] == ["Wachter, Sandra", "Mittelstadt, Brent"]
    assert meta["journal"] == "Computer Law & Security Review"
    assert meta["volume"] == "41"
    assert meta["doi"] == "10.1016/j.clsr.2021.105567"
    assert meta["abstract"] == "An abstract about discrimination."
    assert meta["keywords"] == ["Fairness", "Bias"]


def test_affiliations_are_labelled_and_deduplicated() -> None:
    """Elsevier repeats each affiliation in a formatted and a structured
    form; only the formatted one belongs on a cover page."""
    meta, _b, _n = _extract_article(COREDATA_XML)
    assert meta["affiliations"] == [
        "(a) Oxford Internet Institute, University of Oxford",
    ]


def test_metadata_and_body_come_from_one_parse() -> None:
    meta, blocks, _notes = _extract_article(COREDATA_XML)
    assert meta["title"]
    assert ("p", "Body text.") in blocks


def test_missing_coredata_is_empty_metadata_not_a_crash() -> None:
    meta, blocks, _notes = _extract_article(FOOTNOTE_XML)
    assert meta == {}
    assert blocks, "the body must still be recovered without front matter"


def test_extract_article_survives_malformed_xml() -> None:
    assert _extract_article(b"<not><well>formed") == ({}, [], [])
