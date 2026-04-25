"""Tests for fulltext_code._find_pdf_path (T2-5).

The user's session log shows them writing a `link_zotero_pdfs.py`
script to symlink Zotero attachment files into a project-local
`./pdfs/` directory because `_find_pdf_path` only looked there. The
upstream fix is to resolve PDFs from Zotero's storage tree directly:
`<zotero_storage>/storage/<attachment_key>/<filename>`. The legacy
`pdf_dir` path is kept as a fallback so existing projects keep
working.
"""

from __future__ import annotations

from pathlib import Path

import fulltext_code


def _stored_attachment(att_key: str, filename: str) -> dict:
    """A pyzotero stored-attachment item (md5 set, contentType=PDF)."""
    return {
        "key": att_key,
        "data": {
            "key": att_key,
            "contentType": "application/pdf",
            "filename": filename,
            "md5": "deadbeef",
            "linkMode": "imported_file",
        },
    }


def _linked_attachment(filename: str, path: str) -> dict:
    """A linked-file attachment (Zotero stores a path, not the bytes)."""
    return {
        "key": "LINK0001",
        "data": {
            "key": "LINK0001",
            "contentType": "application/pdf",
            "filename": filename,
            "md5": "linked",
            "linkMode": "linked_file",
            "path": path,
        },
    }


def _journal_item(item_key: str, doi: str = "") -> dict:
    return {"key": item_key, "data": {"key": item_key, "DOI": doi}}


def test_resolves_stored_attachment_under_zotero_storage_tree(tmp_path: Path) -> None:
    """The default Zotero convention: stored attachments live at
    `<zotero_storage>/storage/<attachment_key>/<filename>`. The
    resolver finds them there without any project-local symlinking."""
    zotero_storage = tmp_path / "Zotero"
    storage_dir = zotero_storage / "storage" / "ATTACH001"
    storage_dir.mkdir(parents=True)
    pdf_path = storage_dir / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 mock")

    item = _journal_item("ITEM0001")
    atts = {"ITEM0001": [_stored_attachment("ATTACH001", "paper.pdf")]}
    found = fulltext_code._find_pdf_path(
        item, atts, pdf_dir=None, zotero_storage=zotero_storage,
    )
    assert found == pdf_path


def test_resolves_linked_file_with_absolute_path(tmp_path: Path) -> None:
    """Linked attachments carry an absolute path in `data.path`. The
    resolver uses it directly — no storage tree lookup."""
    pdf_path = tmp_path / "external" / "linked.pdf"
    pdf_path.parent.mkdir(parents=True)
    pdf_path.write_bytes(b"%PDF-1.4 mock")

    item = _journal_item("ITEM0002")
    atts = {"ITEM0002": [_linked_attachment("linked.pdf", str(pdf_path))]}
    found = fulltext_code._find_pdf_path(
        item, atts, pdf_dir=None, zotero_storage=tmp_path,
    )
    assert found == pdf_path


def test_resolves_linked_file_with_attachments_relative_path(tmp_path: Path) -> None:
    """Linked attachments may use Zotero's `attachments:<rel>` sentinel
    relative to the data dir. Resolver walks `zotero_storage` for these."""
    zotero_storage = tmp_path / "Zotero"
    rel_target = zotero_storage / "linked-pdfs" / "paper.pdf"
    rel_target.parent.mkdir(parents=True)
    rel_target.write_bytes(b"%PDF-1.4 mock")

    item = _journal_item("ITEM0003")
    atts = {"ITEM0003": [_linked_attachment(
        "paper.pdf", "attachments:linked-pdfs/paper.pdf",
    )]}
    found = fulltext_code._find_pdf_path(
        item, atts, pdf_dir=None, zotero_storage=zotero_storage,
    )
    assert found == rel_target


def test_falls_back_to_legacy_pdf_dir_when_storage_tree_misses(tmp_path: Path) -> None:
    """Legacy contract: if the Zotero storage tree doesn't have it but
    a project-local `./pdfs/<filename>` does, use that. Keeps existing
    projects working without forcing every user to migrate."""
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    pdf_path = pdf_dir / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 mock")

    zotero_storage = tmp_path / "Zotero"  # exists but empty
    zotero_storage.mkdir()

    item = _journal_item("ITEM0004")
    atts = {"ITEM0004": [_stored_attachment("ATTACH004", "paper.pdf")]}
    found = fulltext_code._find_pdf_path(
        item, atts, pdf_dir=pdf_dir, zotero_storage=zotero_storage,
    )
    assert found == pdf_path


def test_doi_named_fallback_in_pdf_dir(tmp_path: Path) -> None:
    """Final fallback for hand-placed / TDM-recovered PDFs that match
    the `<doi-with-slash-replaced>.pdf` convention."""
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    pdf_path = pdf_dir / "10.1016_j.respol.2020.104010.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 mock")

    item = _journal_item("ITEM0005", doi="10.1016/j.respol.2020.104010")
    atts: dict[str, list[dict]] = {"ITEM0005": []}
    found = fulltext_code._find_pdf_path(
        item, atts, pdf_dir=pdf_dir, zotero_storage=None,
    )
    assert found == pdf_path


def test_returns_none_when_no_path_works(tmp_path: Path) -> None:
    item = _journal_item("ITEM0006", doi="10.1234/missing")
    atts = {"ITEM0006": [_stored_attachment("ATTACH006", "paper.pdf")]}
    found = fulltext_code._find_pdf_path(
        item, atts,
        pdf_dir=tmp_path / "missing-dir",
        zotero_storage=tmp_path / "Zotero",
    )
    assert found is None


def test_skips_non_pdf_attachments(tmp_path: Path) -> None:
    """Attachments without contentType=application/pdf are ignored."""
    zotero_storage = tmp_path / "Zotero"
    storage_dir = zotero_storage / "storage" / "ATTACH007"
    storage_dir.mkdir(parents=True)
    (storage_dir / "notes.txt").write_text("not a PDF", encoding="utf-8")

    item = _journal_item("ITEM0007")
    atts = {"ITEM0007": [{
        "key": "ATTACH007",
        "data": {
            "key": "ATTACH007",
            "contentType": "text/plain",  # not a PDF
            "filename": "notes.txt",
            "md5": "deadbeef",
        },
    }]}
    found = fulltext_code._find_pdf_path(
        item, atts, pdf_dir=None, zotero_storage=zotero_storage,
    )
    assert found is None


def test_skips_attachments_without_md5(tmp_path: Path) -> None:
    """Stub attachments (no md5 yet — pyzotero hasn't synced the bytes)
    are skipped so we don't 404 on a pending upload."""
    zotero_storage = tmp_path / "Zotero"
    storage_dir = zotero_storage / "storage" / "ATTACH008"
    storage_dir.mkdir(parents=True)
    (storage_dir / "stub.pdf").write_bytes(b"%PDF-1.4 stub")

    item = _journal_item("ITEM0008")
    atts = {"ITEM0008": [{
        "key": "ATTACH008",
        "data": {
            "key": "ATTACH008",
            "contentType": "application/pdf",
            "filename": "stub.pdf",
            "md5": "",  # stub — no synced bytes
        },
    }]}
    found = fulltext_code._find_pdf_path(
        item, atts, pdf_dir=None, zotero_storage=zotero_storage,
    )
    assert found is None


def test_zotero_storage_takes_precedence_over_pdf_dir(tmp_path: Path) -> None:
    """When both the storage tree and the legacy pdf_dir have the file,
    prefer the storage tree (it's authoritative)."""
    zotero_storage = tmp_path / "Zotero"
    storage_dir = zotero_storage / "storage" / "ATTACH009"
    storage_dir.mkdir(parents=True)
    storage_pdf = storage_dir / "paper.pdf"
    storage_pdf.write_bytes(b"%PDF-1.4 from-storage")

    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    legacy_pdf = pdf_dir / "paper.pdf"
    legacy_pdf.write_bytes(b"%PDF-1.4 from-legacy")

    item = _journal_item("ITEM0009")
    atts = {"ITEM0009": [_stored_attachment("ATTACH009", "paper.pdf")]}
    found = fulltext_code._find_pdf_path(
        item, atts, pdf_dir=pdf_dir, zotero_storage=zotero_storage,
    )
    assert found == storage_pdf
