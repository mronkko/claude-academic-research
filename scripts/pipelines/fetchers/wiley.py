"""Wiley — PDF download via the first-party Text and Data Mining client.

Uses the `wiley-tdm` library, which handles authentication, rate
limiting, and the download retry loop. `WILEY_TDM_TOKEN` is required.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fetchers.base import PdfFetcher

logger = logging.getLogger(__name__)

_WILEY_PREFIXES = ("10.1002/", "10.1111/", "10.1046/")


class WileySource(PdfFetcher):
    name = "wiley"
    direct_access_domains = ("onlinelibrary.wiley.com", "wiley.com")

    def _token(self) -> str:
        return (
            getattr(self.config, "wiley_tdm_token", None)
            or os.environ.get("WILEY_TDM_TOKEN", "")
        )

    def fetch_pdf(
        self, doi: str, *, cache_dir, bypass_prefix_filter: bool = False,
    ) -> tuple[Path, str] | None:
        if (not bypass_prefix_filter
                and not any(doi.startswith(p) for p in _WILEY_PREFIXES)):
            return None
        token = self._token()
        if not token:
            return None
        try:
            from wiley_tdm import TDMClient
            from wiley_tdm.download_status import DownloadStatus
        except Exception as e:
            logger.debug("wiley-tdm import failed: %s", e)
            return None

        os.makedirs(cache_dir, exist_ok=True)
        client = TDMClient(api_token=token, download_dir=str(cache_dir))
        try:
            results = client.download_pdfs([doi])
        except Exception as e:
            logger.debug("wiley TDM download(%s) failed: %s", doi, e)
            return None
        if not results:
            return None
        r = results[0]
        if r.status != DownloadStatus.SUCCESS:
            return None
        path_str = getattr(r, "file_path", None) or getattr(r, "path", None)
        if not path_str:
            return None
        path = Path(str(path_str))
        if not path.exists():
            return None
        return path, f"wiley-tdm://{doi}"
