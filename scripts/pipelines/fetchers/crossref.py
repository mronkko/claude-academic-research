"""Crossref — abstract and PDF (via text-and-data-mining links).

Crossref is the non-profit that registers scholarly DOIs and holds
publisher-deposited abstracts and TDM URLs. Uses `habanero` for the
JSON metadata fetch and `requests.Session` for the PDF byte download.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

from fetchers.base import AbstractFetcher, PdfFetcher

if TYPE_CHECKING:
    import habanero

logger = logging.getLogger(__name__)


def _doi_safe(doi: str) -> str:
    return doi.replace("/", "_").replace(":", "_")


def _cache_pdf_path(cache_dir: str | Path, doi: str) -> Path:
    return Path(cache_dir) / f"{_doi_safe(doi)}.pdf"


def _strip_jats(abstract_html: str) -> str | None:
    """Crossref abstracts arrive with JATS XML tags. Strip to plain text."""
    text = re.sub(r"<[^>]+>", " ", abstract_html)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) > 50 else None


class CrossrefSource(AbstractFetcher, PdfFetcher):
    name = "crossref"

    def __init__(self, http, config=None):
        super().__init__(http, config)
        self._cr: habanero.Crossref | None = None

    @property
    def cr(self) -> habanero.Crossref:
        if self._cr is None:
            import habanero
            mailto = getattr(self.config, "crossref_mailto", None) or os.environ.get(
                "CROSSREF_MAILTO", ""
            )
            self._cr = habanero.Crossref(mailto=mailto or None)
        return self._cr

    def fetch_abstract(self, doi: str, *, title=None, cache_dir=None) -> str | None:
        try:
            msg = self.cr.works(ids=doi).get("message") or {}
        except Exception as e:
            logger.debug("crossref.works(%s) failed: %s", doi, e)
            return None
        abstract = msg.get("abstract")
        if not abstract:
            return None
        return _strip_jats(abstract)

    def fetch_pdf(self, doi: str, *, cache_dir) -> tuple[Path, str] | None:
        path = _cache_pdf_path(cache_dir, doi)
        if path.exists():
            return path, f"cache://{path}"

        try:
            msg = self.cr.works(ids=doi).get("message") or {}
        except Exception as e:
            logger.debug("crossref.works(%s) failed: %s", doi, e)
            return None

        pdf_url = None
        for link in msg.get("link", []) or []:
            if (
                link.get("intended-application") == "text-mining"
                and link.get("content-type") == "application/pdf"
            ):
                pdf_url = link.get("URL")
                break
        if not pdf_url:
            return None

        mailto = getattr(self.config, "crossref_mailto", None) or os.environ.get(
            "CROSSREF_MAILTO", ""
        )
        headers = {
            "Accept": "application/pdf",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        }
        if mailto:
            headers["CR-Clickthrough-Client-Token"] = mailto

        try:
            resp = self.http.get(pdf_url, headers=headers, timeout=60)
        except Exception as e:
            logger.debug("crossref PDF %s download failed: %s", pdf_url, e)
            return None
        if resp.status_code != 200 or resp.content[:4] != b"%PDF":
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
        return path, pdf_url
