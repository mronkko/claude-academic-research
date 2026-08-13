"""Unit tests for `_run_api_cascade`'s TDM-recovered tagging (P11 item 3).

When the ScienceDirect XML-fallback path recovers a text-only PDF, the
cached file is named `<doi>-tdm-recovered.pdf` (see
`fetchers.sciencedirect._TDM_RECOVERED_SUFFIX`). After uploading such a
file to Zotero, the cascade should tag the item `pdf:tdm-recovered` so
`audit_zotero_library.py` can flag it for review. Natively-fetched PDFs
must not be tagged.
"""

from __future__ import annotations

import argparse
import csv
import io
from pathlib import Path
from unittest.mock import MagicMock

from enrich_pdfs import LOG_FIELDS, _run_api_cascade
from fetchers.sciencedirect import TDM_RECOVERED_TAG


def _make_args(cache_dir: str) -> argparse.Namespace:
    return argparse.Namespace(
        workers=1,
        cache_dir=cache_dir,
        failure_log_csv=None,
        dry_run=False,
    )


def _fake_pdf(marker: bytes = b"body") -> bytes:
    """A structurally plausible PDF.

    `_attach_and_log` validates structure before uploading, because
    attaching a truncated file makes the item look permanently done.
    Fixtures therefore need an %%EOF trailer and a realistic size.
    """
    return b"%PDF-1.4\n" + marker + b"\n" + b"0" * 2000 + b"\n%%EOF\n"


def _make_log_writer():
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=LOG_FIELDS)
    writer.writeheader()
    return writer


def test_tags_item_when_recovered_pdf_is_attached(tmp_path: Path) -> None:
    recovered_path = tmp_path / "10.1016_j.example.2020.01.001-tdm-recovered.pdf"
    recovered_path.write_bytes(_fake_pdf(b"recovered"))

    item = {"key": "ITEM1", "data": {"DOI": "10.1016/j.example.2020.01.001", "title": "T"}}
    source = MagicMock(name="elsevier")
    source.name = "elsevier"
    source.fetch_pdf.return_value = (recovered_path, "elsevier (xml-fallback)")

    zot = MagicMock()
    args = _make_args(str(tmp_path))

    _run_api_cascade([item], [source], args, "2026-06-13", zot, _make_log_writer())

    zot.attach_pdf.assert_called_once_with("ITEM1", str(recovered_path))
    zot.update_tags.assert_called_once_with("ITEM1", add=[TDM_RECOVERED_TAG])


def test_does_not_tag_item_when_native_pdf_is_attached(tmp_path: Path) -> None:
    native_path = tmp_path / "10.1016_j.example.2020.01.002.pdf"
    native_path.write_bytes(_fake_pdf(b"native"))

    item = {"key": "ITEM2", "data": {"DOI": "10.1016/j.example.2020.01.002", "title": "T"}}
    source = MagicMock(name="elsevier")
    source.name = "elsevier"
    source.fetch_pdf.return_value = (native_path, "elsevier")

    zot = MagicMock()
    args = _make_args(str(tmp_path))

    _run_api_cascade([item], [source], args, "2026-06-13", zot, _make_log_writer())

    zot.attach_pdf.assert_called_once_with("ITEM2", str(native_path))
    zot.update_tags.assert_not_called()


def test_does_not_tag_on_dry_run(tmp_path: Path) -> None:
    recovered_path = tmp_path / "10.1016_j.example.2020.01.003-tdm-recovered.pdf"
    recovered_path.write_bytes(_fake_pdf(b"recovered"))

    item = {"key": "ITEM3", "data": {"DOI": "10.1016/j.example.2020.01.003", "title": "T"}}
    source = MagicMock(name="elsevier")
    source.name = "elsevier"
    source.fetch_pdf.return_value = (recovered_path, "elsevier (xml-fallback)")

    zot = MagicMock()
    args = _make_args(str(tmp_path))
    args.dry_run = True

    _run_api_cascade([item], [source], args, "2026-06-13", zot, _make_log_writer())

    zot.attach_pdf.assert_not_called()
    zot.update_tags.assert_not_called()
