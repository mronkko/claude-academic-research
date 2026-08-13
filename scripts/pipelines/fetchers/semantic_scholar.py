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

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Set once a 403 proves SEMANTIC_SCHOLAR_API_KEY is being rejected,
        # so later calls on this instance (reused across every item in the
        # enrichment run) stop resending the dead key and re-warning.
        self._key_rejected = False

    def _api_key(self) -> str:
        if self._key_rejected:
            return ""
        return (
            getattr(self.config, "semantic_scholar_api_key", None)
            or os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "")
        )

    def _headers(self) -> dict[str, str]:
        key = self._api_key()
        return {"x-api-key": key} if key else {}

    def _get(self, url: str):
        """GET with automatic one-time fallback to unauthenticated if the
        configured key is rejected (403). A 403 with a key attached means
        the key itself is invalid/revoked — anonymous calls to the same
        endpoints succeed — not a scope/plan restriction."""
        used_key = bool(self._api_key())
        resp = self.http.get(url, headers=self._headers(), timeout=30)
        if resp.status_code == 403 and used_key and not self._key_rejected:
            print(
                "  WARNING: SEMANTIC_SCHOLAR_API_KEY was rejected (403 "
                "Forbidden) by the Semantic Scholar API — the key appears "
                "invalid or revoked. Continuing unauthenticated (lower, "
                "shared rate limit applies). Rotate the key via `/setup` "
                "when convenient.",
                flush=True,
            )
            self._key_rejected = True
            resp = self.http.get(url, headers=self._headers(), timeout=30)
        return resp

    def fetch_abstract(self, doi: str, *, title=None, cache_dir=None) -> str | None:
        # Primary: look up by DOI.
        url = f"{_API_BASE}/paper/DOI:{doi}?fields=abstract"
        try:
            resp = self._get(url)
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
            resp = self._get(url)
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
