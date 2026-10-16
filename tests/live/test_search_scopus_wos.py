"""Live: the Scopus and WoS searchers each return in-scope rows.

Until this file, their only live coverage was the full mini review
(`test_mini_slr.py`), which needs a Zotero group and an LLM key as well,
so a broken search key or a changed response shape went unnoticed unless
that whole run was attempted (BACKLOG P25).

Each test is one small query: one phrase, one journal, one year, so it
costs a page of results against the institution's quota. A missing key
skips, with the searcher's own explanation of which credential is
absent; a failing search fails.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from searchers import ScopusSearch, WosSearch
from searchers.base import SearchContext

pytestmark = pytest.mark.live

#: Journal of Applied Psychology, 2016: "job satisfaction" in titles and
#: abstracts is common enough there to be reliably non-empty, and one
#: journal-year keeps the result to a page.
ISSN = "0021-9010"
YEAR = 2016


def _config() -> SimpleNamespace:
    return SimpleNamespace(QUERY_DEFS=[(
        "live", 'TITLE-ABS-KEY("job satisfaction")', 'TS=("job satisfaction")',
    )])


def _ctx() -> SearchContext:
    return SearchContext(from_year=YEAR, to_year=YEAR, issns=[ISSN],
                         journal_titles=["Journal of Applied Psychology"])


def _run(source) -> list[dict]:
    ctx = _ctx()
    missing = source.credentials_error(ctx)
    if missing:
        pytest.skip(missing)
    return source.run(_config(), ctx)


def _assert_in_scope(rows: list[dict], db: str) -> None:
    assert rows, f"{db} returned no rows for a query that always has some"
    assert {r["db"] for r in rows} == {db}
    assert {r["query"] for r in rows} == {"live"}
    wrong_year = [r["year"] for r in rows if r["year"] != str(YEAR)]
    assert not wrong_year, f"{db} rows outside {YEAR}: {wrong_year[:5]}"
    assert all(r["title"] for r in rows), f"{db} returned a row with no title"
    # Most rows carry a DOI; the import step dedups on it.
    with_doi = sum(1 for r in rows if r["doi"].startswith("10."))
    assert with_doi >= len(rows) // 2, f"{db}: only {with_doi}/{len(rows)} rows have a DOI"


def test_scopus_search_returns_rows_in_scope() -> None:
    pytest.importorskip("pybliometrics", reason="live test requires `pybliometrics`")
    rows = _run(ScopusSearch())
    _assert_in_scope(rows, "scopus")
    issns = {r["issn"].replace("-", "") for r in rows if r["issn"]}
    assert issns <= {ISSN.replace("-", "")}, f"Scopus rows from other journals: {issns}"


def test_wos_search_returns_rows_in_scope() -> None:
    rows = _run(WosSearch())
    _assert_in_scope(rows, "wos")
