"""A source that holds the record but will not show it.

WoS answered `RecordsFound: 1` with an empty `records` for a record
outside the subscription's entitlement. That is not evidence the article
has no abstract, and `not_found` is exactly what licenses that ruling
downstream; nor is it a transport failure. So it gets its own status.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import enrich_abstracts
from fetchers.base import AbstractWithheld

ITEM = {"data": {"DOI": "10.1/x", "title": "T"}}


class _Src:
    def __init__(self, name, result):
        self.name, self.result = name, result

    def fetch_abstract(self, doi, **kw):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_withheld_moves_on_and_is_its_own_status() -> None:
    got = enrich_abstracts._try_cascade(ITEM, [
        _Src("wos", AbstractWithheld("record exists but is not viewable")),
        _Src("crossref", None),
    ], "/tmp")
    assert not got.found
    assert got.withheld == [("wos", "record exists but is not viewable")]
    assert got.status() == "withheld"
    assert not got.confirmed_absent
    assert "wos: record exists but is not viewable" in got.detail()


def test_a_later_source_still_finds_it() -> None:
    got = enrich_abstracts._try_cascade(ITEM, [
        _Src("wos", AbstractWithheld("not viewable")),
        _Src("crossref", "We study labor unions across firms in twelve countries."),
    ], "/tmp")
    assert got.found and got.source == "crossref"


def test_an_error_still_outranks_withheld() -> None:
    got = enrich_abstracts._try_cascade(ITEM, [
        _Src("wos", AbstractWithheld("not viewable")),
        _Src("scopus", RuntimeError("HTTP 500")),
    ], "/tmp")
    assert got.status() == "lookup_failed"


def test_plain_absence_is_still_not_found() -> None:
    got = enrich_abstracts._try_cascade(ITEM, [_Src("crossref", None)], "/tmp")
    assert got.status() == "not_found"


def test_wos_raises_withheld_for_an_unviewable_record() -> None:
    import pytest
    from fetchers.wos import WosSource

    resp = MagicMock(status_code=200)
    resp.json.return_value = {
        "QueryResult": {"RecordsFound": 1}, "Data": {"Records": {"records": ""}},
    }
    src = WosSource.__new__(WosSource)
    src.http = MagicMock()
    src.http.get.return_value = resp
    with pytest.raises(AbstractWithheld):
        src._fetch_expanded("10.18311/sdmimd/2019/y", None, "key")


def test_every_log_row_carries_a_timestamp(tmp_path) -> None:
    """Two runs on one day tied on the date-only run_date."""
    import csv

    fh, writer = enrich_abstracts._open_log(str(tmp_path / "log.csv"))
    writer.writerow({"run_date": "2026-09-23", "item_key": "A", "status": "updated"})
    fh.close()
    row = next(csv.DictReader(open(tmp_path / "log.csv")))
    assert row["ran_at"].startswith("20") and "T" in row["ran_at"]
    assert row["ran_at"][-6] in "+-" or row["ran_at"].endswith("Z")
