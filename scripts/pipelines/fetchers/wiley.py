"""Wiley — PDF download via the first-party Text and Data Mining client.

Uses the `wiley-tdm` library, which handles authentication, rate
limiting, and the download retry loop. `WILEY_TDM_TOKEN` is required.

**Calls are serialised, on purpose.** `wiley-tdm` rate-limits per client
instance, and this used to build a fresh `TDMClient` for every DOI — so
the cascade's six worker threads produced six independent limiters,
none of which could see the aggregate. Wiley throttled the excess, the
client raised, and the handler swallowed it into `logger.debug`, which
is invisible at normal log level. The result looked exactly like "this
article is not available".

Measured on one live library, same token and the same Wiley-prefix
items both times: **47** PDFs at `--workers 4`, **110** when the same
items were retried one at a time. Less than half the yield, lost
silently. One shared client behind one lock costs Wiley items their
parallelism — they were never really parallel — and every other source
in the cascade keeps running concurrently around it.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from fetchers.base import PdfFetcher

logger = logging.getLogger(__name__)

#: One client for the process, and one caller inside it at a time, so the
#: library's own rate limiter sees every request this run makes. Built
#: lazily because constructing it needs a token we may not have.
_CLIENT_LOCK = threading.Lock()
_CLIENT: object | None = None
_CLIENT_KEY: tuple[str, str] | None = None

#: `10.1348` is the British Psychological Society, whose journals Wiley
#: publishes and serves from onlinelibrary.wiley.com like any other —
#: the TDM endpoint accepts it, it was simply never listed.
_WILEY_PREFIXES = ("10.1002/", "10.1111/", "10.1046/", "10.1348/")


class WileySource(PdfFetcher):
    name = "wiley"
    doi_prefixes = _WILEY_PREFIXES
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
        # One client, one caller at a time. The lock is held across the
        # download, not just the construction: it is the *requests* that
        # must be rate-limited, and a shared client called from six
        # threads would throttle exactly as badly as six clients did.
        global _CLIENT, _CLIENT_KEY
        with _CLIENT_LOCK:
            key = (token, str(cache_dir))
            if _CLIENT is None or _CLIENT_KEY != key:
                _CLIENT = TDMClient(
                    api_token=token, download_dir=str(cache_dir),
                )
                _CLIENT_KEY = key
            try:
                results = _CLIENT.download_pdfs([doi])
            except Exception as e:
                # Was `logger.debug`, which is why losing half the yield
                # to throttling looked like "not available". A failure to
                # *ask* is not an answer and must be visible.
                logger.warning("wiley TDM download(%s) failed: %s", doi, e)
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
