"""SpringerLink — direct PDF download for Springer Nature DOIs.

No API key. The URL is public, and this was believed to be gated only by
institutional network access (FinELib / campus VPN).

**That is not what happens in practice, as of 2026-08.** SpringerLink
answers `content/pdf/<doi>.pdf` with an identical ~3 KB HTML page titled
`Client Challenge` — an Imperva JavaScript interstitial — for every
request from an HTTP client. Measured from an on-campus IP
(`*.aalto.fi`) across ten DOIs including licensed titles, and unchanged
by a complete browser header set: the same 3038 bytes every time. Being
entitled makes no difference, because the challenge is served before
entitlement is ever evaluated.

So this source reliably returns None in the automated cascade, and
Springer DOIs have to be recovered through the browser pass instead
(`fetchers/browser/`), where a real JS engine can clear the challenge.
It is kept in the stage-1 list because asking costs one cheap request
that fails fast, and the block may be relaxed at any time — but never
read a Springer miss as evidence that an article is unavailable.
"""

from __future__ import annotations

import logging
import urllib.parse
from pathlib import Path

from fetchers import _pdf_validate
from fetchers.base import PdfFetcher

logger = logging.getLogger(__name__)

#: `10.1023` is Kluwer Academic, absorbed into Springer in 2004; its
#: titles serve from link.springer.com under the same URL shape as
#: `10.1007`. Legacy imprints like this are where a prefix list quietly
#: costs retrieval — a live 1,895-item pass left 12 Kluwer-era items
#: with no handler at all, so they fell through to the resolver route
#: for want of one line.
_SPRINGER_PREFIXES = (
    "10.1007/", "10.1023/", "10.1057/", "10.1038/", "10.1140/",
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
