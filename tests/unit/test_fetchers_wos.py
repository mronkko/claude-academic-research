"""Unit tests for fetchers/wos.py and fetchers/_title_match.py.

All tests monkey-patch `session.get` to return canned responses — no
real Clarivate API calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fetchers import WosSource
from fetchers._title_match import matches, normalise, strip_html


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


def _expanded_record(title: str, abstract_text: str | list | None) -> dict:
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
                "titles": {"title": [{"type": "item", "content": title}]},
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


def test_wos_returns_none_when_no_key() -> None:
    src = WosSource(http=MagicMock(), config=_Config())
    assert src.fetch_abstract("10.1/x") is None


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


def test_wos_non_200_returns_none() -> None:
    sess = MagicMock()
    bad = MagicMock()
    bad.status_code = 503
    sess.get.return_value = bad
    src = WosSource(http=sess, config=_Config(extended="KEY"))
    assert src.fetch_abstract("10.5465/amd.2015.0052") is None


def test_wos_exception_returns_none() -> None:
    """Network errors must not propagate — upstream cascade continues."""
    sess = MagicMock()
    sess.get.side_effect = RuntimeError("network down")
    src = WosSource(http=sess, config=_Config(extended="KEY"))
    assert src.fetch_abstract("10.5465/amd.2015.0052") is None
