"""The preprint fetcher: what it may return, and what it must not.

A preprint is the manuscript before peer review. Between it and the
published paper sit revised hypotheses, dropped analyses and sometimes a
reversed finding — differences a systematic review cannot detect after
the fact, because the coded row reads identically either way.

Three properties carry the whole safety argument, and each is pinned
here:

1. **The source is off unless asked for**, and `--sources preprint` is
   not asking — the flag is where the hazard is explained.
2. **A location qualifies by host and by nothing else**, so this source
   cannot return a publisher's own PDF and label it a preprint.
3. **Discovery is anchored on the DOI**, never on a title, because a
   near-miss would attach a different paper's manuscript under a tag
   that invites less scrutiny rather than more.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import fetchers
from fetchers.preprint import (
    PREPRINT_VERSION_TAG,
    PreprintSource,
    _arxiv_pdf_url,
    is_preprint_path,
    preprint_server_for,
)


class _Cfg:
    crossref_mailto = "test@example.org"


def _pdf_bytes() -> bytes:
    body = (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj\n"
    )
    body += b"%" + b"padding" * 200 + b"\n"
    return body + b"startxref\n9\n%%EOF\n"


def _json_response(status: int, payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    return resp


def _pdf_response(content: bytes) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.content = content
    resp.headers = {"Content-Type": "application/pdf"}
    return resp


def _openalex(*pdf_urls: str) -> MagicMock:
    return _json_response(200, {
        "locations": [{"pdf_url": url} for url in pdf_urls],
    })


def _crossref_relation(*preprint_dois: str) -> MagicMock:
    return _json_response(200, {"message": {"relation": {
        "has-preprint": [
            {"id-type": "doi", "id": doi} for doi in preprint_dois
        ],
    }}})


# ---------------------------------------------------------------------------
# Host qualification
# ---------------------------------------------------------------------------


def test_the_three_named_servers_are_recognised() -> None:
    assert preprint_server_for("https://arxiv.org/pdf/2401.01234") == "arxiv"
    assert preprint_server_for("https://papers.ssrn.com/x.pdf") == "ssrn"
    assert preprint_server_for("https://ideas.repec.org/p/x.pdf") == "repec"


def test_a_publisher_url_is_not_a_preprint_host() -> None:
    """The filter that makes it impossible to tag a version of record as
    a preprint."""
    assert preprint_server_for("https://journals.sagepub.com/x.pdf") is None
    assert preprint_server_for("https://link.springer.com/x.pdf") is None


def test_a_lookalike_domain_does_not_qualify() -> None:
    assert preprint_server_for("https://arxiv.org.evil.example/x.pdf") is None


def test_a_publisher_hosted_pdf_is_ignored_even_when_openalex_lists_it(
    tmp_path,
) -> None:
    http = MagicMock()
    http.get.side_effect = [
        _openalex("https://journals.sagepub.com/doi/pdf/10.1177/x"),
        _json_response(200, {"message": {}}),      # crossref: no relations
    ]
    src = PreprintSource(http=http, config=_Cfg())

    assert src.fetch_pdf("10.1177/x", cache_dir=tmp_path) is None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_an_openalex_preprint_location_is_downloaded(tmp_path) -> None:
    http = MagicMock()
    http.get.side_effect = [
        _openalex("https://arxiv.org/pdf/1602.03837"),
        _pdf_response(_pdf_bytes()),
    ]
    src = PreprintSource(http=http, config=_Cfg())

    result = src.fetch_pdf("10.1103/PhysRevLett.116.061102", cache_dir=tmp_path)

    assert result is not None
    path, url = result
    assert url == "https://arxiv.org/pdf/1602.03837"
    assert path.read_bytes().startswith(b"%PDF-")


def test_crossrefs_has_preprint_relation_is_the_fallback(tmp_path) -> None:
    """The publisher's own statement that these are the same paper —
    stronger evidence than anything this plugin could infer."""
    http = MagicMock()
    http.get.side_effect = [
        _openalex(),                               # openalex: no preprint copy
        _crossref_relation("10.48550/arXiv.2401.01234"),
        _pdf_response(_pdf_bytes()),
    ]
    src = PreprintSource(http=http, config=_Cfg())

    result = src.fetch_pdf("10.1/published", cache_dir=tmp_path)

    assert result is not None
    assert result[1] == "https://arxiv.org/pdf/2401.01234"


def test_an_ssrn_relation_is_resolved_through_openalex(tmp_path) -> None:
    """SSRN publishes no downloadable URL of its own, so the preprint DOI
    has to be looked up a second time."""
    http = MagicMock()
    http.get.side_effect = [
        _openalex(),                               # published DOI: nothing
        _crossref_relation("10.2139/ssrn.1234567"),
        _openalex("https://papers.ssrn.com/1234567.pdf"),
        _pdf_response(_pdf_bytes()),
    ]
    src = PreprintSource(http=http, config=_Cfg())

    result = src.fetch_pdf("10.5465/amj.2020.0001", cache_dir=tmp_path)

    assert result is not None
    assert result[1] == "https://papers.ssrn.com/1234567.pdf"


def test_a_relation_to_a_non_preprint_doi_is_ignored(tmp_path) -> None:
    http = MagicMock()
    http.get.side_effect = [
        _openalex(),
        _crossref_relation("10.1234/some-erratum"),
    ]
    src = PreprintSource(http=http, config=_Cfg())

    assert src.fetch_pdf("10.1/x", cache_dir=tmp_path) is None


def test_arxiv_dois_map_to_the_pdf_path() -> None:
    assert _arxiv_pdf_url("10.48550/arXiv.2401.01234") == (
        "https://arxiv.org/pdf/2401.01234"
    )
    assert _arxiv_pdf_url("10.2139/ssrn.1234567") is None


def test_nothing_found_is_a_clean_miss(tmp_path) -> None:
    """The cascade has to keep going: coverage is uneven by design, so a
    miss means "no preprint this plugin can reach", not "no PDF exists"."""
    http = MagicMock()
    http.get.side_effect = [
        _openalex(),
        _json_response(404, {}),
    ]
    src = PreprintSource(http=http, config=_Cfg())

    assert src.fetch_pdf("10.1/x", cache_dir=tmp_path) is None


def test_a_landing_page_served_as_a_pdf_is_rejected(tmp_path) -> None:
    """Preprint servers answer the same URL shape with abstract pages and
    embargo notices."""
    html = MagicMock()
    html.status_code = 200
    html.content = b"<!DOCTYPE html><title>Download unavailable</title>"
    html.headers = {"Content-Type": "text/html"}

    http = MagicMock()
    http.get.side_effect = [_openalex("https://arxiv.org/pdf/x"), html]
    src = PreprintSource(http=http, config=_Cfg())

    assert src.fetch_pdf("10.1/x", cache_dir=tmp_path) is None


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_the_cache_filename_carries_the_provenance(tmp_path) -> None:
    """By attach time the orchestrator holds only a path, so the tag has
    to be recoverable from the filename."""
    http = MagicMock()
    http.get.side_effect = [
        _openalex("https://arxiv.org/pdf/2401.01234"),
        _pdf_response(_pdf_bytes()),
    ]
    result = PreprintSource(http=http, config=_Cfg()).fetch_pdf(
        "10.1/x", cache_dir=tmp_path,
    )

    assert result is not None
    assert is_preprint_path(result[0])
    assert not is_preprint_path(tmp_path / "10.1_x.pdf")


def test_the_tag_follows_the_existing_provenance_convention() -> None:
    """`pdf:tdm-recovered` and `pdf:repository-copy` set the pattern; a
    new spelling would need a new entry in every skill catalogue."""
    assert PREPRINT_VERSION_TAG == "pdf:preprint-version"


def test_the_orchestrator_knows_how_to_tag_this_source() -> None:
    assert fetchers.is_preprint_path("/cache/10.1_x-preprint.pdf")
    assert fetchers.PREPRINT_VERSION_TAG == PREPRINT_VERSION_TAG


# ---------------------------------------------------------------------------
# The opt-in
# ---------------------------------------------------------------------------


def test_the_default_cascade_does_not_include_preprints() -> None:
    names = [s.name for s in fetchers.pdf_sources(MagicMock(), None)]
    assert "preprint" not in names


def test_allow_preprints_appends_it_after_every_other_source() -> None:
    """Last of all: every earlier source serves a paper that passed peer
    review, and one that did not is only worth taking when nothing else
    answered."""
    names = [
        s.name for s in fetchers.pdf_sources(
            MagicMock(), None, allow_preprints=True,
        )
    ]
    assert names[-1] == "preprint"


def test_naming_the_source_explicitly_still_selects_it() -> None:
    """`names=` is the orchestrator's own selection path; the CLI guard
    that requires --allow-preprints lives in enrich_pdfs."""
    names = [
        s.name for s in fetchers.pdf_sources(MagicMock(), None,
                                             names=["preprint"])
    ]
    assert names == ["preprint"]
