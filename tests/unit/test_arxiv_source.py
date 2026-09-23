"""An arXiv-native item: its own DOI names the arXiv PDF.

Four arXiv items with DOIs like 10.48550/ARXIV.2007.05443 came back
"every fetch route tried and came back empty" on 2026-09-23. arXiv
serves a PDF for every posting; the gap was that nothing in the
cascade derived `arxiv.org/pdf/<id>` from the item's *own* DOI. The
nine that attached did so only because their OpenAlex record happened
to list the PDF; the DataCite-minted records (where arXiv's uppercase
DOI form comes from) list none.

For such an item the arXiv PDF *is* the work, not a preprint of some
other version of record, so this is a plain open-access source: no
`--allow-preprints`, no `pdf:preprint-version` tag.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import fetchers
from fetchers.arxiv import ArxivSource, arxiv_pdf_url


def test_the_pdf_url_comes_from_the_doi_in_either_case() -> None:
    assert arxiv_pdf_url("10.48550/ARXIV.2007.05443") == "https://arxiv.org/pdf/2007.05443"
    assert arxiv_pdf_url("10.48550/arxiv.2012.14406") == "https://arxiv.org/pdf/2012.14406"
    assert arxiv_pdf_url("10.1016/j.x.2020.1") is None


def test_a_non_arxiv_doi_costs_no_request(tmp_path) -> None:
    src = ArxivSource(MagicMock(), None)
    assert src.fetch_pdf("10.1016/j.x.2020.1", cache_dir=tmp_path) is None
    src.http.get.assert_not_called()


def test_it_downloads_and_validates(tmp_path) -> None:
    resp = MagicMock(status_code=200, content=b"%PDF-1.4\n" + b"0" * 3000 + b"\n%%EOF\n",
                     headers={"Content-Type": "application/pdf"})
    http = MagicMock()
    http.get.return_value = resp
    path, url = ArxivSource(http, None).fetch_pdf(
        "10.48550/ARXIV.2007.05443", cache_dir=tmp_path,
    )
    assert url == "https://arxiv.org/pdf/2007.05443" and path.exists()
    assert not fetchers.is_preprint_path(path)


def test_it_runs_by_default_ahead_of_the_aggregators() -> None:
    names = [s.name for s in fetchers.pdf_sources(MagicMock(), None)]
    assert "arxiv" in names
    assert names.index("arxiv") < names.index("openalex")
