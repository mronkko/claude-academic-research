"""Tests for the Zotero library audit script's classifier."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

AUDIT = Path(__file__).resolve().parents[2] / "scripts" / "pipelines" / "audit_zotero_library.py"


def _load():
    spec = importlib.util.spec_from_file_location("audit_zotero", AUDIT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["audit_zotero"] = mod
    spec.loader.exec_module(mod)
    return mod


def _item(key: str, abstract: str = "", item_type: str = "journalArticle",
          title: str = "") -> dict:
    return {"data": {"key": key, "itemType": item_type,
                     "abstractNote": abstract, "title": title}}


def _pdf_attachment(parent: str, md5: str | None = "deadbeef") -> dict:
    return {"data": {"parentItem": parent, "contentType": "application/pdf",
                     "md5": md5, "key": f"att_{parent}"}}


def test_classify_empty_library() -> None:
    mod = _load()
    r = mod._classify([], {})
    assert r["total_items"] == 0
    assert r["have_pdf"] == 0
    assert r["missing_pdf_count"] == 0
    assert r["empty_stub_count"] == 0
    assert r["missing_abstract_count"] == 0


def test_classify_item_with_pdf_and_abstract() -> None:
    mod = _load()
    items = [_item("A1", abstract="Some abstract", title="Paper A")]
    atts = {"A1": [_pdf_attachment("A1")]}
    r = mod._classify(items, atts)
    assert r["total_items"] == 1
    assert r["have_pdf"] == 1
    assert r["missing_pdf_count"] == 0
    assert r["missing_abstract_count"] == 0


def test_classify_item_missing_abstract() -> None:
    mod = _load()
    items = [_item("A1", title="No abstract")]
    r = mod._classify(items, {"A1": [_pdf_attachment("A1")]})
    assert r["missing_abstract_count"] == 1
    assert r["missing_abstract"][0]["key"] == "A1"
    assert r["have_pdf"] == 1  # has a PDF, just no abstract


def test_classify_item_missing_pdf() -> None:
    mod = _load()
    items = [_item("A1", abstract="x")]
    r = mod._classify(items, {})
    assert r["missing_pdf_count"] == 1
    assert r["have_pdf"] == 0


def test_classify_empty_stub() -> None:
    mod = _load()
    items = [_item("A1", abstract="x")]
    atts = {"A1": [_pdf_attachment("A1", md5=None)]}  # no md5 = empty stub
    r = mod._classify(items, atts)
    assert r["empty_stub_count"] == 1
    assert r["missing_pdf_count"] == 0  # stub counts separately


def test_classify_ignores_attachments_and_notes() -> None:
    mod = _load()
    items = [
        _item("A1", item_type="attachment"),
        _item("N1", item_type="note"),
        _item("B1", abstract="x", item_type="journalArticle"),
    ]
    r = mod._classify(items, {})
    assert r["total_items"] == 1  # only B1 counts
