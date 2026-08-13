"""CORE (core.ac.uk) — full text from institutional repositories.

CORE aggregates ~300M open-access records harvested from university and
funder repositories. That is a different population from the other
fetchers here, and specifically the useful one for this plugin's hardest
bucket: management and organisational-behaviour articles published by
Sage, the Academy of Management, and APA, which sit behind Cloudflare at
the publisher and are frequently deposited by their authors as accepted
manuscripts in an institutional repository.

Two consequences worth stating plainly.

**What CORE serves is usually the accepted manuscript, not the version
of record.** Post-peer-review, pre-typesetting: the content matches, the
pagination does not. For screening and coding that is fine and is why
this fetcher exists. For quoting a page number it is not, which is why
every attachment from here is tagged `pdf:repository-copy` so the
provenance survives into the coding stage rather than being lost the
moment the file lands in Zotero.

**Its DOI coverage is uneven**, because repositories deposit metadata
with varying care. So a miss here is weak evidence — it means "not found
in this index", not "no accessible copy exists", and the cascade should
keep going.

An API key is free and self-service. Without one CORE returns 401 on
every endpoint, so this fetcher stays out of the cascade entirely rather
than burning a request per item to be told no.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fetchers import _pdf_validate
from fetchers.base import PdfFetcher

logger = logging.getLogger(__name__)

_API_BASE = "https://api.core.ac.uk/v3"

#: Filename marker identifying a cache file as CORE-sourced. Same
#: mechanism as `sciencedirect._TDM_RECOVERED_SUFFIX`: by attach time the
#: orchestrator holds only a path, so provenance has to be recoverable
#: from the filename rather than threaded through the ABC's return type.
_REPOSITORY_COPY_SUFFIX = "-repository-copy"

#: Applied to every attachment this source produces. The repository copy
#: is normally the accepted manuscript rather than the published article,
#: and a coding stage that quotes page numbers needs to know that.
#: Follows the same `pdf:<status>` convention as `pdf:tdm-recovered`.
REPOSITORY_COPY_TAG = "pdf:repository-copy"


def _doi_safe(doi: str) -> str:
    return doi.replace("/", "_").replace(":", "_")


def _cache_pdf_path(cache_dir: str | Path, doi: str) -> Path:
    return Path(cache_dir) / f"{_doi_safe(doi)}{_REPOSITORY_COPY_SUFFIX}.pdf"


class CoreSource(PdfFetcher):
    name = "core"

    def _api_key(self) -> str:
        return (
            getattr(self.config, "core_api_key", None)
            or os.environ.get("CORE_API_KEY", "")
        )

    def _headers(self) -> dict[str, str]:
        key = self._api_key()
        return {"Authorization": f"Bearer {key}"} if key else {}

    def _download_url(self, doi: str) -> str | None:
        """CORE's `downloadUrl` for this DOI, if it has a full text.

        Searched by DOI rather than fetched by ID because CORE has no
        DOI-keyed endpoint — `search/works` with a `doi:` filter is the
        documented route, and it returns the best-matching work first.
        """
        try:
            resp = self.http.get(
                f"{_API_BASE}/search/works",
                params={"q": f'doi:"{doi}"', "limit": 3},
                headers=self._headers(),
                timeout=30,
            )
        except Exception as e:
            logger.debug("core search for %s failed: %s", doi, e)
            return None
        if resp.status_code == 401:
            logger.warning(
                "core: CORE_API_KEY rejected (401). Add or rotate it via "
                "`/setup`; skipping this source.",
            )
            return None
        if resp.status_code != 200:
            return None

        doi_norm = doi.lower().strip()
        for hit in (resp.json() or {}).get("results") or []:
            # Confirm the DOI rather than trusting rank: CORE's search is
            # fuzzy, and a near-miss here would attach a *different
            # paper's* full text — the one failure mode worse than
            # attaching nothing.
            if (hit.get("doi") or "").lower().strip() != doi_norm:
                continue
            url = (hit.get("downloadUrl") or "").strip()
            if url:
                return url
        return None

    def fetch_pdf(
        self, doi: str, *, cache_dir, bypass_prefix_filter: bool = False,
    ) -> tuple[Path, str] | None:
        del bypass_prefix_filter          # not prefix-filtered
        if not self._api_key():
            return None                   # every endpoint 401s without one

        path = _cache_pdf_path(cache_dir, doi)
        if path.exists():
            defect = _pdf_validate.file_defect(path)
            if defect is None:
                return path, f"cache://{path}"
            logger.warning("discarding cached PDF for %s — %s", doi, defect)
            path.unlink(missing_ok=True)

        url = self._download_url(doi)
        if not url:
            return None

        try:
            resp = self.http.get(
                url,
                headers={**self._headers(), "User-Agent": "Mozilla/5.0"},
                timeout=60,
                allow_redirects=True,
            )
        except Exception as e:
            logger.debug("core PDF %s failed: %s", url, e)
            return None

        defect = _pdf_validate.response_defect(resp)
        if defect is not None:
            # Repositories serve landing pages, splash pages and embargo
            # notices from the same URL shape as the file itself.
            logger.warning("%s: rejected PDF for %s — %s", self.name, doi, defect)
            return None

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
        return path, url


def is_repository_copy_path(path: str | Path) -> bool:
    """True when `path` is a cache file this source produced.

    Mirrors `sciencedirect.is_tdm_recovered_path`, and is read at attach
    time to apply `REPOSITORY_COPY_TAG`.
    """
    return Path(path).stem.endswith(_REPOSITORY_COPY_SUFFIX)
