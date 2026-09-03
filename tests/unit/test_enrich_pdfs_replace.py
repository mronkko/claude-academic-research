"""`--replace`: repair a corpus without deleting the only copy first.

An item that already carries an attachment drops out at `pdf_map()`
before any fetch is attempted, and the run report tells the user to
delete the attachment to get a new one. That makes a legitimate re-fetch
destructive by construction: you have to destroy the file you have before
finding out whether a replacement will arrive. For a corpus repair — the
concrete case being ~130 recovered PDFs written by a superseded XML
transformation — that is the difference between a safe operation and a
gamble repeated a hundred times.

`--replace` re-admits those items and swaps on success only: the new PDF
is fetched and attached first, and the old attachment is deleted after
that attach returns. A fetch that fails leaves the library exactly as it
was.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import enrich_pdfs

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "pipelines" / "enrich_pdfs.py"


def _item(key: str) -> dict:
    return {"key": key, "data": {"DOI": f"10.1/{key}", "title": key}}


# ---------------------------------------------------------------------------
# Selection: which items a run is allowed to touch
# ---------------------------------------------------------------------------


def test_items_with_a_real_pdf_are_skipped_by_default() -> None:
    items = [_item("A"), _item("B")]
    pdf_map = {"A": (True, []), "B": (False, [])}
    to_process, stubs, targets = enrich_pdfs._partition_by_attachment(
        items, pdf_map, {}, replace=False,
    )
    assert [it["key"] for it in to_process] == ["B"]
    assert targets == {}
    assert stubs == []


def test_replace_readmits_items_that_already_have_a_pdf() -> None:
    items = [_item("A"), _item("B")]
    pdf_map = {"A": (True, []), "B": (False, [])}
    real_map = {"A": ["OLDATT01"]}
    to_process, _stubs, targets = enrich_pdfs._partition_by_attachment(
        items, pdf_map, real_map, replace=True,
    )
    assert [it["key"] for it in to_process] == ["A", "B"]
    assert targets == {"A": ["OLDATT01"]}


def test_replace_records_every_existing_attachment_on_an_item() -> None:
    """An item can already hold more than one; a swap replaces them all."""
    items = [_item("A")]
    _to_process, _stubs, targets = enrich_pdfs._partition_by_attachment(
        items, {"A": (True, [])}, {"A": ["OLD1", "OLD2"]}, replace=True,
    )
    assert targets == {"A": ["OLD1", "OLD2"]}


def test_replace_records_nothing_for_an_item_with_no_pdf() -> None:
    """Nothing to swap — the ordinary fetch path, not a replacement."""
    _to_process, _stubs, targets = enrich_pdfs._partition_by_attachment(
        [_item("B")], {"B": (False, [])}, {}, replace=True,
    )
    assert targets == {}


def test_stubs_are_collected_for_deletion_either_way() -> None:
    for replace in (False, True):
        _to_process, stubs, _targets = enrich_pdfs._partition_by_attachment(
            [_item("A")], {"A": (False, ["STUB1", "STUB2"])}, {}, replace=replace,
        )
        assert stubs == ["STUB1", "STUB2"]


# ---------------------------------------------------------------------------
# The swap: delete only after the replacement is attached
# ---------------------------------------------------------------------------


def _attach_env(tmp_path):
    pdf = tmp_path / "new.pdf"
    pdf.write_bytes(b"%PDF-1.4\n" + b"0" * 2000 + b"\n%%EOF\n")
    zot = MagicMock()
    zot.attach_pdf.return_value = "NEWATT01"
    return zot, pdf, MagicMock()


def test_old_attachment_is_deleted_after_a_successful_attach(
    tmp_path, monkeypatch,
) -> None:
    zot, pdf, log_writer = _attach_env(tmp_path)
    monkeypatch.setitem(enrich_pdfs._REPLACE_TARGETS, "A", ["OLDATT01"])

    ok = enrich_pdfs._attach_and_log(
        zot, log_writer, run_date="2026-09-03", item_key="A",
        doi="10.1/A", title="A", source="test", pdf_path=pdf,
        check_text=False,
    )
    assert ok
    zot.delete_item.assert_called_once_with("OLDATT01")


def test_nothing_is_deleted_when_the_attach_fails(tmp_path, monkeypatch) -> None:
    """The property that makes this safe to run over a whole corpus."""
    zot, pdf, log_writer = _attach_env(tmp_path)
    zot.attach_pdf.side_effect = RuntimeError("upload rejected")
    monkeypatch.setitem(enrich_pdfs._REPLACE_TARGETS, "A", ["OLDATT01"])

    ok = enrich_pdfs._attach_and_log(
        zot, log_writer, run_date="2026-09-03", item_key="A",
        doi="10.1/A", title="A", source="test", pdf_path=pdf,
        check_text=False,
    )
    assert not ok
    zot.delete_item.assert_not_called()


def test_nothing_is_deleted_when_the_pdf_is_rejected_as_corrupt(
    tmp_path, monkeypatch,
) -> None:
    zot, _pdf, log_writer = _attach_env(tmp_path)
    truncated = tmp_path / "bad.pdf"
    truncated.write_bytes(b"%PDF-1.4\n" + b"0" * 2000)  # no %%EOF
    monkeypatch.setitem(enrich_pdfs._REPLACE_TARGETS, "A", ["OLDATT01"])

    ok = enrich_pdfs._attach_and_log(
        zot, log_writer, run_date="2026-09-03", item_key="A",
        doi="10.1/A", title="A", source="test", pdf_path=truncated,
        check_text=False,
    )
    assert not ok
    zot.delete_item.assert_not_called()


def test_the_newly_attached_file_is_never_the_one_deleted(
    tmp_path, monkeypatch,
) -> None:
    """A stale registry naming the new attachment would delete the
    replacement and leave the item with nothing."""
    zot, pdf, log_writer = _attach_env(tmp_path)
    monkeypatch.setitem(
        enrich_pdfs._REPLACE_TARGETS, "A", ["NEWATT01", "OLDATT01"],
    )

    enrich_pdfs._attach_and_log(
        zot, log_writer, run_date="2026-09-03", item_key="A",
        doi="10.1/A", title="A", source="test", pdf_path=pdf,
        check_text=False,
    )
    deleted = [c.args[0] for c in zot.delete_item.call_args_list]
    assert deleted == ["OLDATT01"]


def test_an_item_with_no_replacement_target_deletes_nothing(tmp_path) -> None:
    zot, pdf, log_writer = _attach_env(tmp_path)
    enrich_pdfs._attach_and_log(
        zot, log_writer, run_date="2026-09-03", item_key="UNRELATED",
        doi="10.1/U", title="U", source="test", pdf_path=pdf,
        check_text=False,
    )
    zot.delete_item.assert_not_called()


def test_a_failed_delete_does_not_fail_the_attach(tmp_path, monkeypatch) -> None:
    """The new PDF is on the item; a leftover old attachment is untidy,
    not a lost fetch. Reporting failure here would send the item back
    into the retry population and attach a third copy."""
    zot, pdf, log_writer = _attach_env(tmp_path)
    zot.delete_item.side_effect = RuntimeError("403")
    monkeypatch.setitem(enrich_pdfs._REPLACE_TARGETS, "A", ["OLDATT01"])

    assert enrich_pdfs._attach_and_log(
        zot, log_writer, run_date="2026-09-03", item_key="A",
        doi="10.1/A", title="A", source="test", pdf_path=pdf,
        check_text=False,
    )


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_replace_flag_appears_in_help() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True, text=True, check=True,
    )
    assert "--replace" in result.stdout
