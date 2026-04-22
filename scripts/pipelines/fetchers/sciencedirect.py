"""ScienceDirect / Elsevier — abstract via pybliometrics, PDF via Elsevier API.

ScienceDirect is Elsevier's full-text platform. The same API key is
used for abstract and PDF endpoints at different URLs. This file hosts
both capabilities because they're the same publisher, though the
abstract path uses pybliometrics while the PDF path goes through the
shared requests.Session.
"""

from __future__ import annotations

import logging
import os
import re
import urllib.parse
from pathlib import Path

from fetchers.base import AbstractFetcher, PdfFetcher

logger = logging.getLogger(__name__)

_ELSEVIER_BASE = "https://api.elsevier.com/content/article/doi"
_ELSEVIER_PREFIXES = (
    "10.1016/", "10.1006/", "10.1053/", "10.1054/",
    "10.1067/", "10.1074/", "10.1078/", "10.1383/",
)


def _doi_safe(doi: str) -> str:
    return doi.replace("/", "_").replace(":", "_")


def _cache_pdf_path(cache_dir: str | Path, doi: str) -> Path:
    return Path(cache_dir) / f"{_doi_safe(doi)}.pdf"


class ScienceDirectSource(AbstractFetcher, PdfFetcher):
    """Abstract via pybliometrics.sciencedirect.ArticleRetrieval;
    PDF via https://api.elsevier.com/content/article/doi/{doi}."""

    name = "sciencedirect"

    def _api_key(self) -> str:
        return (
            getattr(self.config, "elsevier_api_key", None)
            or os.environ.get("ELSEVIER_API_KEY", "")
        )

    def fetch_abstract(self, doi: str, *, title=None, cache_dir=None) -> str | None:
        try:
            from pybliometrics.utils.startup import init
            init()
            from pybliometrics.sciencedirect import ArticleRetrieval
        except Exception as e:
            logger.debug("pybliometrics import/init failed: %s", e)
            return None

        try:
            a = ArticleRetrieval(doi, view="FULL")
        except Exception as e:
            logger.debug("ScienceDirect ArticleRetrieval(%s) failed: %s", doi, e)
            return None

        if a.abstract:
            text = str(a.abstract).strip()
            if text:
                return text

        raw = str(a.originalText) if a.originalText else ""
        for pattern in (
            r"<abstract[^>]*>(.*?)</abstract>",
            r"<ce:abstract-sec[^>]*>(.*?)</ce:abstract-sec>",
        ):
            match = re.search(pattern, raw, re.DOTALL | re.IGNORECASE)
            if match:
                text = re.sub(r"<[^>]+>", " ", match.group(1))
                text = re.sub(r"\s+", " ", text).strip()
                if len(text) > 100:
                    return text
        return None

    def fetch_pdf(self, doi: str, *, cache_dir) -> tuple[Path, str] | None:
        if not any(doi.startswith(p) for p in _ELSEVIER_PREFIXES):
            return None
        key = self._api_key()
        if not key:
            return None
        path = _cache_pdf_path(cache_dir, doi)
        if path.exists():
            return path, f"cache://{path}"

        url = f"{_ELSEVIER_BASE}/{urllib.parse.quote(doi, safe='')}"
        try:
            resp = self.http.get(
                url,
                headers={"X-ELS-APIKey": key, "Accept": "application/pdf"},
                timeout=30,
            )
        except Exception as e:
            logger.debug("elsevier PDF %s failed: %s", doi, e)
            return None
        if resp.status_code != 200 or resp.content[:4] != b"%PDF":
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
        return path, url
