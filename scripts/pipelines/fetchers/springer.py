"""SpringerLink — direct PDF download for Springer Nature DOIs.

Requires institutional access (e.g. FinELib or campus VPN) — the URL
is public but gated by the network. No API key.
"""

from __future__ import annotations

import logging
import urllib.parse
from pathlib import Path

from fetchers import _pdf_validate
from fetchers.base import PdfFetcher

logger = logging.getLogger(__name__)

_SPRINGER_PREFIXES = (
    "10.1007/", "10.1057/", "10.1038/", "10.1140/",
    "10.1186/", "10.1365/", "10.1245/",
)


def _doi_safe(doi: str) -> str:
    return doi.replace("/", "_").replace(":", "_")


def _cache_pdf_path(cache_dir: str | Path, doi: str) -> Path:
    return Path(cache_dir) / f"{_doi_safe(doi)}.pdf"


class SpringerSource(PdfFetcher):
    name = "springer"
    direct_access_domains = ("link.springer.com", "springer.com")

    def fetch_pdf(
        self, doi: str, *, cache_dir, bypass_prefix_filter: bool = False,
    ) -> tuple[Path, str] | None:
        if (not bypass_prefix_filter
                and not any(doi.startswith(p) for p in _SPRINGER_PREFIXES)):
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

        encoded = urllib.parse.quote(doi, safe="")
        url = f"https://link.springer.com/content/pdf/{encoded}.pdf"
        try:
            resp = self.http.get(
                url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30,
            )
        except Exception as e:
            logger.debug("springer %s failed: %s", doi, e)
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
        return path, url
