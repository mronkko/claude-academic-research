"""A source-restricted run must not report items it never asked about.

Found live. `--sources wiley` over a 1,133-item queue printed `no PDF`
for the first 34 items on screen; the user checked and none of them were
Wiley — Taylor & Francis `10.1080`, BMJ `10.1136`, Cambridge `10.1017`,
Sage `10.1177`. `WileySource.fetch_pdf` returns None on a prefix
mismatch, and the orchestrator cannot tell that apart from "Wiley was
asked and had nothing", so ~970 items were announced as misses and
written to `pdf_attach_log.csv` as `skipped_no_pdf` plus a generic
`api_cascade` row in the failure log.

The exclusion path was never at risk — the cascade passes
`browser_pass_untried=True`, which keeps UNAVAILABLE off these items —
but recording a non-attempt as a failure is the defect this repo spent a
release removing everywhere else.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import enrich_pdfs
import pytest
from fetchers.sciencedirect import ScienceDirectSource
from fetchers.springer import SpringerSource
from fetchers.wiley import WileySource

# --- the predicate ----------------------------------------------------


@pytest.mark.parametrize(
    ("cls", "mine", "not_mine"),
    [
        (WileySource, "10.1111/j.1467-8543.1982.tb00100.x", "10.1080/x"),
        (SpringerSource, "10.1007/s10551-016-3079-9", "10.1136/bmj.m454"),
        (ScienceDirectSource, "10.1016/j.jom.2016.04.002", "10.1017/x"),
    ],
)
def test_a_prefix_filtering_source_knows_its_own_remit(
    cls, mine: str, not_mine: str,
) -> None:
    src = cls(None, None)
    assert src.handles_doi(mine)
    assert not src.handles_doi(not_mine)


def test_an_unfiltered_source_handles_anything() -> None:
    """Crossref, OpenAlex, Unpaywall and friends take any DOI, so the
    filter must be a no-op for them — otherwise the full cascade would
    start skipping items."""
    from fetchers.crossref import CrossrefSource

    src = CrossrefSource(None, None)
    assert src.handles_doi("10.1080/anything")
    assert src.handles_doi("")


def test_case_does_not_decide_whether_a_source_applies() -> None:
    """DOIs arrive from Zotero in whatever case the publisher used, and
    the cascade lower-cases only later."""
    assert WileySource(None, None).handles_doi("10.1111/J.1467-8543.X")


# --- the cascade ------------------------------------------------------


class _Rows:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def writerow(self, row: dict) -> None:
        self.rows.append(row)


def _item(key: str, doi: str) -> dict:
    return {"key": key, "data": {"DOI": doi, "title": f"T{key}",
                                 "itemType": "journalArticle"}}


def _args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        cache_dir=str(tmp_path), workers=2, dry_run=True,
        failure_log_csv="", no_check_text=True,
    )


def test_items_no_selected_source_covers_are_not_logged(
    tmp_path, capsys,
) -> None:
    """The live case: a Wiley-only run over a mixed queue."""
    rows = _Rows()
    items = [
        _item("W1", "10.1111/j.1467-8543.1982.tb00100.x"),
        _item("T1", "10.1080/1360080x.2016.1211976"),
        _item("B1", "10.1136/bmj.m454"),
        _item("C1", "10.1017/s0305741023001467"),
    ]
    enrich_pdfs._run_api_cascade(
        items, [WileySource(None, None)], _args(tmp_path),
        "2026-08-18", None, rows,
    )

    logged = {r["item_key"] for r in rows.rows}
    assert "T1" not in logged and "B1" not in logged and "C1" not in logged, (
        f"non-Wiley items were logged as a result: {sorted(logged)}"
    )
    out = capsys.readouterr().out
    assert "3 of 4 items have a DOI no selected source covers" in out, out


def test_the_full_cascade_still_considers_everything(tmp_path) -> None:
    """The guard must be invisible when nothing is restricted — a
    regression here would silently shrink every normal run."""
    from fetchers.crossref import CrossrefSource

    rows = _Rows()
    items = [_item("T1", "10.1080/x"), _item("B1", "10.1136/y")]
    enrich_pdfs._run_api_cascade(
        items, [CrossrefSource(None, None), WileySource(None, None)],
        _args(tmp_path), "2026-08-18", None, rows,
    )
    assert {r["item_key"] for r in rows.rows} == {"T1", "B1"}
