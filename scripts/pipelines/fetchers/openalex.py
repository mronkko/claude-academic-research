"""OpenAlex — abstracts (GROBID TEI XML) and PDFs, split across two sources.

OpenAlex offers two different things behind one API, and they belong at
different points in the PDF cascade because they differ in both cost and
in *which version of the paper* they return:

  - **`OpenAlexContentSource`** (`openalex_content`) — the **paid**
    Content API. $0.01 per download, `OPENALEX_API_KEY` required, and
    only when the work's `has_content.pdf` is true. What it serves is
    the publisher's own file: the **version of record**. It therefore
    sits in the cascade's version-of-record tier, immediately after the
    free publisher-direct sources and *ahead* of the open-access
    aggregators — a correctly paginated published article is worth
    $0.01 more than a free author manuscript when the downstream job is
    quoting text and citing page numbers.

  - **`OpenAlexSource`** (`openalex`) — the **free** OA metadata tier
    (`open_access.oa_url`). Costs nothing, needs no key, and returns
    whatever the OA route happens to host, which is often an author
    accepted manuscript rather than the published article. It sits with
    Unpaywall / Semantic Scholar / CORE in the open-access tier.

Splitting them is what lets the cascade put "free version of record"
first, "paid version of record" second, and "free author version" third.
Before the split, a single fetcher tried the paid endpoint first and the
free one as fallback, from a slot ahead of Unpaywall — so a configured
key meant paying $0.01 for articles the free tiers would also have
served, with no way to express the priority the user actually wanted.

`OpenAlexSource` keeps the abstract capability (`fetch_abstract`), which
also goes through the paid Content API and so honours the same opt-in.

**Shared PDF cache, deliberately.** Both sources cache to the same
`<cache_dir>/<doi>.pdf` path, so a file already downloaded by either one
is served from disk instead of bought again. A consequence worth knowing
when reading logs: on a re-run, a row attributed to `openalex_content`
may be a cache hit on bytes originally fetched free — the recorded
source URL is `cache://…` in that case, not a `content.openalex.org` URL.

pyalex handles the metadata lookups; requests.Session does the byte
downloads and the GROBID XML fetch.
"""

from __future__ import annotations

import gzip
import logging
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING

from fetchers import _pdf_validate
from fetchers.base import AbstractFetcher, PdfFetcher

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_TEI_NS = {"tei": "http://www.tei-c.org/ns/1.0"}

# Env override for the paid-tier opt-in. Values are read leniently
# because this is a user-facing toggle, not a protocol field.
_PAID_ENV = "OPENALEX_USE_PAID_CONTENT_API"
_TRUE_WORDS = ("1", "true", "yes", "on")
_FALSE_WORDS = ("0", "false", "no", "off")


def coerce_paid_opt_in(value, *, default: bool | None = None) -> bool | None:
    """Coerce a paid-tier opt-in value to a tri-state.

    Returns True / False for a recognised value, and `default` for
    anything unset or unrecognised — `None`, `""`, or a typo. The
    tri-state matters: callers need to tell "the user said no" from "the
    user never answered", because those two resolve differently (see
    `_OpenAlexClient._paid_enabled`).

    Lives here, and is public, so the two orchestrator `Config`
    dataclasses that load this setting share one definition of what
    counts as off rather than each hand-rolling a bool parser.
    """
    if isinstance(value, bool):
        return value
    text = str(value if value is not None else "").strip().lower()
    if text in _FALSE_WORDS:
        return False
    if text in _TRUE_WORDS:
        return True
    return default


def _doi_safe(doi: str) -> str:
    return doi.replace("/", "_").replace(":", "_")


def _cache_pdf_path(cache_dir: str | Path, doi: str) -> Path:
    return Path(cache_dir) / f"{_doi_safe(doi)}.pdf"


def _serve_cached_pdf(
    path: Path, doi: str, who: str,
) -> tuple[Path, str] | None:
    """Return a validated cached PDF, or None when there isn't a usable one.

    Validates before serving. A cache entry written by an earlier,
    unvalidated run can be truncated, and returning it unchecked made
    the corruption permanent — every subsequent run short-circuited on
    the bad file and never re-fetched. A defective entry is deleted so
    the caller's own fetch path can replace it.
    """
    if not path.exists():
        return None
    defect = _pdf_validate.file_defect(path)
    if defect is None:
        return path, f"cache://{path}"
    logger.warning("%s: discarding cached PDF for %s — %s", who, doi, defect)
    path.unlink(missing_ok=True)
    return None


class _OpenAlexClient:
    """Credential / pyalex plumbing shared by both OpenAlex sources.

    Deliberately *not* a `Source` subclass: `tests/unit/test_live_coverage.py`
    discovers fetchers by walking `Source.__subclasses__()`, and a shared
    base in that tree would widen the walk for no reason. A plain mixin
    keeps the registry exactly as wide as the two concrete sources.
    """

    #: Class-level default; `_ensure_configured` shadows it per instance.
    #: Avoids an `__init__` override, so `Source.__init__` stays the only
    #: constructor in the chain.
    _configured = False

    def _api_key(self) -> str:
        return (
            getattr(self.config, "openalex_api_key", None)
            or os.environ.get("OPENALEX_API_KEY", "")
        )

    def _mailto(self) -> str:
        return (
            getattr(self.config, "crossref_mailto", None)
            or os.environ.get("CROSSREF_MAILTO", "")
        )

    def _paid_enabled(self) -> bool:
        """Whether the user has opted in to the paid Content API.

        Tri-state on purpose. An *absent* setting reads as enabled,
        because configuring `OPENALEX_API_KEY` at all is itself an
        opt-in signal and an existing setup must not silently lose a
        working tier on upgrade. Only an explicit false — from
        `[openalex] use_paid_content_api = false` or the env override —
        turns it off. The wizard asks outright so new setups record a
        deliberate answer either way.

        This gates *intent*, not capability: with no API key the paid
        path is inert regardless of what this returns.
        """
        from_config = coerce_paid_opt_in(
            getattr(self.config, "openalex_use_paid_content_api", None),
        )
        if from_config is not None:
            return from_config
        return coerce_paid_opt_in(os.environ.get(_PAID_ENV), default=True)

    def _ensure_configured(self) -> None:
        """pyalex exposes a module-level config singleton for email +
        api_key. Set it once per fetcher instance."""
        if self._configured:
            return
        import pyalex
        mailto = self._mailto()
        if mailto:
            pyalex.config.email = mailto
        api_key = self._api_key()
        if api_key:
            pyalex.config.api_key = api_key
        self._configured = True

    def _work(self, doi: str):
        """Look up one work by DOI, or None on any failure."""
        import pyalex
        try:
            work = pyalex.Works()[f"doi:{doi}"]
        except Exception as e:
            logger.debug("openalex lookup %s failed: %s", doi, e)
            return None
        return work or None


class OpenAlexSource(_OpenAlexClient, AbstractFetcher, PdfFetcher):
    """Free OA tier for PDFs; paid GROBID tier for abstracts.

    `fetch_pdf` here is the *free* route only — the paid Content API
    lives in `OpenAlexContentSource` so the cascade can rank it above
    the other open-access aggregators. See the module docstring.
    """

    name = "openalex"

    # ------------------------------------------------------------------
    # Abstract (GROBID XML — paid Content API)
    # ------------------------------------------------------------------

    def fetch_abstract(self, doi: str, *, title=None, cache_dir=None) -> str | None:
        api_key = self._api_key()
        if not api_key or not self._paid_enabled():
            return None
        self._ensure_configured()

        work = self._work(doi)
        if not work:
            return None
        has_grobid = (work.get("has_content") or {}).get("grobid_xml", False)
        if not has_grobid:
            return None

        work_id = (work.get("id") or "").rsplit("/", 1)[-1]
        if not work_id:
            return None

        xml_bytes = self._download_grobid_xml(work_id, cache_dir)
        if not xml_bytes:
            return None

        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as e:
            logger.debug("openalex GROBID XML parse failed for %s: %s", doi, e)
            return None

        abstract_el = root.find(".//tei:profileDesc/tei:abstract", _TEI_NS)
        if abstract_el is None:
            return None
        text = ET.tostring(abstract_el, encoding="unicode", method="text").strip()
        return text if len(text) > 50 else None

    def _download_grobid_xml(self, work_id: str, cache_dir) -> bytes | None:
        api_key = self._api_key()
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)
            cache_path = Path(cache_dir) / f"{work_id}.xml"
            if cache_path.exists():
                try:
                    return cache_path.read_bytes()
                except Exception:
                    cache_path.unlink(missing_ok=True)
        else:
            cache_path = None

        url = f"https://content.openalex.org/works/{work_id}.grobid-xml?api_key={api_key}"
        try:
            resp = self.http.get(url, timeout=30)
        except Exception as e:
            logger.debug("openalex GROBID download %s failed: %s", work_id, e)
            return None
        if resp.status_code != 200:
            return None
        try:
            xml_bytes = gzip.decompress(resp.content)
        except Exception:
            xml_bytes = resp.content
        if cache_path is not None:
            cache_path.write_bytes(xml_bytes)
        return xml_bytes

    # ------------------------------------------------------------------
    # PDF (free OA metadata tier)
    # ------------------------------------------------------------------

    def fetch_pdf(
        self, doi: str, *, cache_dir, bypass_prefix_filter: bool = False,
    ) -> tuple[Path, str] | None:
        del bypass_prefix_filter          # not prefix-filtered
        path = _cache_pdf_path(cache_dir, doi)
        cached = _serve_cached_pdf(path, doi, self.name)
        if cached is not None:
            return cached
        self._ensure_configured()
        return self._fetch_pdf_oa_url(doi, path)

    def _fetch_pdf_oa_url(
        self, doi: str, path: Path,
    ) -> tuple[Path, str] | None:
        work = self._work(doi)
        if not work:
            return None
        pdf_url = (work.get("open_access") or {}).get("oa_url")
        if not pdf_url:
            return None
        ua = f"mailto:{self._mailto()}" if self._mailto() else "Mozilla/5.0"
        try:
            resp = self.http.get(pdf_url, headers={"User-Agent": ua}, timeout=60)
        except Exception as e:
            logger.debug("openalex OA PDF %s failed: %s", pdf_url, e)
            return None
        defect = _pdf_validate.response_defect(resp)
        if defect is not None:
            logger.warning("openalex OA PDF %s rejected — %s", pdf_url, defect)
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
        return path, pdf_url


class OpenAlexContentSource(_OpenAlexClient, PdfFetcher):
    """Paid OpenAlex Content API — $0.01 per PDF, version of record.

    PDF-only: the abstract capability stays on `OpenAlexSource`, so this
    class is exactly one thing — the single tier in the default cascade
    that costs money per item.
    """

    name = "openalex_content"

    def fetch_pdf(
        self, doi: str, *, cache_dir, bypass_prefix_filter: bool = False,
    ) -> tuple[Path, str] | None:
        del bypass_prefix_filter          # not prefix-filtered
        path = _cache_pdf_path(cache_dir, doi)
        cached = _serve_cached_pdf(path, doi, self.name)
        if cached is not None:
            return cached

        api_key = self._api_key()
        if not api_key or not self._paid_enabled():
            return None
        self._ensure_configured()

        work = self._work(doi)
        if not work:
            return None
        if not (work.get("has_content") or {}).get("pdf", False):
            return None
        work_id = (work.get("id") or "").rsplit("/", 1)[-1]
        if not work_id:
            return None

        dl_url = f"https://content.openalex.org/works/{work_id}.pdf?api_key={api_key}"
        try:
            resp = self.http.get(dl_url, timeout=120)
        except Exception as e:
            logger.debug("openalex content PDF %s failed: %s", doi, e)
            return None
        defect = _pdf_validate.response_defect(resp)
        if defect is not None:
            # Return None rather than raising so the cascade moves on to
            # the next source. OpenAlex has served permanently-truncated
            # copies (byte-identical across retries), so the next source
            # is the only route that helps.
            logger.warning("openalex content PDF %s rejected — %s", doi, defect)
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
        return path, dl_url
