"""What the end-of-run browser summary is allowed to claim.

`--log-csv` is cumulative across runs, so counting all of it answered a
different question than the one the summary asks. A live 14-item batch
that attached nothing ended with "Done. 393 of 14 queued items now have
a PDF attached", and because `queued - attached` was negative the
"still missing" line never printed and the `run_done` progress event
carried `missing: 0`.

The print is cosmetic; the event is not. An unattended caller reading
`--progress-json` was told a total failure had left nothing outstanding.
"""

from __future__ import annotations

import csv

import enrich_pdfs
import pytest


@pytest.fixture
def log_with_history(tmp_path):
    """A log holding one earlier run's attaches plus one from this run."""
    path = tmp_path / "pdf_attach_log.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["item_key", "status"])
        w.writeheader()
        for key in ("OLD00001", "OLD00002", "OLD00003"):
            w.writerow({"item_key": key, "status": "attached"})
        w.writerow({"item_key": "NEW00001", "status": "attached"})
    return str(path)


def _run(monkeypatch, args_log, queued_keys):
    events = []
    from fetchers.browser import interaction
    monkeypatch.setattr(interaction, "report_progress", events.append)

    class Args:
        log_csv = args_log

    enrich_pdfs._print_browser_summary(Args(), queued_keys)
    return events[-1]


def test_summary_counts_only_what_this_run_queued(
    monkeypatch, capsys, log_with_history,
) -> None:
    event = _run(monkeypatch, log_with_history, ["NEW00001", "NEW00002"])

    assert event["queued"] == 2
    assert event["attached"] == 1
    assert event["missing"] == 1
    out = capsys.readouterr().out
    assert "1 of 2 queued items" in out
    assert "393" not in out


def test_a_run_that_attached_nothing_says_so(
    monkeypatch, capsys, log_with_history,
) -> None:
    """The regression proper: silence here read as success."""
    event = _run(monkeypatch, log_with_history, ["MISS0001", "MISS0002"])

    assert event["attached"] == 0
    assert event["missing"] == 2
    out = capsys.readouterr().out
    assert "0 of 2 queued items" in out
    assert "2 still missing" in out


def test_queued_keys_are_matched_case_insensitively(
    monkeypatch, capsys, log_with_history,
) -> None:
    """`load_done_keys` lower-cases; Zotero keys are upper-case.

    Intersecting the raw forms is empty for every run, which would swap
    the over-count for an equally wrong under-count.
    """
    event = _run(monkeypatch, log_with_history, ["NEW00001"])

    assert event["attached"] == 1
    assert event["missing"] == 0


def test_history_alone_never_inflates_the_count(
    monkeypatch, capsys, log_with_history,
) -> None:
    """Items attached by earlier runs are not this run's work."""
    event = _run(monkeypatch, log_with_history, ["NEW00001"])

    # OLD00001..3 are in the log and attached, but were not queued here.
    assert event["queued"] == 1
    assert event["attached"] == 1


def test_rows_still_in_the_write_buffer_are_counted(monkeypatch, capsys, tmp_path) -> None:
    """The summary reads a file this run is still writing.

    A live 17-item run attached 5 and reported "Done. 0 of 17": the
    handle was open and buffered, so those rows were not yet on disk.
    The whole-log count had masked it — a few missing rows out of 393
    change nothing visible.
    """
    import csv as _csv

    path = tmp_path / "pdf_attach_log.csv"
    fh = open(path, "w", newline="", encoding="utf-8")
    w = _csv.DictWriter(fh, fieldnames=["item_key", "status"])
    w.writeheader()
    w.writerow({"item_key": "NEW00001", "status": "attached"})
    # Deliberately not flushed — this is the live shape.

    events = []
    from fetchers.browser import interaction
    monkeypatch.setattr(interaction, "report_progress", events.append)

    class Args:
        log_csv = str(path)

    try:
        enrich_pdfs._print_browser_summary(
            Args(), ["NEW00001", "NEW00002"], log_fh=fh)
    finally:
        fh.close()

    assert events[-1]["attached"] == 1, (
        "the just-attached row was still in the write buffer and got "
        "counted as a miss"
    )
    assert "1 of 2 queued items" in capsys.readouterr().out
