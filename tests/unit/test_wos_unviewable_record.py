"""WoS expanded API: a record that exists but is not viewable.

Live on 2026-09-22 for DOI 10.18311/sdmimd/2019/y, both the DOI query and
the title fallback answered `RecordsFound: 1` with `"records": ""` — an
empty string where the record object belongs, which is what a record
outside the subscription's entitlement looks like. The parser called
`.get` on that string and every lookup raised AttributeError, logged as
lookup_failed on every run although the API had answered.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from fetchers.wos import WosSource


def _source(payload: dict) -> WosSource:
    resp = MagicMock(status_code=200)
    resp.json.return_value = payload
    src = WosSource.__new__(WosSource)
    src.http = MagicMock()
    src.http.get.return_value = resp
    return src


def test_found_but_not_viewable_is_no_record_not_a_crash() -> None:
    src = _source({
        "QueryResult": {"QueryID": 91, "RecordsFound": 1},
        "Data": {"Records": {"records": ""}},
    })
    assert src._expanded_search("DO=(10.18311/sdmimd/2019/y)", {}, count=1) == []
    assert src._fetch_expanded(
        "10.18311/sdmimd/2019/y",
        "Employee Benefits and its Effect on Productivity", "key",
    ) is None
