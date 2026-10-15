"""Scopus and WoS scope clauses must bind to the whole user query.

Both searchers append `AND ISSN(...)` / `AND IS=(...)` and a year limit
to the user's core query. Both engines bind `AND NOT` and a top-level
`OR` more loosely than the `AND` that joins the scope clauses, so an
unparenthesised `a OR b` became `a OR (b AND scope)` — every `a` record
from every journal and year came back. The core is now wrapped.

An empty `JOURNALS` used to produce `ISSN()` / `IS=()`, a 400 from the
API; it now means "unscoped" and the clause is omitted.

WoS also files many DOIs only under `xref_doi` and online-only journals
only under `eissn`; both were dropped.
"""

from __future__ import annotations

from searchers import ScopusSearch, WosSearch
from searchers.base import SearchContext


def _ctx(issns: list[str]) -> SearchContext:
    return SearchContext(from_year=2015, to_year=2018, issns=issns)


def test_scopus_wraps_the_core_before_the_scope_clauses() -> None:
    q = ScopusSearch()._full_query(
        "TITLE-ABS-KEY(a) OR TITLE-ABS-KEY(b) AND NOT TITLE(c)",
        _ctx(["0021-9010", "0001-8392"]),
    )
    assert q.startswith("(TITLE-ABS-KEY(a) OR TITLE-ABS-KEY(b) AND NOT TITLE(c)) AND ")
    assert "AND ISSN(0021-9010 OR 0001-8392) AND" in q
    assert q.endswith("PUBYEAR > 2014 AND PUBYEAR < 2019")


def test_wos_wraps_the_core_before_the_scope_clauses() -> None:
    q = WosSearch()._full_query("TS=(a) OR TS=(b) NOT TI=(c)", _ctx(["0021-9010"]))
    assert q == "(TS=(a) OR TS=(b) NOT TI=(c)) AND IS=(0021-9010) AND PY=2015-2018"


def test_scopus_empty_journals_omits_the_issn_clause() -> None:
    q = ScopusSearch()._full_query("TITLE(a)", _ctx([]))
    assert "ISSN(" not in q
    assert q == "(TITLE(a)) AND PUBYEAR > 2014 AND PUBYEAR < 2019"


def test_wos_empty_journals_omits_the_is_clause() -> None:
    q = WosSearch()._full_query("TS=(a)", _ctx(["", "  "]))
    assert "IS=" not in q
    assert q == "(TS=(a)) AND PY=2015-2018"


def _wos_rec(identifiers: list[dict]) -> dict:
    return {
        "UID": "WOS:1",
        "static_data": {"summary": {"titles": {"title": []}}},
        "dynamic_data": {
            "cluster_related": {"identifiers": {"identifier": identifiers}},
        },
    }


def test_wos_reads_xref_doi_and_eissn_when_plain_ones_are_absent() -> None:
    row = WosSearch()._extract_record(_wos_rec([
        {"type": "xref_doi", "value": "10.1002/SMJ.1"},
        {"type": "eissn", "value": "1097-0266"},
    ]), "q1")
    assert row["doi"] == "10.1002/smj.1"
    assert row["issn"] == "1097-0266"


def test_wos_prefers_plain_doi_and_print_issn() -> None:
    row = WosSearch()._extract_record(_wos_rec([
        {"type": "eissn", "value": "1097-0266"},
        {"type": "xref_doi", "value": "10.9/xref"},
        {"type": "doi", "value": "10.9/plain"},
        {"type": "issn", "value": "0143-2095"},
    ]), "q1")
    assert row["doi"] == "10.9/plain"
    assert row["issn"] == "0143-2095"
