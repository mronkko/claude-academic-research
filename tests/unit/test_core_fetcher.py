"""The CORE fetcher: repository full text, and the guards it needs.

CORE is last in the PDF cascade and is the only source here that
searches rather than looks up. Both facts drive what is tested:

- a *search* can return a near-miss, and attaching a near-miss puts
  another paper's full text on the item — worse than attaching nothing,
  and invisible afterwards. So the DOI is re-checked against every hit.
- what it serves is usually the accepted manuscript, not the version of
  record, so the provenance has to survive as far as the Zotero tag.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from fetchers.core import (
    REPOSITORY_COPY_TAG,
    CoreSource,
    is_repository_copy_path,
)


class _Cfg:
    core_api_key = "test-key"


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


def _hit(doi: str, url: str = "https://repo.example/x.pdf") -> dict:
    return {"doi": doi, "downloadUrl": url}


# ---------------------------------------------------------------------------
# The near-miss guard
# ---------------------------------------------------------------------------


def test_a_non_matching_doi_is_not_accepted(tmp_path) -> None:
    """CORE's search is fuzzy and ranks by relevance. Trusting rank would
    attach a different paper's full text to the item."""
    http = MagicMock()
    http.get.return_value = _json_response(200, {
        "results": [_hit("10.9999/some-other-paper")],
    })
    src = CoreSource(http=http, config=_Cfg())

    assert src.fetch_pdf("10.1/x", cache_dir=tmp_path) is None


def test_the_matching_hit_is_used_even_when_it_is_not_first(tmp_path) -> None:
    http = MagicMock()
    http.get.side_effect = [
        _json_response(200, {"results": [
            _hit("10.9999/wrong", "https://repo.example/wrong.pdf"),
            _hit("10.1/x", "https://repo.example/right.pdf"),
        ]}),
        _pdf_response(_pdf_bytes()),
    ]
    src = CoreSource(http=http, config=_Cfg())

    result = src.fetch_pdf("10.1/x", cache_dir=tmp_path)
    assert result is not None
    assert result[1] == "https://repo.example/right.pdf"


def test_doi_matching_is_case_insensitive(tmp_path) -> None:
    """DOIs are case-insensitive by spec and repositories are
    inconsistent about it."""
    http = MagicMock()
    http.get.side_effect = [
        _json_response(200, {"results": [_hit("10.1/ABC")]}),
        _pdf_response(_pdf_bytes()),
    ]
    src = CoreSource(http=http, config=_Cfg())

    assert src.fetch_pdf("10.1/abc", cache_dir=tmp_path) is not None


def test_a_matching_hit_without_a_download_url_is_a_miss(tmp_path) -> None:
    """CORE indexes metadata it has no full text for."""
    http = MagicMock()
    http.get.return_value = _json_response(200, {
        "results": [{"doi": "10.1/x", "downloadUrl": ""}],
    })
    src = CoreSource(http=http, config=_Cfg())

    assert src.fetch_pdf("10.1/x", cache_dir=tmp_path) is None


# ---------------------------------------------------------------------------
# Credential handling
# ---------------------------------------------------------------------------


def test_without_a_key_the_source_makes_no_request(tmp_path, monkeypatch) -> None:
    """Every CORE endpoint 401s without a key, so asking is pure waste —
    one wasted round trip per item across the whole corpus."""
    monkeypatch.delenv("CORE_API_KEY", raising=False)
    http = MagicMock()
    src = CoreSource(http=http, config=None)

    assert src.fetch_pdf("10.1/x", cache_dir=tmp_path) is None
    http.get.assert_not_called()


def test_the_key_is_sent_as_a_bearer_token(tmp_path) -> None:
    http = MagicMock()
    http.get.return_value = _json_response(200, {"results": []})
    src = CoreSource(http=http, config=_Cfg())

    src.fetch_pdf("10.1/x", cache_dir=tmp_path)

    headers = http.get.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer test-key"


def test_a_rejected_key_does_not_raise(tmp_path, caplog) -> None:
    """A dead optional key must degrade to "no result", not kill the run."""
    http = MagicMock()
    http.get.return_value = _json_response(401, {})
    src = CoreSource(http=http, config=_Cfg())

    assert src.fetch_pdf("10.1/x", cache_dir=tmp_path) is None


# ---------------------------------------------------------------------------
# Validation and provenance
# ---------------------------------------------------------------------------


def test_a_landing_page_is_rejected_and_not_cached(tmp_path) -> None:
    """Repositories serve splash pages and embargo notices from the same
    URL shape as the file."""
    http = MagicMock()
    http.get.side_effect = [
        _json_response(200, {"results": [_hit("10.1/x")]}),
        _pdf_response(b"<html>This item is under embargo</html>"),
    ]
    src = CoreSource(http=http, config=_Cfg())

    assert src.fetch_pdf("10.1/x", cache_dir=tmp_path) is None
    assert not list(tmp_path.glob("*.pdf"))


def test_a_fetched_file_is_marked_as_a_repository_copy(tmp_path) -> None:
    """The tag is applied at attach time from the path alone, so the
    filename is the only channel provenance has."""
    http = MagicMock()
    http.get.side_effect = [
        _json_response(200, {"results": [_hit("10.1/x")]}),
        _pdf_response(_pdf_bytes()),
    ]
    src = CoreSource(http=http, config=_Cfg())

    result = src.fetch_pdf("10.1/x", cache_dir=tmp_path)
    assert result is not None
    assert is_repository_copy_path(result[0])


def test_other_fetchers_paths_are_not_marked_as_repository_copies() -> None:
    assert not is_repository_copy_path("/cache/10.1_x.pdf")


def test_the_tag_follows_the_pdf_namespace_convention() -> None:
    """`pdf:tdm-recovered` set the pattern; a new spelling would need a
    new entry in every skill's tag catalogue."""
    assert REPOSITORY_COPY_TAG.startswith("pdf:")


def test_a_corrupt_cache_entry_is_discarded(tmp_path) -> None:
    cached = tmp_path / "10.1_x-repository-copy.pdf"
    cached.write_bytes(b"%PDF-1.4\ntruncated")

    http = MagicMock()
    http.get.side_effect = [
        _json_response(200, {"results": [_hit("10.1/x")]}),
        _pdf_response(_pdf_bytes()),
    ]
    src = CoreSource(http=http, config=_Cfg())

    result = src.fetch_pdf("10.1/x", cache_dir=tmp_path)
    assert result is not None
    assert result[1] == "https://repo.example/x.pdf"   # re-fetched


def test_a_valid_cache_entry_is_served_without_a_request(tmp_path) -> None:
    cached = tmp_path / "10.1_x-repository-copy.pdf"
    cached.write_bytes(_pdf_bytes())
    http = MagicMock()
    src = CoreSource(http=http, config=_Cfg())

    result = src.fetch_pdf("10.1/x", cache_dir=tmp_path)

    assert result == (cached, f"cache://{cached}")
    http.get.assert_not_called()


# ---------------------------------------------------------------------------
# Cascade placement
# ---------------------------------------------------------------------------


def test_repository_sources_are_last_in_the_default_cascade() -> None:
    """CORE, OpenAIRE and BASE all serve the accepted manuscript rather
    than the version of record, so they must only answer for DOIs
    nothing else could — and CORE leads them, being the only one of the
    three with measured recall on a real corpus."""
    import fetchers

    names = [s.name for s in fetchers.pdf_sources(MagicMock(), None)]
    assert names[-3:] == ["core", "openaire", "base"], names
