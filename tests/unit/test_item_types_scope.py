"""`--item-types` admits book chapters and books to the PDF pass.

Requested from the phase2 run, 2026-09-25: enrich_pdfs skipped every
non-journalArticle item ("22 key(s) resolved to non-journalArticle items
and were skipped"), although the review holds chapters with DOIs its
library licenses (Palgrave 10.1057/9780230522763_2, Elgar
10.4337/9781847207203.00006, Springer 10.1007/978-3-319-40931-3_7).

Admitting a type has a second half that is easy to miss: the failure
log classifies bookSection and book as OUT_OF_SCOPE by default, which
reads as "exclude, do not retry" (FE2). A chapter the user opted in and
the fetch then missed must not come out of the log as an exclusion.
"""

from __future__ import annotations

import csv

import enrich_pdfs
import pdf_fetch_log
import pytest


def _item(key: str, item_type: str) -> dict:
    return {"key": key, "data": {"itemType": item_type, "key": key}}


def test_default_scope_is_unchanged() -> None:
    fetched = [_item("A", "journalArticle"), _item("C", "bookSection")]
    kept, skipped = enrich_pdfs.select_requested_articles(fetched, {"A", "C"})
    assert [i["key"] for i in kept] == ["A"]
    assert skipped == 1


def test_admitted_types_are_selected() -> None:
    fetched = [_item("A", "journalArticle"), _item("C", "bookSection"),
               _item("B", "book"), _item("T", "thesis")]
    kept, skipped = enrich_pdfs.select_requested_articles(
        fetched, {"A", "C", "B", "T"},
        item_types=("journalArticle", "bookSection", "book"),
    )
    assert sorted(i["key"] for i in kept) == ["A", "B", "C"]
    assert skipped == 1


def test_the_flag_parses_and_rejects_unknown_types() -> None:
    parser = enrich_pdfs._build_parser()
    args = parser.parse_args(["--item-types", "journalArticle, bookSection"])
    assert args.item_types == ("journalArticle", "bookSection")
    assert parser.parse_args([]).item_types == ("journalArticle",)
    with pytest.raises(SystemExit):
        parser.parse_args(["--item-types", "bookChapter"])


def test_admitted_types_leave_the_out_of_scope_set() -> None:
    scope = pdf_fetch_log.out_of_scope_types(("journalArticle", "bookSection"))
    assert "bookSection" not in scope
    assert "book" in scope and "thesis" in scope


def test_an_admitted_chapter_is_not_logged_out_of_scope(tmp_path) -> None:
    log = tmp_path / "pdf_fetch_log.csv"
    scope = pdf_fetch_log.out_of_scope_types(("journalArticle", "bookSection"))
    cause = pdf_fetch_log.log_failure(
        log, item_key="C", doi="10.1057/x", item_type="bookSection",
        http_status=403, scope_types=scope,
    )
    assert cause == pdf_fetch_log.FailureCause.ACCESS_BLOCKED


class _Miss:
    name = "miss"

    def fetch_pdf(self, doi, cache_dir=None):
        return None


@pytest.mark.parametrize("scope,want", [
    (None, "OUT_OF_SCOPE"),
    (pdf_fetch_log.out_of_scope_types(("journalArticle", "bookSection")),
     None),
])
def test_the_api_cascade_honours_the_runs_scope(tmp_path, scope, want) -> None:
    log = tmp_path / "pdf_fetch_log.csv"
    item = {"key": "C", "data": {"itemType": "bookSection",
                                 "DOI": "10.1057/9780230522763_2"}}
    enrich_pdfs._try_cascade(item, [_Miss()], str(tmp_path),
                             failure_log_path=str(log), scope_types=scope)
    rows = list(csv.DictReader(log.open()))
    assert len(rows) == 1
    if want:
        assert rows[0]["cause"] == want
    else:
        assert rows[0]["cause"] != "OUT_OF_SCOPE"
