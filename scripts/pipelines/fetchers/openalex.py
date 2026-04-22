"""OpenAlex — abstract (GROBID TEI XML) and PDF (Content API + OA metadata).

One provider, three capabilities:

  - fetch_abstract: downloads the GROBID-parsed TEI XML from the paid
    Content API and extracts the <abstract> element.  `OPENALEX_API_KEY`
    required.  Last-resort fallback — Crossref / Semantic Scholar are
    preferred for abstracts because GROBID text is occasionally garbled.

  - fetch_pdf: tries the paid Content API first ($0.01/download, only
    when `OPENALEX_API_KEY` is set AND the work's `has_content.pdf` is
    true), then falls back to the free OA metadata tier (follow the
    `open_access.oa_url` if present).

pyalex is used for the metadata lookups.  requests.Session handles the
byte downloads and the GROBID XML fetch.
"""

from __future__ import annotations

import gzip
import logging
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING

from fetchers.base import AbstractFetcher, PdfFetcher

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_TEI_NS = {"tei": "http://www.tei-c.org/ns/1.0"}


def _doi_safe(doi: str) -> str:
    return doi.replace("/", "_").replace(":", "_")


def _cache_pdf_path(cache_dir: str | Path, doi: str) -> Path:
    return Path(cache_dir) / f"{_doi_safe(doi)}.pdf"


class OpenAlexSource(AbstractFetcher, PdfFetcher):
    name = "openalex"

    def __init__(self, http, config=None):
        super().__init__(http, config)
        self._configured = False

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

    # ------------------------------------------------------------------
    # Abstract (GROBID XML)
    # ------------------------------------------------------------------

    def fetch_abstract(self, doi: str, *, title=None, cache_dir=None) -> str | None:
        api_key = self._api_key()
        if not api_key:
            return None
        self._ensure_configured()

        import pyalex
        try:
            work = pyalex.Works()[f"doi:{doi}"]
        except Exception as e:
            logger.debug("openalex lookup %s failed: %s", doi, e)
            return None
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
    # PDF (Content API, then OA metadata fallback)
    # ------------------------------------------------------------------

    def fetch_pdf(
        self, doi: str, *, cache_dir, bypass_prefix_filter: bool = False,
    ) -> tuple[Path, str] | None:
        del bypass_prefix_filter          # not prefix-filtered
        path = _cache_pdf_path(cache_dir, doi)
        if path.exists():
            return path, f"cache://{path}"
        self._ensure_configured()

        result = self._fetch_pdf_content_api(doi, path)
        if result:
            return result
        return self._fetch_pdf_oa_url(doi, path)

    def _fetch_pdf_content_api(
        self, doi: str, path: Path,
    ) -> tuple[Path, str] | None:
        api_key = self._api_key()
        if not api_key:
            return None
        import pyalex
        try:
            work = pyalex.Works()[f"doi:{doi}"]
        except Exception as e:
            logger.debug("openalex content lookup %s failed: %s", doi, e)
            return None
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
        if resp.status_code != 200 or resp.content[:4] != b"%PDF":
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
        return path, dl_url

    def _fetch_pdf_oa_url(
        self, doi: str, path: Path,
    ) -> tuple[Path, str] | None:
        import pyalex
        try:
            work = pyalex.Works()[f"doi:{doi}"]
        except Exception as e:
            logger.debug("openalex OA lookup %s failed: %s", doi, e)
            return None
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
        if resp.status_code != 200 or resp.content[:4] != b"%PDF":
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
        return path, pdf_url
