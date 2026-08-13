"""Semantic Scholar — abstracts, and open-access PDFs via `openAccessPdf`.

Both capabilities from one class, sharing one key, one session, and one
rejected-key fallback. The PDF half was added because the abstract half
was already asking Semantic Scholar about the same DOIs and throwing
away the `openAccessPdf` field sitting in the response: the cheapest
real coverage gain available, with no new credential and no new
provider to configure.

Its value is complementary rather than duplicative. Unpaywall indexes
publisher and repository OA; Semantic Scholar's S2 corpus also surfaces
author-deposited copies that Unpaywall's `best_oa_location` misses, and
it answers by DOI *or* by title, which matters for the records whose
DOI never resolved.
"""

from __future__ import annotations

import logging
import os
import urllib.parse
from pathlib import Path

from fetchers import _pdf_validate
from fetchers.base import AbstractFetcher, PdfFetcher

logger = logging.getLogger(__name__)

_API_BASE = "https://api.semanticscholar.org/graph/v1"


def _doi_safe(doi: str) -> str:
    return doi.replace("/", "_").replace(":", "_")


def _cache_pdf_path(cache_dir: str | Path, doi: str) -> Path:
    return Path(cache_dir) / f"{_doi_safe(doi)}.pdf"


class SemanticScholarSource(AbstractFetcher, PdfFetcher):
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

    # -- PdfFetcher ----------------------------------------------------

    def _open_access_pdf_url(self, doi: str) -> str | None:
        """The `openAccessPdf.url` S2 reports for this DOI, if any.

        One field on the same paper endpoint the abstract half already
        calls. S2 populates it only when it has resolved an actually
        downloadable copy, so an empty answer here is a real negative
        rather than a lookup that needs retrying elsewhere.
        """
        url = f"{_API_BASE}/paper/DOI:{doi}?fields=openAccessPdf"
        try:
            resp = self._get(url)
        except Exception as e:
            logger.debug("semantic_scholar openAccessPdf lookup failed: %s", e)
            return None
        if resp.status_code != 200:
            return None
        oa = (resp.json() or {}).get("openAccessPdf") or {}
        return (oa.get("url") or "").strip() or None

    def fetch_pdf(
        self, doi: str, *, cache_dir, bypass_prefix_filter: bool = False,
    ) -> tuple[Path, str] | None:
        del bypass_prefix_filter          # not prefix-filtered
        path = _cache_pdf_path(cache_dir, doi)
        if path.exists():
            # Same rule as every other fetcher: validate before serving a
            # cached file, or an earlier truncated download becomes
            # permanent because every later run short-circuits on it.
            defect = _pdf_validate.file_defect(path)
            if defect is None:
                return path, f"cache://{path}"
            logger.warning("discarding cached PDF for %s — %s", doi, defect)
            path.unlink(missing_ok=True)

        pdf_url = self._open_access_pdf_url(doi)
        if not pdf_url:
            return None

        try:
            resp = self.http.get(
                pdf_url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=60,
                allow_redirects=True,
            )
        except Exception as e:
            logger.debug("semantic_scholar PDF %s failed: %s", pdf_url, e)
            return None

        defect = _pdf_validate.response_defect(resp)
        if defect is not None:
            # None rather than an exception, so the cascade moves on: S2
            # links out to repositories that sometimes serve a landing
            # page or a truncated file where another source has the
            # article intact.
            logger.warning("%s: rejected PDF for %s — %s", self.name, doi, defect)
            return None

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
        return path, pdf_url
