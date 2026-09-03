"""Unit tests for Elsevier ScienceDirect preview detection + XML fallback (P11).

The session-log evidence: Elsevier's TDM API returned a 1-page preview
PDF (200 OK + valid `%PDF` magic) for 38 articles where the user's
institution lacked TDM entitlement. The signal was the `x-els-status`
response header `WARNING - Response limited to first page because
requestor not entitled to resource`. The fetcher previously ignored
that header and silently cached the preview; the user had to write
an out-of-band remediation script to recover full text via the XML
endpoint.

These tests pin the fix:
- preview-headed responses are NOT cached;
- on WARNING the fetcher hits the XML endpoint;
- a successful XML response is rendered to a text-only PDF and
  cached with the `-tdm-recovered` suffix.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from fetchers.sciencedirect import (
    TDM_RECOVERED_TAG,
    ScienceDirectSource,
    _extract_xml_body,
    _is_preview_warning,
    _recovery_note,
    _render_text_pdf,
    is_tdm_recovered_path,
)

# ---------------------------------------------------------------------------
# _is_preview_warning — header classifier
# ---------------------------------------------------------------------------


def test_is_preview_warning_matches_canonical_wording() -> None:
    assert _is_preview_warning(
        "WARNING - Response limited to first page because requestor not entitled to resource"
    )


def test_is_preview_warning_matches_short_warning_prefix() -> None:
    """Elsevier sometimes emits a shorter `WARNING ...` form."""
    assert _is_preview_warning("WARNING - some other partial response reason")


def test_is_preview_warning_matches_lowercase_not_entitled() -> None:
    assert _is_preview_warning("Status: not entitled to download")


def test_is_preview_warning_returns_false_for_ok_status() -> None:
    assert not _is_preview_warning("OK")
    assert not _is_preview_warning("")
    assert not _is_preview_warning("FULL")


# ---------------------------------------------------------------------------
# _extract_xml_body — XML body parser
# ---------------------------------------------------------------------------


def test_extract_xml_body_pulls_text_from_namespaced_xml() -> None:
    xml = b"""<?xml version="1.0"?>
    <ns0:article xmlns:ns0="http://example.com/elsevier">
        <ns0:body>
            <ns0:section><ns0:para>Body paragraph one.</ns0:para></ns0:section>
            <ns0:section><ns0:para>Body paragraph two.</ns0:para></ns0:section>
        </ns0:body>
    </ns0:article>
    """
    body = _extract_xml_body(xml)
    assert "Body paragraph one." in body
    assert "Body paragraph two." in body


def test_extract_xml_body_returns_empty_on_no_body_element() -> None:
    xml = b"<article><coredata>Just metadata</coredata></article>"
    assert _extract_xml_body(xml) == ""


def test_extract_xml_body_returns_empty_on_malformed_xml() -> None:
    assert _extract_xml_body(b"<not><well>formed") == ""


# ---------------------------------------------------------------------------
# fetch_pdf — preview detection + XML fallback
# ---------------------------------------------------------------------------


def _make_source(api_key: str = "fake-key") -> ScienceDirectSource:
    """Build a ScienceDirectSource with a stub config and HTTP session."""
    cfg = MagicMock()
    cfg.elsevier_api_key = api_key
    src = ScienceDirectSource(config=cfg)
    src.http = MagicMock()
    return src


# A structurally plausible PDF: `_pdf_validate` now rejects bodies that
# are too small or lack an %%EOF trailer, because a truncated download
# passes a bare `%PDF` magic-byte check. Test fixtures have to look like
# real files for the happy paths to stay happy.
def _fake_pdf(marker: bytes = b"body") -> bytes:
    return b"%PDF-1.4\n" + marker + b"\n" + b"0" * 2000 + b"\n%%EOF\n"


def _pdf_response(*, status: int = 200, content: bytes | None = None,
                  els_status: str = "OK") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.content = _fake_pdf() if content is None else content
    resp.headers = {"x-els-status": els_status}
    return resp


def _xml_response(*, status: int = 200, content: bytes = b"",
                  els_status: str = "OK") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.content = content
    resp.headers = {"x-els-status": els_status}
    return resp


def test_fetch_pdf_caches_when_pdf_is_full(tmp_path: Path) -> None:
    """Happy path: PDF endpoint returns 200 + %PDF magic + status OK.
    The fetcher caches the bytes verbatim — no XML fallback needed."""
    src = _make_source()
    src.http.get.return_value = _pdf_response(els_status="OK")
    result = src.fetch_pdf("10.1016/j.respol.2020.01.001", cache_dir=tmp_path)
    assert result is not None
    path, _ = result
    assert path.is_file()
    assert path.read_bytes().startswith(b"%PDF")
    assert "-tdm-recovered" not in path.name


def test_fetch_pdf_skips_preview_and_falls_back_to_xml(tmp_path: Path) -> None:
    """The P11 contract: WARNING header on the PDF endpoint triggers
    the XML fallback. The recovered text-only PDF is cached under
    `<doi>-tdm-recovered.pdf` so audits can distinguish recovered
    items from natively-fetched ones.
    """
    src = _make_source()

    # Body text has to sit in `<para>` inside a `<section>`: since 0.15.0
    # the extractor walks elements rather than joining raw text, so a bare
    # string under `<body>` yields zero blocks and the fallback declines.
    # This fixture previously did exactly that, and the test read the
    # resulting None as "reportlab is missing" and skipped — so the
    # rendering path it exists to cover went unexercised for a whole
    # release, which is the failure mode the reportlab dev-dependency was
    # added to prevent.
    long_body = "Section text. " * 80  # ~1100 chars
    xml_payload = (
        b"<?xml version='1.0'?>"
        b"<article xmlns='http://example.com/elsevier'>"
        b"<body><sections><section>"
        b"<section-title>Introduction</section-title>"
        b"<para>" + long_body.encode("utf-8") + b"</para>"
        b"</section></sections></body>"
        b"</article>"
    )

    # First call: PDF endpoint returns preview-warning. Second call:
    # XML endpoint returns full body.
    src.http.get.side_effect = [
        _pdf_response(
            els_status="WARNING - Response limited to first page because requestor not entitled to resource",
        ),
        _xml_response(content=xml_payload, els_status="OK"),
    ]
    result = src.fetch_pdf("10.1016/j.jbusvent.2020.05.001", cache_dir=tmp_path)
    assert result is not None, "XML fallback produced no recovered PDF"
    path, source_label = result
    assert path.is_file()
    assert "-tdm-recovered" in path.name
    assert "xml-fallback" in source_label
    assert path.read_bytes().startswith(b"%PDF")


def test_fetch_pdf_returns_none_when_xml_also_unentitled(tmp_path: Path) -> None:
    """If the XML endpoint also responds with a WARNING header, the
    item is genuinely unrecoverable — return None rather than caching
    the preview as a fallback. Down-stream cascade then logs the
    failure with cause UNAVAILABLE / ACCESS_BLOCKED."""
    src = _make_source()
    src.http.get.side_effect = [
        _pdf_response(els_status="WARNING - not entitled"),
        _xml_response(els_status="WARNING - not entitled"),
    ]
    result = src.fetch_pdf("10.1016/j.unrecoverable.2020.01.001", cache_dir=tmp_path)
    assert result is None
    # And no preview-cache was left behind.
    assert not any(tmp_path.iterdir())


def test_fetch_pdf_returns_none_when_xml_body_is_too_short(tmp_path: Path) -> None:
    """An entitled XML response with a tiny body (Elsevier rarely
    returns these, but defensive against edge cases) is treated as
    not-recovered rather than cached as a near-empty PDF."""
    src = _make_source()
    src.http.get.side_effect = [
        _pdf_response(els_status="WARNING - not entitled"),
        _xml_response(
            content=b"<article><body>just a short note.</body></article>",
            els_status="OK",
        ),
    ]
    result = src.fetch_pdf("10.1016/j.short.2020.01.001", cache_dir=tmp_path)
    assert result is None


def test_fetch_pdf_prefers_cached_recovered_pdf_over_fresh_call(
    tmp_path: Path,
) -> None:
    """A previously-recovered PDF in the cache (from an earlier run)
    short-circuits the network call. The naming convention
    `<doi>-tdm-recovered.pdf` is what makes this lookup work.

    The cached file has to be one this build would produce: an entry with
    no version stamp is refused and re-fetched, because it predates the
    0.15.0 transformation change. See
    tests/unit/test_recovered_cache_validation.py for that half.
    """
    src = _make_source()
    doi = "10.1016/j.cached.2020.01.001"
    cached = tmp_path / "10.1016_j.cached.2020.01.001-tdm-recovered.pdf"
    _render_text_pdf(
        [("p", "Recovered body text. " * 20)],
        cached,
        note=_recovery_note(0),
        meta={"title": "Cached", "doi": doi},
    )
    src.http.get.side_effect = AssertionError("should not be called on cache hit")

    result = src.fetch_pdf(doi, cache_dir=tmp_path)
    assert result is not None
    path, source = result
    assert path == cached
    assert source.startswith("cache://")


def test_fetch_pdf_skips_when_doi_prefix_not_elsevier(tmp_path: Path) -> None:
    """ScienceDirectSource only handles Elsevier DOIs — should bail
    early without calling the API for an arXiv / Wiley / Springer DOI.
    """
    src = _make_source()
    src.http.get.side_effect = AssertionError("should not be called for non-Elsevier DOI")
    assert src.fetch_pdf("10.48550/arxiv.2401.01234", cache_dir=tmp_path) is None


# ---------------------------------------------------------------------------
# is_tdm_recovered_path / TDM_RECOVERED_TAG — P11 item 3
# ---------------------------------------------------------------------------


def test_is_tdm_recovered_path_true_for_recovered_suffix(tmp_path: Path) -> None:
    path = tmp_path / "10.1016_j.example.2020.01.001-tdm-recovered.pdf"
    assert is_tdm_recovered_path(path) is True


def test_is_tdm_recovered_path_false_for_native_pdf(tmp_path: Path) -> None:
    path = tmp_path / "10.1016_j.example.2020.01.001.pdf"
    assert is_tdm_recovered_path(path) is False


def test_tdm_recovered_tag_is_pdf_namespaced() -> None:
    assert TDM_RECOVERED_TAG == "pdf:tdm-recovered"
