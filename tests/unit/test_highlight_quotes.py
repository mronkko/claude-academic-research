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
