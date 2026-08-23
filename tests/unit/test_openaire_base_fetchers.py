"""Response-shape tests for the OpenAIRE and BASE repository fetchers.

Both APIs collapse single-element lists to a bare object and bury the
full-text link several levels down, so the extraction — not the HTTP —
is what breaks. These pin the shapes recorded from live responses on
2026-08-23.
"""

from __future__ import annotations

from fetchers.base_search import candidate_urls as base_urls
from fetchers.base_search import is_access_denied
from fetchers.openaire import candidate_urls as openaire_urls


def _openaire(fulltext=None, instances=None):
    return {"response": {"results": {"result": {"metadata": {
        "oaf:entity": {"oaf:result": {
            "fulltext": fulltext,
            "children": {"instance": instances} if instances else {},
        }}}}}}}


# ---------------------------------------------------------------------------
# OpenAIRE
# ---------------------------------------------------------------------------


def test_openaire_reads_the_fulltext_field() -> None:
    payload = _openaire(fulltext=[
        {"$": "https://repo.example.edu/record/41"},
        {"$": "https://repo.example.edu/viewcontent/41.pdf"},
    ])
    assert openaire_urls(payload) == [
        "https://repo.example.edu/record/41",
        "https://repo.example.edu/viewcontent/41.pdf",
    ]


def test_openaire_accepts_a_single_unwrapped_fulltext() -> None:
    """The API drops the list wrapper when there is exactly one entry."""
    payload = _openaire(fulltext={"$": "https://repo.example.edu/a.pdf"})
    assert openaire_urls(payload) == ["https://repo.example.edu/a.pdf"]


def test_openaire_takes_open_instances_and_skips_closed_ones() -> None:
    payload = _openaire(instances=[
        {"accessright": {"@classid": "UNKNOWN"},
         "webresource": {"url": {"$": "https://closed.example/x"}}},
        {"accessright": {"@classid": "OPEN"},
         "webresource": {"url": {"$": "https://open.example/y.pdf"}}},
    ])
    assert openaire_urls(payload) == ["https://open.example/y.pdf"]


def test_openaire_discards_doi_org_urls() -> None:
    """A doi.org link redirects to the publisher page the cascade has
    already failed on; following it wastes a request and can bank an
    HTML error page as a hit."""
    payload = _openaire(instances=[
        {"accessright": {"@classid": "OPEN"},
         "webresource": {"url": {"$": "https://doi.org/10.1/x"}}},
    ])
    assert openaire_urls(payload) == []


def test_openaire_discards_preprint_hosts() -> None:
    """OpenAIRE lists an OSF copy as the open instance of a published
    article; this plugin treats a preprint as a different paper."""
    payload = _openaire(fulltext=[
        {"$": "https://osf.io/preprints/pk2b7"},
        {"$": "https://arxiv.org/pdf/1234.5678"},
        {"$": "https://repo.example.edu/real.pdf"},
    ])
    assert openaire_urls(payload) == ["https://repo.example.edu/real.pdf"]


def test_openaire_returns_empty_on_a_missing_record() -> None:
    assert openaire_urls({"response": {"results": {}}}) == []
    assert openaire_urls({}) == []


# ---------------------------------------------------------------------------
# BASE
# ---------------------------------------------------------------------------


def test_base_detects_the_ip_registration_refusal() -> None:
    """BASE answers 200 with an `error` body for an unregistered IP.
    Undetected, that parses as "no results" and the source looks like a
    coverage gap forever instead of a registration one."""
    assert is_access_denied(
        {"error": "Access denied for IP address 130.233.23.169 ..."}
    )
    assert not is_access_denied({"response": {"docs": []}})


def test_base_reads_dclink_and_filters_the_same_way() -> None:
    payload = {"response": {"docs": [
        {"dclink": ["https://doi.org/10.1/x",
                    "https://repo.example.edu/full.pdf"]},
        {"dclink": "https://osf.io/abc"},
    ]}}
    assert base_urls(payload) == ["https://repo.example.edu/full.pdf"]


def test_base_returns_empty_when_there_are_no_docs() -> None:
    assert base_urls({"response": {"docs": []}}) == []
    assert base_urls({}) == []
