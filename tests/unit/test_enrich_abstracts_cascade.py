"""Absence and failure must not share one log status.

`_try_cascade` used to return a bare `None` whenever it came away
without an abstract, and the caller wrote `status="not_found"` for every
such item. Three different facts collapsed into that one label: the item
carried no DOI so nothing was ever looked up; every source raised, so
the question went unanswered; and every source answered cleanly and none
had an abstract. Only the third is genuine absence.

The distinction is not cosmetic. A caller reporting how many records in
a corpus genuinely have no abstract — a real finding in a systematic
review, since an abstract-less record cannot be screened by a human or a
model — would silently count broken lookups as confirmed absences and
overstate it. The exception text existed only in terminal scrollback, so
nothing in the log could be used to correct the number after the fact.

These tests pin the three outcomes apart and assert the reason survives
into the log's `detail` column.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from enrich_abstracts import CascadeResult, _try_cascade
from log_schemas import ABSTRACT_FETCH_FIELDS

DOI = "10.1177/0149206320916987"


def _item(doi: str = DOI, title: str = "A Title") -> dict:
    return {"key": "ABCD1234", "data": {"DOI": doi, "title": title}}


def _source(name: str, *, text: str = "", raises: Exception | None = None):
    src = MagicMock()
    src.name = name
    if raises is not None:
        src.fetch_abstract.side_effect = raises
    else:
        src.fetch_abstract.return_value = text
    return src


def test_hit_reports_the_source_that_supplied_it(tmp_path):
    sources = [_source("crossref"), _source("openalex", text="An abstract.")]

    result = _try_cascade(_item(), sources, str(tmp_path))

    assert result.found
    assert result.abstract == "An abstract."
    assert result.source == "openalex"
    assert not result.confirmed_absent


def test_all_sources_answering_cleanly_is_confirmed_absence(tmp_path):
    sources = [_source("crossref"), _source("openalex")]

    result = _try_cascade(_item(), sources, str(tmp_path))

    assert not result.found
    assert result.confirmed_absent
    assert result.errors == []
    assert result.asked == ["crossref", "openalex"]
    assert "crossref" in result.detail()


def test_every_source_raising_is_not_confirmed_absence(tmp_path):
    sources = [
        _source("crossref", raises=TimeoutError("read timed out")),
        _source("openalex", raises=ValueError("bad payload")),
    ]

    result = _try_cascade(_item(), sources, str(tmp_path))

    assert not result.found
    assert not result.confirmed_absent, (
        "lookups that all failed cannot establish that the abstract is absent"
    )
    assert [name for name, _ in result.errors] == ["crossref", "openalex"]


def test_one_source_raising_taints_the_absence_verdict(tmp_path):
    """A clean 'no' from one source does not license 'confirmed absent'
    when another source never answered — the silent source may have had it."""
    sources = [
        _source("crossref", raises=TimeoutError("read timed out")),
        _source("openalex"),
    ]

    result = _try_cascade(_item(), sources, str(tmp_path))

    assert not result.found
    assert not result.confirmed_absent
    assert result.asked == ["openalex"]
    assert len(result.errors) == 1


def test_missing_doi_short_circuits_without_claiming_absence(tmp_path):
    sources = [_source("crossref")]

    result = _try_cascade(_item(doi=""), sources, str(tmp_path))

    assert not result.found
    assert not result.confirmed_absent
    assert result.asked == []
    sources[0].fetch_abstract.assert_not_called()


def test_not_implemented_source_is_counted_neither_way(tmp_path):
    """A fetcher that does not offer abstracts at all is not evidence."""
    sources = [_source("wos", raises=NotImplementedError())]

    result = _try_cascade(_item(), sources, str(tmp_path))

    assert result.errors == []
    assert result.asked == []
    assert not result.confirmed_absent, (
        "a cascade where no source actually answered proves nothing"
    )


def test_detail_carries_the_exception_text(tmp_path):
    sources = [_source("crossref", raises=TimeoutError("read timed out"))]

    detail = _try_cascade(_item(), sources, str(tmp_path)).detail()

    assert "crossref" in detail
    assert "read timed out" in detail
    assert "TimeoutError" in detail


def test_log_schema_carries_detail():
    assert "detail" in ABSTRACT_FETCH_FIELDS, (
        "without a detail column the failure reason lives only in scrollback"
    )


def test_empty_result_is_not_confirmed_absence():
    assert not CascadeResult().confirmed_absent
