"""DOI → canonical resource URL resolver via Crossref.

Used by the browser pipeline to route DOIs to the correct publisher
handler when the DOI's prefix is misleading. The common case: a
journal migrates publishers (ETAP moved from Wiley to Sage around
2021), and old DOIs retain the original prefix (10.1111/etap.*)
while the content is now hosted on the new publisher's platform
(journals.sagepub.com).

Crossref's `URL` field for a work is the DOI-registered primary
resource URL and is kept up-to-date by the current publisher. That
makes it a more reliable routing signal than either the DOI prefix
or following the doi.org redirect chain (publisher landing pages
sometimes wrap behind click-throughs or session-scoped intermediate
hosts).

Results are cached on disk (`<cache_dir>/doi_resolver_cache.json`) so
a repeat run against the same library makes zero Crossref calls.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from habanero import Crossref

logger = logging.getLogger(__name__)


@dataclass
class DoiResolution:
    """The routing-relevant fields we keep from Crossref's work metadata."""

    url: str = ""                  # canonical resource URL, e.g. "https://journals.sagepub.com/doi/…"
    publisher: str = ""            # e.g. "SAGE Publications"
    issn: str = ""                 # first ISSN when Crossref lists several


class DoiResolverCache:
    """On-disk {doi: DoiResolution-dict} cache.

    Lives next to the SFX cache so "clear the cache dir" resets both
    without a bespoke command. Corrupt cache files don't block the
    pipeline — they're treated as empty.
    """

    def __init__(self, cache_dir: str | Path) -> None:
        self.path = Path(cache_dir) / "doi_resolver_cache.json"
        self._data: dict[str, dict] = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text())
            except Exception:
                self._data = {}

    def get(self, doi: str) -> DoiResolution | None:
        raw = self._data.get(doi)
        if not raw:
            return None
        return DoiResolution(
            url=str(raw.get("url", "")),
            publisher=str(raw.get("publisher", "")),
            issn=str(raw.get("issn", "")),
        )

    def put(self, doi: str, resolution: DoiResolution) -> None:
        self._data[doi] = asdict(resolution)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, indent=1))
        except Exception as e:
            logger.debug("DoiResolverCache write failed: %s", e)


def _extract_resolution(msg: dict) -> DoiResolution:
    """Pull the three routing-relevant fields out of a Crossref `message`.

    The `URL` field is the canonical DOI-registered URL. Crossref
    sometimes also carries `resource.primary.URL` which usually
    matches `URL`; when they differ the primary URL is slightly more
    specific (platform-on-record at time of last metadata update).
    Prefer primary, fall back to URL.
    """
    resource = msg.get("resource") or {}
    primary = (resource.get("primary") or {}).get("URL", "")
    url = str(primary or msg.get("URL", "") or "")
    publisher = str(msg.get("publisher", "") or "")
    issns = msg.get("ISSN") or []
    issn = str(issns[0]) if issns else ""
    return DoiResolution(url=url, publisher=publisher, issn=issn)


def resolve_doi(
    doi: str,
    *,
    crossref: Crossref,
    cache: DoiResolverCache | None = None,
) -> DoiResolution | None:
    """Resolve one DOI to its Crossref-registered primary URL.

    Returns None on any Crossref miss or error — callers must treat
    None as "unknown, fall back to another routing signal". Never
    raises.
    """
    doi_key = (doi or "").strip().lower()
    if not doi_key:
        return None

    if cache is not None:
        hit = cache.get(doi_key)
        if hit is not None:
            return hit

    try:
        resp = crossref.works(ids=doi_key)
    except Exception as e:
        logger.debug("Crossref lookup failed for %s: %s", doi_key, e)
        return None

    if not isinstance(resp, dict):
        return None
    if resp.get("status") != "ok":
        return None
    msg = resp.get("message")
    if not isinstance(msg, dict):
        return None

    resolution = _extract_resolution(msg)
    if not resolution.url:
        # Metadata exists but no URL — useless for routing, don't
        # cache a negative result (next run might get a better answer
        # if Crossref updates).
        return None

    if cache is not None:
        cache.put(doi_key, resolution)
    return resolution


__all__ = [
    "DoiResolution",
    "DoiResolverCache",
    "resolve_doi",
]
