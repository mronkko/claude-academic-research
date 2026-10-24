"""What `enrich_pdfs.main()` does with the stub attachments `pdf_map` names.

A4: the cleanup ran before the `--dry-run` and `--plan` guards and used
pyzotero's permanent `delete_item`, so a read-only run destroyed data —
and since `pdf_map` judged stubs by md5 alone, the data it destroyed
included linked-file PDFs with their annotations. Which attachments are
stubs is tested in `test_zotero_io.py`; this is about what main() does
with them: trash, never delete, and nothing at all when told not to write.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import enrich_pdfs
import pytest
import zotero_io


@pytest.fixture
def zot(monkeypatch, tmp_path):
    fake = MagicMock()
    fake.journal_articles.return_value = [
        {"key": "ITEM0001", "data": {"DOI": "10.1/x", "itemType": "journalArticle"}},
    ]
    # Already has a real PDF, plus one stub: nothing left to fetch, so
    # main() returns right after the cleanup.
    fake.pdf_map.return_value = {"ITEM0001": (True, ["STUB0001"])}
    fake.real_pdf_md5_map.return_value = {}

    monkeypatch.setattr(zotero_io.ZoteroClient, "from_args", lambda args: fake)
    monkeypatch.setattr(enrich_pdfs, "_load_config",
                        lambda: SimpleNamespace(crossref_mailto=""))
    monkeypatch.setattr(enrich_pdfs.http_client, "build_session",
                        lambda **k: MagicMock())
    monkeypatch.setattr(enrich_pdfs, "_print_run_report", lambda *a, **k: None)
    monkeypatch.delenv("LIBRARY_ACTIVE", raising=False)
    monkeypatch.setattr(sys, "argv", [
        "enrich_pdfs.py", "--user",
        "--cache-dir", str(tmp_path / "cache"),
        "--log-csv", str(tmp_path / "log.csv"),
    ])
    return fake


def test_stubs_are_trashed_not_deleted(zot, capsys) -> None:
    assert enrich_pdfs.main() == 0
    zot.trash_item.assert_called_once_with("STUB0001")
    zot.delete_item.assert_not_called()


@pytest.mark.parametrize("flags", [
    ["--dry-run"],
    # --plan alone is a usage error (it only plans the browser pass).
    ["--plan", "--sources", "browser"],
])
def test_read_only_runs_leave_stubs_alone(zot, flags, capsys) -> None:
    sys.argv.extend(flags)
    assert enrich_pdfs.main() == 0
    zot.trash_item.assert_not_called()
    zot.delete_item.assert_not_called()
    assert "1 stub attachment(s) would be moved to the trash" in capsys.readouterr().out
