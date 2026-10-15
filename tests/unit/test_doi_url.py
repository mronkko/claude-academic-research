"""DOIs spliced into URL paths are encoded where it changes meaning.

A raw `#` started a fragment and a raw `?` a query, so a request asked
about a truncated DOI and its "not found" was logged against the real
one. Ordinary DOIs must come through unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fetchers.browser.sage import SageHandler
from fetchers.doi_url import doi_path

SICI = "10.1002/(sici)1097-0266(199701)18:1<1::aid-smj894>3.0.co;2-d"


@pytest.mark.parametrize("doi", [
    "10.1287/orsc.11.4.367.14601", "10.5465/amd.2015.0052", "10.1/a:b;c(1)",
])
def test_ordinary_dois_are_unchanged(doi) -> None:
    assert doi_path(doi) == doi


def test_structure_changing_characters_are_encoded() -> None:
    out = doi_path(SICI + "#x?y z")
    for raw in "<>#? ":
        assert raw not in out
    assert out.startswith("10.1002/(sici)")


def test_browser_handler_urls_encode_the_doi() -> None:
    url = SageHandler()._setup_url_for("10.1177/a#b")
    assert "#" not in url and "%23" in url


def test_semantic_scholar_encodes_the_doi(monkeypatch) -> None:
    from fetchers.semantic_scholar import SemanticScholarSource

    seen = []

    def fake_get(url, *a, **k):
        seen.append(url)
        return SimpleNamespace(status_code=404, json=lambda: {}, headers={})

    http = MagicMock()
    http.get.side_effect = fake_get
    src = SemanticScholarSource(http=http, config=None)
    try:
        src.fetch_abstract("10.1/a#b")
    except Exception:  # noqa: BLE001 — only the URL matters here
        pass
    assert seen and "#" not in seen[0] and "%23" in seen[0]
