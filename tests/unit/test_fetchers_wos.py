"""Unit tests for fetchers/wos.py and fetchers/_title_match.py.

All tests monkey-patch `session.get` to return canned responses — no
real Clarivate API calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fetchers import WosSource
from fetchers._title_match import item_meta, matches, normalise, strip_html


@pytest.fixture(autouse=True)
def _no_wos_env(monkeypatch):
    """Prevent the test runner's real WoS env vars from leaking into
    tests — WosSource falls through to `os.environ` when config fields
    are empty, which would pick up the developer's live key and change
    test behaviour."""
    monkeypatch.delenv("WOS_API_KEY_EXTENDED", raising=False)
    monkeypatch.delenv("WOS_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# _title_match helpers
# ---------------------------------------------------------------------------


def test_strip_html_removes_italic_and_sub_tags() -> None:
    """Tags are replaced with spaces, not elided — so <b>foo</b><b>bar</b>
    doesn't become 'foobar'. Confirm both the tag removal and the
    intentional space behaviour."""
    s = "Putting Framing in Perspective: <i>A Review</i> and <sub>sub</sub>text"
    out = strip_html(s)
    assert "<i>" not in out and "</i>" not in out
    assert "<sub>" not in out
    assert "A Review" in out
    # Tag boundary leaves a space on each side; that's what normalise()
    # strips. The raw strip_html output keeps that space.
    assert "sub text" in out


def test_normalise_drops_punctuation_case_and_html() -> None:
    a = normalise("Putting Framing in Perspective: <i>A Review</i>")
    b = normalise("PUTTING FRAMING IN PERSPECTIVE — a review!")
    # Both should reduce to "puttingframinginperspectiveareview" (plus
    # trailing chars if any). The exact form matters less than equality.
    assert a == b


def test_matches_accepts_truncated_forms() -> None:
    long_t = "Putting Framing in Perspective: A Review of Framing and Frame Analysis across the Management Literature"
    short = "Putting Framing in Perspective: A Review of Framing and Frame Analysis"
    assert matches(long_t, short)
    assert matches(short, long_t)


def test_matches_rejects_different_papers() -> None:
    assert not matches(
        "Putting Framing in Perspective",
        "Do Androids Dream of Entrepreneurial Possibilities",
    )


def test_matches_empty_inputs_are_false() -> None:
    assert not matches("", "")
    assert not matches("title", "")
    assert not matches("", "title")


# ---------------------------------------------------------------------------
# WosSource — expanded tier
# ---------------------------------------------------------------------------


def _expanded_response(records_found: int, records: list[dict] | None = None) -> dict:
    """Canned shape of the WoS Expanded JSON payload."""
    if records_found == 0:
        return {"QueryResult": {"RecordsFound": 0}, "Data": {}}
    rec_field = records if records is not None else []
    return {
        "QueryResult": {"RecordsFound": records_found},
        "Data": {"Records": {"records": {"REC": rec_field}}},
    }


def _expanded_record(
    title: str, abstract_text: str | list | None, *,
    year: int = 2014, author: str = "Cornelissen", source: str = "ACADEMY OF MANAGEMENT ANNALS",
) -> dict:
    """One WoS Expanded record with the fields WosSource reads."""
    abstracts_block: dict
    if abstract_text is None:
        abstracts_block = {"count": 0}
    else:
        abstracts_block = {
            "count": 1,
            "abstract": {"abstract_text": {"p": abstract_text}},
        }
    return {
        "static_data": {
            "summary": {
                "titles": {"title": [
                    {"type": "item", "content": title},
                    {"type": "source", "content": source},
                ]},
                "pub_info": {"pubyear": year},
                "names": {"name": [{"role": "author", "last_name": author}]},
            },
            "fullrecord_metadata": {"abstracts": abstracts_block},
        },
    }


class _Config:
    def __init__(self, extended="", starter=""):
        self.wos_api_key_extended = extended
        self.wos_api_key = starter


def _http_returning(*responses: dict) -> tuple[MagicMock, list]:
    """Build a mock session whose .get() yields the given dicts in order.

    Returns (mock_session, captured_params_list) — tests can inspect
    what queries the source ran without threading a global.
    """
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(params or {})
        idx = min(len(calls) - 1, len(responses) - 1)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = responses[idx]
        return resp

    sess = MagicMock()
    sess.get.side_effect = fake_get
    return sess, calls


def test_wos_without_a_key_is_unavailable_not_a_miss() -> None:
    """None would put WoS in the log's "no abstract at" list although
    it was never asked."""
    from fetchers.base import SourceUnavailable
    src = WosSource(http=MagicMock(), config=_Config())
    with pytest.raises(SourceUnavailable):
        src.fetch_abstract("10.1/x")


def test_wos_doi_hit_returns_abstract() -> None:
    text = "A comprehensive study of framing in organisational research " * 3
    sess, _ = _http_returning(
        _expanded_response(1, [_expanded_record("Some Title", text.strip())]),
    )
    src = WosSource(http=sess, config=_Config(extended="KEY"))
    result = src.fetch_abstract("10.5465/amd.2015.0052")
    assert result is not None
    assert "framing" in result.lower()


def test_wos_doi_miss_with_no_title_returns_none() -> None:
    sess, _ = _http_returning(_expanded_response(0))
    src = WosSource(http=sess, config=_Config(extended="KEY"))
    assert src.fetch_abstract("10.5465/amd.2015.0052") is None


ANNALS_2014 = item_meta({
    "date": "2014", "creators": [{"lastName": "Cornelissen"}],
    "publicationTitle": "Academy of Management Annals",
})


def test_wos_doi_miss_title_fallback_hits() -> None:
    """Key scenario: DOI missing because WoS has a different DOI alias,
    but the title matches one of the title-search hits."""
    sess, calls = _http_returning(
        _expanded_response(0),          # 1st call: DO= miss
        _expanded_response(              # 2nd call: TI= hit
            2,
            [
                _expanded_record("Unrelated: A Different Paper", "different"),
                _expanded_record(
                    "Putting Framing in Perspective: <i>A Review of Framing</i>",
                    "The long-awaited review of framing across management…",
                ),
            ],
        ),
    )
    src = WosSource(http=sess, config=_Config(extended="KEY"))
    result = src.fetch_abstract(
        "10.5465/19416520.2014.875669",
        title="Putting Framing in Perspective: A Review of Framing",
        meta=ANNALS_2014,
    )
    assert result is not None
    assert "framing" in result.lower()
    assert len(calls) == 2
    assert calls[0]["usrQuery"].startswith("DO=")
    assert calls[1]["usrQuery"].startswith("TI=")


def test_wos_title_fallback_rejects_mismatched_title() -> None:
    """Title fallback returns results but none match the requested
    title — should return None, not the first random result."""
    sess, _ = _http_returning(
        _expanded_response(0),
        _expanded_response(
            1,
            [_expanded_record("A Completely Different Paper", "some abstract")],
        ),
    )
    src = WosSource(http=sess, config=_Config(extended="KEY"))
    result = src.fetch_abstract(
        "10.5465/amj.2023.0001",
        title="Putting Framing in Perspective",
    )
    assert result is None


def test_wos_record_without_abstract_returns_none() -> None:
    """WoS has the record, but the abstract field is empty (count=0).
    Many publishers index metadata without depositing abstract text."""
    sess, _ = _http_returning(
        _expanded_response(1, [_expanded_record("Some Title", None)]),
    )
    src = WosSource(http=sess, config=_Config(extended="KEY"))
    assert src.fetch_abstract("10.5465/amr.2024.0299") is None


def test_wos_prefers_extended_key_over_starter() -> None:
    """When both keys are configured, the expanded-tier path wins."""
    sess, _ = _http_returning(
        _expanded_response(1, [_expanded_record("T", "abstract text long enough")]),
    )
    src = WosSource(
        http=sess, config=_Config(extended="EXT", starter="STA"),
    )
    src.fetch_abstract("10.5465/amd.2015.0052")

    # Confirm expanded URL was used, not the starter URL.
    from fetchers.wos import _EXPANDED_URL, _STARTER_URL
    called_urls = [c.args[0] for c in sess.get.call_args_list]
    assert _EXPANDED_URL in called_urls
    assert _STARTER_URL not in called_urls


def test_wos_starter_tier_is_used_when_only_starter_key() -> None:
    sess, _ = _http_returning(
        # Starter response shape: {hits: [{abstract: ...}]}
        {"hits": [{"abstract": "starter abstract text long enough to qualify"}]},
    )
    src = WosSource(http=sess, config=_Config(starter="STA"))
    result = src.fetch_abstract("10.5465/amd.2015.0052")
    assert result is not None
    assert "starter" in result
    from fetchers.wos import _STARTER_URL
    assert sess.get.call_args_list[0].args[0] == _STARTER_URL


def test_wos_short_abstract_rejected() -> None:
    """A near-empty abstract field (<=40 chars) is treated as noise."""
    sess, _ = _http_returning(
        _expanded_response(1, [_expanded_record("T", "too short")]),
    )
    src = WosSource(http=sess, config=_Config(extended="KEY"))
    assert src.fetch_abstract("10.5465/amd.2015.0052") is None


def test_wos_concatenates_list_paragraphs() -> None:
    """abstract_text.p is sometimes a list of strings (multi-paragraph);
    the source must join them, not stringify the list."""
    long_paras = ["First paragraph of the abstract. " * 3,
                  "Second paragraph with more detail. " * 3]
    sess, _ = _http_returning(
        _expanded_response(1, [_expanded_record("T", long_paras)]),
    )
    src = WosSource(http=sess, config=_Config(extended="KEY"))
    result = src.fetch_abstract("10.5465/amd.2015.0052")
    assert result is not None
    assert "First paragraph" in result
    assert "Second paragraph" in result


@pytest.mark.parametrize("status", [429, 500, 503])
def test_wos_error_status_raises(status) -> None:
    """A failed lookup must reach the cascade as a failure: returning
    None logged 6I9S5R8C not_found although WoS holds it."""
    sess = MagicMock()
    bad = MagicMock()
    bad.status_code = status
    sess.get.return_value = bad
    src = WosSource(http=sess, config=_Config(extended="KEY"))
    with pytest.raises(RuntimeError, match=str(status)):
        src.fetch_abstract("10.5465/amd.2015.0052")


def test_wos_exception_propagates() -> None:
    """The cascade catches it and logs lookup_failed; swallowing it here
    turned a network error into "no abstract"."""
    sess = MagicMock()
    sess.get.side_effect = RuntimeError("network down")
    src = WosSource(http=sess, config=_Config(extended="KEY"))
    with pytest.raises(RuntimeError, match="network down"):
        src.fetch_abstract("10.5465/amd.2015.0052")


# ---------------------------------------------------------------------------
# Title fallback: another record's abstract (2026-09-23, 15 of 152)
# ---------------------------------------------------------------------------


def _fallback(title: str, meta, rec: dict):
    sess, calls = _http_returning(_expanded_response(0), _expanded_response(1, [rec]))
    src = WosSource(http=sess, config=_Config(extended="KEY"))
    return src.fetch_abstract("10.1/x", title=title, meta=meta), calls


@pytest.mark.parametrize("title", ["Erratum", "COMMENTARY", "Introduction", "Time to get tough"])
def test_a_generic_title_is_not_searched(title) -> None:
    """IKBK6EQN "COMMENTARY" (1993) got a 2026 SEC proposal's abstract;
    SEFAI6KR "Erratum" (2015) a 2024 erratum."""
    got, calls = _fallback(title, ANNALS_2014, _expanded_record(title, "x" * 80))
    assert got is None
    assert len(calls) == 1                   # the DOI query only


def test_a_title_hit_from_another_year_is_refused() -> None:
    """W6FFXNPB "Striking the right note", Nature 1999, got modern text
    on electric-vehicle sound."""
    meta = item_meta({"date": "1999-07-01", "creators": [{"lastName": "Ball"}],
                      "publicationTitle": "Nature"})
    rec = _expanded_record("Striking the right note: acoustic vehicle alerts", "y" * 80,
                           year=2023, author="Ball", source="NATURE")
    assert _fallback("Striking the right note", meta, rec)[0] is None


def test_a_title_hit_with_no_author_or_venue_in_common_is_refused() -> None:
    rec = _expanded_record("Putting Framing in Perspective: A Review", "z" * 80,
                           author="Someone", source="JOURNAL OF ELSEWHERE")
    assert _fallback("Putting Framing in Perspective: A Review", ANNALS_2014, rec)[0] is None


def test_a_title_hit_matching_the_venue_alone_is_accepted() -> None:
    rec = _expanded_record("Putting Framing in Perspective: A Review", "w" * 80,
                           author="Someone")
    assert _fallback("Putting Framing in Perspective: A Review", ANNALS_2014, rec)[0]


def test_an_item_without_a_year_gets_no_title_fallback() -> None:
    meta = item_meta({"creators": [{"lastName": "Cornelissen"}]})
    rec = _expanded_record("Putting Framing in Perspective: A Review", "v" * 80)
    assert _fallback("Putting Framing in Perspective: A Review", meta, rec)[0] is None


def test_surnames_match_across_accents() -> None:
    meta = item_meta({"date": "2019", "creators": [{"lastName": "Röth"}]})
    rec = _expanded_record("Resistance to change and innovativeness", "u" * 80,
                           year=2019, author="Roth", source="J BUS RES")
    assert _fallback("Resistance to change and innovativeness", meta, rec)[0]
