"""Live: the PubMed searcher returns parseable rows from NCBI E-utilities.

One small query (an ear-prosthesis term in one year) so it costs a page
or two of results. PubMed needs no key, so this never skips for
credentials; a failing search fails.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from searchers import PubMedSearch
from searchers.base import SearchContext

pytestmark = pytest.mark.live

YEAR = 2021


def test_pubmed_search_returns_rows_in_window() -> None:
    config = SimpleNamespace(PUBMED_QUERY_DEFS=[(
        "live", '"auricular prosthesis"[tiab] AND scanning[tiab]',
    )])
    ctx = SearchContext(from_year=YEAR, to_year=YEAR, issns=[])
    source = PubMedSearch()
    assert source.credentials_error(ctx) is None
    rows = source.run(config, ctx)
    assert rows, "expected at least one PubMed record"
    for row in rows:
        assert row["db"] == "pubmed"
        assert row["title"]
        assert row["year"] == str(YEAR)
