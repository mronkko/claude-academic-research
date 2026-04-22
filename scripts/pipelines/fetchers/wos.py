"""Web of Science — abstract retrieval via the WoS Expanded or Starter API.

Two-phase lookup strategy:
  1. Query by DOI (`DO=(<doi>)`). Most papers resolve here.
  2. Fallback by title (`TI=(<cleaned title>)`) when DOI lookup misses
     but a title is available. WoS sometimes indexes a paper under a
     different publisher DOI than the one in Zotero — common for AoM
     journals whose DOIs were reissued after publisher transfers
     (e.g. Annals `10.1080/...` in WoS vs `10.5465/...` in the
     library).

Requires `WOS_API_KEY_EXTENDED`. Falls back to `WOS_API_KEY` (Starter
tier) if only the starter key is configured — Starter has a narrower
query language but supports `DO=` and `TI=` for abstract lookup.
"""

from __future__ import annotations

import logging
import os

from fetchers._title_match import matches, strip_html
from fetchers.base import AbstractFetcher

logger = logging.getLogger(__name__)

_EXPANDED_URL = "https://api.clarivate.com/api/wos"
_STARTER_URL = "https://api.clarivate.com/apis/wos-starter/v1/documents"


class WosSource(AbstractFetcher):
    name = "wos"

    def _key_and_tier(self) -> tuple[str, str]:
        """Return (api_key, tier) where tier is 'expanded' or 'starter'."""
        extended = (
            getattr(self.config, "wos_api_key_extended", None)
            or os.environ.get("WOS_API_KEY_EXTENDED", "")
        )
        if extended:
            return extended, "expanded"
        starter = (
            getattr(self.config, "wos_api_key", None)
            or os.environ.get("WOS_API_KEY", "")
        )
        if starter:
            return starter, "starter"
        return "", ""

    def fetch_abstract(self, doi: str, *, title=None, cache_dir=None) -> str | None:
        del cache_dir                 # WoS fetchers don't use the cache dir
        key, tier = self._key_and_tier()
        if not key or self.http is None:
            return None
        fetcher = self._fetch_expanded if tier == "expanded" else self._fetch_starter
        return fetcher(doi, title, key)

    # ------------------------------------------------------------------
    # Expanded tier (richer XML/JSON payload, real abstract element)
    # ------------------------------------------------------------------

    def _fetch_expanded(
        self, doi: str, title: str | None, key: str,
    ) -> str | None:
        headers = {"X-ApiKey": key, "Accept": "application/json"}

        # Phase 1: DOI.
        text = self._extract_expanded_abstract_from_query(
            query=f"DO=({doi})", headers=headers,
        )
        if text:
            return text

        # Phase 2: title fallback.
        if not title:
            return None
        # Guard against double-quotes in the title breaking the query.
        cleaned_title = strip_html(title).replace('"', "").strip()
        if not cleaned_title:
            return None
        # WoS `TI=(...)` with unquoted tokens does keyword-AND matching,
        # which survives subtitle-length and HTML-tag mismatches between
        # Zotero and WoS.  A quoted phrase would require exact match and
        # silently drops to 0 hits when anything differs (publisher
        # added/removed a subtitle, or has <i> embedded in the stored
        # title).  The shortlist is then re-filtered in Python via
        # `matches()` so false-positive keyword hits don't return the
        # wrong abstract.
        hits = self._expanded_search(
            query=f"TI=({cleaned_title[:200]})", headers=headers, count=5,
        )
        for rec in hits:
            rec_title = self._expanded_title(rec)
            if rec_title and matches(rec_title, title):
                text = self._expanded_abstract(rec)
                if text:
                    return text
        return None

    def _extract_expanded_abstract_from_query(
        self, query: str, headers: dict,
    ) -> str | None:
        hits = self._expanded_search(query, headers, count=1)
        if not hits:
            return None
        return self._expanded_abstract(hits[0])

    def _expanded_search(
        self, query: str, headers: dict, *, count: int,
    ) -> list[dict]:
        try:
            resp = self.http.get(
                _EXPANDED_URL,
                headers=headers,
                params={
                    "databaseId": "WOK",
                    "usrQuery": query,
                    "count": count,
                    "firstRecord": 1,
                },
                timeout=30,
            )
        except Exception as e:
            logger.debug("wos expanded %s failed: %s", query, e)
            return []
        if resp.status_code != 200:
            return []
        data = resp.json() or {}
        if data.get("QueryResult", {}).get("RecordsFound", 0) == 0:
            return []
        rec = (
            data.get("Data", {})
            .get("Records", {})
            .get("records", {})
            .get("REC")
        )
        if rec is None:
            return []
        return rec if isinstance(rec, list) else [rec]

    @staticmethod
    def _expanded_title(rec: dict) -> str:
        titles = (
            rec.get("static_data", {})
            .get("summary", {})
            .get("titles", {})
            .get("title", [])
        )
        if not isinstance(titles, list):
            titles = [titles]
        for t in titles:
            if isinstance(t, dict) and t.get("type") == "item":
                return str(t.get("content", ""))
        return ""

    @staticmethod
    def _expanded_abstract(rec: dict) -> str | None:
        block = (
            rec.get("static_data", {})
            .get("fullrecord_metadata", {})
            .get("abstracts", {})
        )
        if not isinstance(block, dict) or block.get("count", 0) == 0:
            return None
        inner = block.get("abstract", {})
        if isinstance(inner, list):
            inner = inner[0] if inner else {}
        text = inner.get("abstract_text", {}).get("p", "") if isinstance(inner, dict) else ""
        if isinstance(text, list):
            text = " ".join(str(p) for p in text)
        text = str(text).strip()
        return text if len(text) > 40 else None

    # ------------------------------------------------------------------
    # Starter tier (simpler payload)
    # ------------------------------------------------------------------

    def _fetch_starter(
        self, doi: str, title: str | None, key: str,
    ) -> str | None:
        headers = {"X-ApiKey": key, "Accept": "application/json"}

        text = self._starter_abstract_from_query(f"DO=({doi})", headers)
        if text:
            return text
        if not title:
            return None
        cleaned_title = strip_html(title).replace('"', "").strip()
        if not cleaned_title:
            return None
        hits = self._starter_search(
            f'TI=("{cleaned_title[:100]}")', headers, limit=5,
        )
        for hit in hits:
            hit_title = (hit.get("title") or {}).get("value") or ""
            if hit_title and matches(hit_title, title):
                abstract = hit.get("abstract") or ""
                if len(abstract.strip()) > 40:
                    return abstract.strip()
        return None

    def _starter_abstract_from_query(
        self, query: str, headers: dict,
    ) -> str | None:
        hits = self._starter_search(query, headers, limit=1)
        if not hits:
            return None
        abstract = hits[0].get("abstract") or ""
        return abstract.strip() if len(abstract.strip()) > 40 else None

    def _starter_search(
        self, query: str, headers: dict, *, limit: int,
    ) -> list[dict]:
        try:
            resp = self.http.get(
                _STARTER_URL,
                headers=headers,
                params={"q": query, "limit": limit, "page": 1, "db": "WOS"},
                timeout=30,
            )
        except Exception as e:
            logger.debug("wos starter %s failed: %s", query, e)
            return []
        if resp.status_code != 200:
            return []
        data = resp.json() or {}
        hits = data.get("hits") or []
        return hits if isinstance(hits, list) else []
