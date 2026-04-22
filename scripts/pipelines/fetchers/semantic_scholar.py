"""Semantic Scholar — abstract (by DOI, with title-search fallback)."""

from __future__ import annotations

import logging
import os
import urllib.parse

from fetchers.base import AbstractFetcher

logger = logging.getLogger(__name__)

_API_BASE = "https://api.semanticscholar.org/graph/v1"


class SemanticScholarSource(AbstractFetcher):
    name = "semantic_scholar"

    def _api_key(self) -> str:
        return (
            getattr(self.config, "semantic_scholar_api_key", None)
            or os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "")
        )

    def _headers(self) -> dict[str, str]:
        key = self._api_key()
        return {"x-api-key": key} if key else {}

    def fetch_abstract(self, doi: str, *, title=None, cache_dir=None) -> str | None:
        # Primary: look up by DOI.
        url = f"{_API_BASE}/paper/DOI:{doi}?fields=abstract"
        try:
            resp = self.http.get(url, headers=self._headers(), timeout=30)
        except Exception as e:
            logger.debug("semantic_scholar DOI lookup failed: %s", e)
            return None
        if resp.status_code == 200:
            abstract = (resp.json() or {}).get("abstract")
            if abstract:
                return abstract

        # Fallback: title search, then filter results by DOI match.
        if not title:
            return None
        return self._fetch_by_title(doi, title)

    def _fetch_by_title(self, doi: str, title: str) -> str | None:
        encoded = urllib.parse.quote(title[:100])
        url = (
            f"{_API_BASE}/paper/search"
            f"?query={encoded}&fields=externalIds,abstract&limit=5"
        )
        try:
            resp = self.http.get(url, headers=self._headers(), timeout=30)
        except Exception as e:
            logger.debug("semantic_scholar title search failed: %s", e)
            return None
        if resp.status_code != 200:
            return None
        doi_norm = doi.lower().strip()
        for hit in (resp.json() or {}).get("data") or []:
            ext = hit.get("externalIds") or {}
            if (ext.get("DOI") or "").lower().strip() == doi_norm:
                return hit.get("abstract") or None
        return None
