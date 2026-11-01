"""Appendix / supplement attachments: never the article, appended when coding.

An item with an online appendix attached as a second PDF used to be coded
from whichever PDF `_find_pdf_path` met first, so the coder could read the
appendix *instead of* the article. Attachments titled appendix/supplement
are now skipped as the main text and appended after it.
"""

from __future__ import annotations

from pathlib import Path

import fulltext_code


def _att(key: str, filename: str, title: str) -> dict:
    return {
        "key": key,
        "data": {
            "key": key, "title": title, "contentType": "application/pdf",
            "filename": filename, "md5": "deadbeef", "linkMode": "imported_file",
        },
    }


def _store(tmp_path: Path, key: str, filename: str) -> Path:
    p = tmp_path / "storage" / key / filename
    p.parent.mkdir(parents=True)
    p.write_bytes(b"%PDF-1.4")
    return p


ITEM = {"key": "ITEM0001", "data": {"key": "ITEM0001", "title": "A paper"}}


def test_supplement_title_matching() -> None:
    for title in ("Online appendix", "Web Appendix C", "Supplementary material",
                  "Online Appendices"):
        assert fulltext_code.is_supplement_attachment(_att("A", "x.pdf", title))
    for title in ("Full Text PDF", "Accepted Version", ""):
        assert not fulltext_code.is_supplement_attachment(_att("A", "x.pdf", title))


def test_filename_does_not_mark_a_supplement() -> None:
    # Zotero renames files after the paper's title, which may contain the word.
    att = _att("A", "Supplementing boards - 2020.pdf", "Full Text PDF")
    assert not fulltext_code.is_supplement_attachment(att)


def test_article_is_main_text_even_when_appendix_comes_first(tmp_path: Path) -> None:
    _store(tmp_path, "APPX0001", "appendix.pdf")
    article = _store(tmp_path, "MAIN0001", "paper.pdf")
    atts = {"ITEM0001": [_att("APPX0001", "appendix.pdf", "Online appendix"),
                         _att("MAIN0001", "paper.pdf", "Full Text PDF")]}
    assert fulltext_code._find_pdf_path(ITEM, atts, zotero_storage=tmp_path) == article
    assert fulltext_code._find_supplement_paths(ITEM, atts, tmp_path) == [
        tmp_path / "storage" / "APPX0001" / "appendix.pdf"]


def test_only_an_appendix_is_no_article(tmp_path: Path) -> None:
    _store(tmp_path, "APPX0001", "appendix.pdf")
    atts = {"ITEM0001": [_att("APPX0001", "appendix.pdf", "Online appendix")]}
    assert fulltext_code._find_pdf_path(ITEM, atts, zotero_storage=tmp_path) is None


def test_appendix_text_is_appended_after_article(tmp_path: Path, monkeypatch) -> None:
    texts = {"paper.pdf": "ARTICLE TEXT", "appendix.pdf": "APPENDIX TEXT"}
    monkeypatch.setattr(fulltext_code.pdf_text_cache, "get_text",
                        lambda key, path: texts[Path(path).name])
    row, msg = fulltext_code.build_coding_request(
        ITEM, tmp_path / "paper.pdf", model="m", fields=[],
        supplement_paths=[tmp_path / "appendix.pdf"])
    assert msg.index("ARTICLE TEXT") < msg.index("ONLINE APPENDIX OR SUPPLEMENT") \
        < msg.index("APPENDIX TEXT")
    assert row["pdf_path"] == str(tmp_path / "paper.pdf")


# --- --strip-references ---------------------------------------------------

import fulltext_code as _fc  # noqa: E402

_BODY = "Method. We regress y on x with fixed effects.\n" * 40


def test_strip_references_cuts_to_the_end():
    text = _BODY + "References\nAntonakis, J. (2010). On making causal claims.\n" * 1
    out, removed = _fc.strip_references(text)
    assert "Antonakis" not in out and out.startswith(_BODY)
    assert _fc.REFERENCES_OMITTED in out and removed > 0


def test_strip_references_keeps_appendices_and_trailing_tables():
    refs = "REFERENCES\nAiken, L. S. (1991). Multiple regression.\n"
    for tail in ("Appendix A\nItem wordings.\n", "Table 1\nMeans and correlations.\n",
                 "Online Appendix\nRobustness.\n"):
        out, removed = _fc.strip_references(_BODY + refs + tail)
        assert "Aiken" not in out and out.endswith(tail) and removed == len(refs)


def test_strip_references_cuts_from_the_last_heading_only_in_the_second_half():
    # A heading early on (a table of contents, a "References to prior work"
    # section) is not the reference list.
    early = "References\n" + _BODY
    assert _fc.strip_references(early) == (early, 0)
    text = _BODY + "1. References\nFirst list.\n" + _BODY + "7. Bibliography\nLast list.\n"
    out, _ = _fc.strip_references(text)
    assert "First list." in out and "Last list." not in out


def test_a_heading_after_another_columns_text_is_not_found():
    # The left column's article text comes first on the line: cutting
    # there would drop it.
    text = _BODY + "the cross-sectional design limits     References\nSmith (2001).\n"
    assert _fc.strip_references(text) == (text, 0)
    deep = _BODY + " " * 60 + "References     networks? Belief in\nSmith (2001).\n"
    assert _fc.strip_references(deep) == (deep, 0)


def test_a_left_margin_heading_with_a_second_column_and_a_page_break_is_found():
    for line in ("References          no. 10: 1303-1314.\n", "\f   References\n"):
        out, removed = _fc.strip_references(_BODY + line + "Smith (2001).\n")
        assert "Smith (2001)" not in out and removed > 0


def test_build_coding_request_strips_only_when_asked(tmp_path, monkeypatch):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF")
    monkeypatch.setattr(_fc.pdf_text_cache, "get_text",
                        lambda key, path: _BODY + "References\nSmith (2001).\n")
    item = {"key": "K", "data": {"key": "K", "title": "T"}}
    row, msg = _fc.build_coding_request(item, pdf, model="m", fields=[])
    assert "Smith (2001)" in msg and row["references_stripped_chars"] == 0
    row, msg = _fc.build_coding_request(item, pdf, model="m", fields=[], strip_refs=True)
    assert "Smith (2001)" not in msg and row["references_stripped_chars"] > 0
