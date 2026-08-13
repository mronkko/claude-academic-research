"""Unpaywall — locate and download an open-access PDF copy for a DOI."""

from __future__ import annotations

import logging
import os
import urllib.parse
from pathlib import Path

from fetchers import _pdf_validate
from fetchers.base import PdfFetcher

logger = logging.getLogger(__name__)

_API_BASE = "https://api.unpaywall.org/v2"


def _doi_safe(doi: str) -> str:
    return doi.replace("/", "_").replace(":", "_")


def _cache_pdf_path(cache_dir: str | Path, doi: str) -> Path:
    return Path(cache_dir) / f"{_doi_safe(doi)}.pdf"


class UnpaywallSource(PdfFetcher):
    name = "unpaywall"

    def _mailto(self) -> str:
        return (
            getattr(self.config, "crossref_mailto", None)
            or os.environ.get("CROSSREF_MAILTO", "")
        )

    def fetch_pdf(
        self, doi: str, *, cache_dir, bypass_prefix_filter: bool = False,
    ) -> tuple[Path, str] | None:
        del bypass_prefix_filter          # not prefix-filtered
        mailto = self._mailto()
        if not mailto:
            return None
        path = _cache_pdf_path(cache_dir, doi)
        if path.exists():
            # Validate before serving: an entry written by an earlier,
            # unvalidated run may be truncated, and returning it unchecked
            # made the corruption permanent — every later run
            # short-circuited on the bad file instead of re-fetching.
            _defect = _pdf_validate.file_defect(path)
            if _defect is None:
                return path, f"cache://{path}"
            logger.warning("discarding cached PDF for %s — %s", doi, _defect)
            path.unlink(missing_ok=True)

        lookup = f"{_API_BASE}/{urllib.parse.quote(doi, safe='')}?email={mailto}"
        try:
            meta = self.http.get(lookup, timeout=30)
        except Exception as e:
            logger.debug("unpaywall lookup %s failed: %s", doi, e)
            return None
        if meta.status_code != 200:
            return None
        data = meta.json() or {}

        best = data.get("best_oa_location") or {}
        pdf_url = best.get("url_for_pdf") or best.get("url")
        if not pdf_url:
            for loc in data.get("oa_locations") or []:
                if loc.get("url_for_pdf"):
                    pdf_url = loc["url_for_pdf"]
                    break
        if not pdf_url:
            return None

        ua = f"mailto:{mailto}" if mailto else "Mozilla/5.0"
        try:
            resp = self.http.get(pdf_url, headers={"User-Agent": ua}, timeout=60)
        except Exception as e:
            logger.debug("unpaywall PDF %s failed: %s", pdf_url, e)
            return None
        _defect = _pdf_validate.response_defect(resp)
        if _defect is not None:
            # None (not an exception) so the cascade falls through to the
            # next source — a truncated copy at one provider is often
            # served intact by another.
            logger.warning("%s: rejected PDF for %s — %s", self.name, doi, _defect)
            return None

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
        return path, pdf_url
