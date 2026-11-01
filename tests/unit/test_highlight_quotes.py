"""highlight_quotes: finding a verbatim quote in a PDF text layer.

Synthetic PDFs built with PyMuPDF; no Zotero, no network.
"""

from __future__ import annotations

import json

import highlight_quotes as hq
import pymupdf
import pytest

SENTENCE = (
    "Heteroskedasticity-based instruments identify the endogenous regressor "
    "without any external instrument when the errors are heteroskedastic."
)


def _pdf(tmp_path, pages: list[str], name="p.pdf"):
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(72, 72, 520, 700), text, fontsize=11)
    path = tmp_path / name
    doc.save(str(path))
    doc.close()
    return path


def test_squash_ignores_ligatures_quotes_hyphens_and_spacing():
    assert hq.squash("ﬁnd “this”-based  x") == hq.squash('find "this" based x')
    assert hq.squash("hetero-\nskedastic") == hq.squash("heteroskedastic")


def test_found_on_single_page_with_rects(tmp_path):
    path = _pdf(tmp_path, ["Intro text.", f"Method. {SENTENCE} More."])
    pages, heights = hq.read_pdf(path)
    loc = hq.locate_quote(pages, heights, SENTENCE)
    assert loc.status == "found" and loc.page == 2
    assert loc.rects and all(r[2] > r[0] and r[3] > r[1] for r in loc.rects)
    assert all(0 <= r[1] <= heights[2] for r in loc.rects)   # bottom-left origin


def test_quote_broken_across_lines_with_hyphen_still_matches(tmp_path):
    path = _pdf(tmp_path, [SENTENCE * 1])         # wraps inside the textbox
    pages, heights = hq.read_pdf(path)
    quote = "Heteroskedasticity based instruments identify the endogenous regressor"
    assert hq.locate_quote(pages, heights, quote).status == "found"


def test_not_found_and_ambiguous(tmp_path):
    path = _pdf(tmp_path, [SENTENCE, SENTENCE])
    pages, heights = hq.read_pdf(path)
    assert hq.locate_quote(pages, heights, SENTENCE).status == "ambiguous"
    assert hq.locate_quote(pages, heights, "A sentence the paper never contains at all.").status == "not_found"


def test_too_short_quote_is_refused(tmp_path):
    path = _pdf(tmp_path, [SENTENCE])
    pages, heights = hq.read_pdf(path)
    assert hq.locate_quote(pages, heights, "the errors").status == "not_found"


def test_parse_quotes_is_tolerant():
    raw = json.dumps([{"field": "setting", "quote": "x" * 20}, {"field": "a"}, "junk"])
    assert hq.parse_quotes(raw) == [{"field": "setting", "quote": "x" * 20}]
    assert hq.parse_quotes("") == [] and hq.parse_quotes("not json") == []


def test_annotation_payload_shape_and_stable_hash(tmp_path):
    path = _pdf(tmp_path, [SENTENCE])
    pages, heights = hq.read_pdf(path)
    loc = hq.locate_quote(pages, heights, SENTENCE)
    entry = {"field": "assumptions", "quote": SENTENCE}
    a = hq.build_annotation("ATT12345", loc, entry, "yes", "assumptions", "demo-x/")
    assert a["parentItem"] == "ATT12345" and a["annotationType"] == "highlight"
    assert a["annotationColor"] == hq.THEME_COLORS["assumptions"]
    assert {"tag": "demo-x/ai-highlight"} in a["tags"]
    assert json.loads(a["annotationPosition"])["pageIndex"] == 0
    again = hq.build_annotation("ATT12345", loc, entry, "yes", "assumptions", "demo-x/")
    assert a["tags"] == again["tags"]            # the idempotency key is stable


class _FakeZot:
    def __init__(self, existing=()):
        self.created: list[dict] = []
        self.existing = list(existing)

    def children(self, key):
        return [{"key": "ATT12345", "data": {
            "itemType": "attachment", "contentType": "application/pdf"}}]

    def list_annotations(self, key):
        return self.existing

    def create_highlights(self, payloads):
        self.created.extend(payloads)
        return ["K"] * len(payloads)

    def delete_item(self, key):
        return True


def _run(tmp_path, zot, monkeypatch, pdf):
    import fulltext_code
    monkeypatch.setattr(fulltext_code, "_find_pdf_path", lambda *a, **k: pdf)
    payload = {"fields": {"evidence_quotes": json.dumps(
        [{"field": "setting", "quote": SENTENCE},
         {"field": "setting", "quote": "This sentence is not in the paper anywhere."}])}}
    return hq.process_item(zot, {"key": "ITEM1234"}, payload, "demo-x/", None, {},
                           dry_run=False, replace=False)


def test_process_item_creates_then_skips_on_rerun(tmp_path, monkeypatch):
    pdf = _pdf(tmp_path, [SENTENCE])
    zot = _FakeZot()
    rows = _run(tmp_path, zot, monkeypatch, pdf)
    assert sorted(r["status"] for r in rows) == ["created", "not_found"]
    assert len(zot.created) == 1
    existing = [{"key": "A1", "data": {"tags": zot.created[0]["tags"]}}]
    rows = _run(tmp_path, _FakeZot(existing), monkeypatch, pdf)
    assert sorted(r["status"] for r in rows) == ["not_found", "skipped_existing"]


def test_several_pdfs_without_recorded_hash_is_refused(tmp_path, monkeypatch):
    import fulltext_code
    monkeypatch.setattr(fulltext_code, "_find_pdf_path",
                        lambda *a, **k: tmp_path / "x.pdf")
    kids = [{"key": k, "data": {"itemType": "attachment",
                                "contentType": "application/pdf"}} for k in ("A", "B")]
    _key, path, problem = hq.choose_attachment({"key": "I"}, kids, {}, None)
    assert path is None and problem == "ambiguous_attachment"


@pytest.mark.parametrize("raw", ["", None])
def test_item_without_quotes_is_logged(tmp_path, raw):
    rows = hq.process_item(_FakeZot(), {"key": "I"}, {"fields": {"evidence_quotes": raw or ""}},
                           "demo-x/", None, {}, dry_run=True, replace=False)
    assert rows[0]["status"] == "no_quotes"


def test_themes_come_from_the_config(tmp_path):
    cfg = tmp_path / "screening_config.py"
    cfg.write_text(
        "FULLTEXT_CODING_FIELDS = [{'name': 'a', 'theme': 'setting'},"
        " {'name': 'b'}]\nHIGHLIGHT_THEMES = {'b': 'overclaims', 'a': 'assumptions'}\n")
    assert hq.themes_from_config(str(cfg)) == {"a": "assumptions", "b": "overclaims"}
    assert hq.themes_from_config(str(tmp_path / "missing.py")) == {}


# --- Issue-list entries: own comment, entry tags, severity colour, merging ---

def _issue_payload(entries, other=None):
    fields = {"evidence_quotes": json.dumps(entries)}
    if other is not None:
        fields["other_findings"] = json.dumps(other)
    return {"fields": fields}


ISSUE_STYLE = hq.HighlightStyle(
    color_by="severity", entry_tags=True,
    layers={"D": "design", "M": "measurement", "A": "analysis", "R": "reporting"})


def test_parse_quotes_keeps_entry_attributes_and_accepts_a_list():
    entry = {"field": "A02", "quote": SENTENCE, "location": "p. 809",
             "status": "present", "severity": None, "comment": "Why."}
    assert hq.parse_quotes([entry]) == [{
        "field": "A02", "quote": SENTENCE, "location": "p. 809",
        "status": "present", "comment": "Why."}]


def test_entry_comment_uses_its_own_comment_else_field_value():
    e = {"field": "A02", "quote": SENTENCE, "status": "present",
         "severity": "minor", "comment": "Reliability is not reported."}
    assert hq.entry_comment(e, "yes") == "A02 · present · minor — Reliability is not reported."
    assert hq.entry_comment({"field": "setting", "quote": SENTENCE}, "lab") == "setting: lab"


def test_issue_entry_gets_comment_tags_and_severity_colour(tmp_path, monkeypatch):
    import fulltext_code
    pdf = _pdf(tmp_path, [SENTENCE])
    monkeypatch.setattr(fulltext_code, "_find_pdf_path", lambda *a, **k: pdf)
    zot = _FakeZot()
    payload = _issue_payload([{"field": "A02", "quote": SENTENCE, "status": "present",
                               "severity": "major", "comment": "Why."}])
    hq.process_item(zot, {"key": "I"}, payload, "slr/", None, {},
                    dry_run=False, replace=False, style=ISSUE_STYLE)
    (a,) = zot.created
    assert a["annotationComment"] == "A02 · present · major — Why."
    assert a["annotationColor"] == hq.SEVERITY_COLORS["major"]
    tags = {t["tag"] for t in a["tags"]}
    assert {"slr/ai-highlight", "slr/issue:A02", "slr/layer:analysis",
            "slr/status:present", "slr/severity:major"} <= tags


def test_entry_tags_are_off_by_default(tmp_path, monkeypatch):
    import fulltext_code
    pdf = _pdf(tmp_path, [SENTENCE])
    monkeypatch.setattr(fulltext_code, "_find_pdf_path", lambda *a, **k: pdf)
    zot = _FakeZot()
    payload = _issue_payload([{"field": "A02", "quote": SENTENCE, "severity": "major"}])
    hq.process_item(zot, {"key": "I"}, payload, "slr/", None, {},
                    dry_run=False, replace=False)
    assert len(zot.created[0]["tags"]) == 2           # ai-highlight + hash only
    assert zot.created[0]["annotationColor"] == hq.THEME_COLORS["other"]


def test_same_quote_on_same_page_merges_into_one_highlight(tmp_path, monkeypatch):
    import fulltext_code
    pdf = _pdf(tmp_path, [SENTENCE])
    monkeypatch.setattr(fulltext_code, "_find_pdf_path", lambda *a, **k: pdf)
    zot = _FakeZot()
    payload = _issue_payload(
        [{"field": "R05", "quote": SENTENCE, "status": "present",
          "severity": "minor", "comment": "First."},
         {"field": "OTHER-1", "quote": SENTENCE.upper(), "status": "present",
          "severity": "moderate", "comment": "Second."}],
        other=[{"id": "OTHER-1", "layer": "analysis"}])
    rows = hq.process_item(zot, {"key": "I"}, payload, "slr/", None, {},
                           dry_run=False, replace=False, style=ISSUE_STYLE)
    (a,) = zot.created
    assert a["annotationComment"] == (
        "R05 · present · minor — First.\n\nOTHER-1 · present · moderate — Second.")
    tags = {t["tag"] for t in a["tags"]}
    assert {"slr/issue:R05", "slr/layer:reporting", "slr/issue:OTHER-1",
            "slr/layer:analysis", "slr/severity:minor", "slr/severity:moderate"} <= tags
    # OTHER-n ranks below any codebook severity, so the minor R05 colours it.
    assert a["annotationColor"] == hq.SEVERITY_COLORS["minor"]
    assert [r["status"] for r in rows] == ["created", "created"]
    assert all(r["detail"] == "merged 2" for r in rows)


@pytest.mark.parametrize("entry, cls", [
    ({"field": "D01", "status": "exemplary"}, "exemplary"),
    ({"field": "OTHER-3", "severity": "major"}, "other"),
    ({"field": "M02", "status": "cannot_determine"}, "unknown"),
    ({"field": "M02", "severity": "moderate"}, "moderate"),
])
def test_severity_class(entry, cls):
    assert hq.severity_class(entry) == cls


def test_other_entry_without_a_recorded_layer_is_layer_other():
    e = {"field": "OTHER-9", "quote": SENTENCE}
    assert "slr/layer:other" in hq.entry_tags(e, "slr/", ISSUE_STYLE, {})


def test_style_from_config(tmp_path):
    cfg = tmp_path / "screening_config.py"
    cfg.write_text("HIGHLIGHT_COLOR_BY = 'severity'\nHIGHLIGHT_ENTRY_TAGS = True\n"
                   "HIGHLIGHT_LAYERS = {'D': 'design'}\n")
    assert hq.style_from_config(str(cfg)) == hq.HighlightStyle("severity", True, {"D": "design"})
    assert hq.style_from_config(str(tmp_path / "missing.py")) == hq.HighlightStyle()
    cfg.write_text("HIGHLIGHT_COLOR_BY = 'rainbow'\n")
    with pytest.raises(SystemExit):
        hq.style_from_config(str(cfg))
