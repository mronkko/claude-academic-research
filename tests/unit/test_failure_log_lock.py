"""Every writer of pdf_fetch_log.csv holds `_FAILURE_LOG_LOCK`.

`_attach_and_log` writes the file from lane threads under the lock;
`_log_browser_failure` wrote it without, and the upsert is
read-modify-replace, so the two could drop each other's rows.
"""

from __future__ import annotations

from types import SimpleNamespace

import enrich_pdfs
import pdf_fetch_log


def test_browser_failure_rows_are_written_under_the_lock(monkeypatch, tmp_path) -> None:
    held: list[bool] = []

    def fake_log_failure(*a, **k):
        held.append(enrich_pdfs._FAILURE_LOG_LOCK.locked())
        return pdf_fetch_log.FailureCause.ACCESS_BLOCKED

    monkeypatch.setattr(pdf_fetch_log, "log_failure", fake_log_failure)
    args = SimpleNamespace(
        failure_log_csv=str(tmp_path / "f.csv"), item_types=("journalArticle",),
    )
    enrich_pdfs._log_browser_failure(
        args, {"item_key": "K1", "doi": "10.1/x"}, source="sage",
    )
    assert held == [True]
