"""Zotero's `DOI` field sometimes holds a full `https://doi.org/...` URL
rather than a bare DOI (seen from native Zotero EBSCO-export imports that
bypass `import_to_zotero.py`'s Crossref-based DOI handling).

Every prefix-filtering PdfFetcher (`ScienceDirectSource`, `WileySource`,
`SpringerSource`) checks `doi.startswith(<registrant prefix>)` — a
URL-wrapped DOI never matches, so `PdfFetcher.handles_doi()` returns False
and `_run_api_cascade` buckets the item as "unhandled": never attempted,
never logged. Even where a fetcher is reached anyway (bypass_prefix_filter),
the raw URL string gets interpolated straight into the API request, which
404s regardless of subscription or entitlement.

The fix is to strip the URL/`doi:` wrapper (via `doi_utils.strip_doi_prefixes`)
before the DOI is used for prefix matching or handed to a fetcher.
"""

from __future__ import annotations

import argparse
import csv
import io
from pathlib import Path
from unittest.mock import MagicMock

from enrich_pdfs import LOG_FIELDS, _run_api_cascade
from fetchers.base import PdfFetcher


def _make_args(cache_dir: str) -> argparse.Namespace:
    return argparse.Namespace(
        workers=1,
        cache_dir=cache_dir,
        failure_log_csv=None,
        dry_run=False,
    )


def _fake_pdf(marker: bytes = b"body") -> bytes:
    return b"%PDF-1.4\n" + marker + b"\n" + b"0" * 2000 + b"\n%%EOF\n"


def _make_log_writer():
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=LOG_FIELDS)
    writer.writeheader()
    return writer


class _FakeElsevier(PdfFetcher):
    """Minimal prefix-filtering fetcher matching the real sources' shape.

    Records every DOI it is actually called with, so the test can assert
    on the exact string `fetch_pdf` receives — not just whether it was
    reached — since a URL-wrapped DOI reaching `fetch_pdf` unstripped
    would still 404 against the real API.
    """

    name = "sciencedirect"
    doi_prefixes = ("10.1016/",)

    def __init__(self):
        super().__init__()
        self.called_with: list[str] = []

    def fetch_pdf(self, doi, *, cache_dir, bypass_prefix_filter=False):
        self.called_with.append(doi)
        if not bypass_prefix_filter and not doi.startswith("10.1016/"):
            return None
        path = Path(cache_dir) / "fake.pdf"
        path.write_bytes(_fake_pdf())
        return path, self.name


def test_url_wrapped_doi_is_still_routed_and_fetched(tmp_path: Path) -> None:
    item = {
        "key": "ITEM1",
        "data": {
            "DOI": "https://doi.org/10.1016/j.obhdp.2011.09.002",
            "title": "T",
        },
    }
    source = _FakeElsevier()
    zot = MagicMock()
    args = _make_args(str(tmp_path))

    _run_api_cascade([item], [source], args, "2026-06-13", zot, _make_log_writer())

    assert source.called_with == ["10.1016/j.obhdp.2011.09.002"], (
        "fetch_pdf must receive the bare DOI, not the doi.org URL wrapper"
    )
    zot.attach_pdf.assert_called_once()
