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
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from habanero import Crossref

logger = logging.getLogger(__name__)


@dataclass
class DoiResolution:
    """Fields we keep from Crossref's work metadata.

    `url / publisher / issn` drive the browser-pipeline routing (v0.4.0).
    `title / author_surnames / issued_year` drive the DOI validation
    logic in `enrich_dois.py` (v0.5.0): comparing Crossref's metadata
    against Zotero's record tells us whether the stored DOI is
    accurate or whether it points to a different paper.
    """

    url: str = ""                  # canonical resource URL, e.g. "https://journals.sagepub.com/doi/…"
    publisher: str = ""            # e.g. "SAGE Publications"
    issn: str = ""                 # first ISSN when Crossref lists several
    title: str = ""                # Crossref's registered title for the DOI
    author_surnames: list[str] = field(default_factory=list)  # family names in order
    issued_year: str = ""          # e.g. "2000"; empty when Crossref has no date


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
        surnames_raw = raw.get("author_surnames") or []
        if not isinstance(surnames_raw, list):
            surnames_raw = []
        return DoiResolution(
            url=str(raw.get("url", "")),
            publisher=str(raw.get("publisher", "")),
            issn=str(raw.get("issn", "")),
            title=str(raw.get("title", "")),
            author_surnames=[str(s) for s in surnames_raw if s],
            issued_year=str(raw.get("issued_year", "")),
        )

    def put(self, doi: str, resolution: DoiResolution) -> None:
        self._data[doi] = asdict(resolution)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, indent=1))
        except Exception as e:
            logger.debug("DoiResolverCache write failed: %s", e)


def _extract_resolution(msg: dict) -> DoiResolution:
    """Pull the routing + validation fields out of a Crossref `message`.

    Routing (v0.4.0):
    - `URL` (or `resource.primary.URL` when more specific) — canonical
      DOI-registered URL. Used to route Pass 2 to the right browser
      handler even when the DOI prefix is misleading (ETAP case).
    - `publisher`, `ISSN[0]` — diagnostic; not routing-critical.

    Validation (v0.5.0):
    - `title[0]` — Crossref's canonical title. Compared against
      Zotero's title to flag DOIs pointing at the wrong paper.
    - `author` — list of `{family, given}` dicts; we keep the family
      names in order for first-author surname comparison.
    - `issued.date-parts[0][0]` — publication year as string (empty
      when Crossref has no date; some older records have no `issued`
      block at all).
    """
    resource = msg.get("resource") or {}
    primary = (resource.get("primary") or {}).get("URL", "")
    url = str(primary or msg.get("URL", "") or "")
    publisher = str(msg.get("publisher", "") or "")
    issns = msg.get("ISSN") or []
    issn = str(issns[0]) if issns else ""

    # Title: Crossref returns a list, typically with one entry.
    # Occasionally missing or empty; default to "".
    titles = msg.get("title") or []
    title = str(titles[0]) if titles else ""

    # Author surnames: skip entries without a `family` key (corporate
    # authors / malformed records). Preserve Crossref order so
    # `surnames[0]` is the first author.
    authors = msg.get("author") or []
    surnames: list[str] = []
    for a in authors:
        if isinstance(a, dict):
            fam = a.get("family") or ""
            if fam:
                surnames.append(str(fam))

    # Issued year: date-parts is a nested list like [[2000, 4, 15]];
    # the inner list may be partial (year only) or missing entirely.
    year = ""
    issued = msg.get("issued") or {}
    if isinstance(issued, dict):
        parts = issued.get("date-parts") or []
        if parts and isinstance(parts, list):
            first = parts[0]
            if first and isinstance(first, list):
                year = str(first[0]) if first[0] is not None else ""

    return DoiResolution(
        url=url,
        publisher=publisher,
        issn=issn,
        title=title,
        author_surnames=surnames,
        issued_year=year,
    )


def resolve_doi(
    doi: str,
    *,
    crossref: Crossref,
    cache: DoiResolverCache | None = None,
) -> DoiResolution | None:
    """Resolve one DOI to its Crossref metadata.

    Returns a `DoiResolution` when Crossref responds with `status=ok`
    and a parsable `message` — individual fields may still be empty
    (a sparse Crossref record). Callers must check the specific
    fields they need:
      - routing wants `resolution.url`;
      - validation wants `resolution.title`.

    Returns None only on hard errors — network failure, non-ok
    status, malformed response. Never raises.
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
    if cache is not None:
        cache.put(doi_key, resolution)
    return resolution


__all__ = [
    "DoiResolution",
    "DoiResolverCache",
    "resolve_doi",
]
