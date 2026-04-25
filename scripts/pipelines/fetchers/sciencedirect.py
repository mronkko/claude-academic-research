"""ScienceDirect / Elsevier — abstract via pybliometrics, PDF via Elsevier API.

ScienceDirect is Elsevier's full-text platform. The same API key is
used for abstract and PDF endpoints at different URLs. This file hosts
both capabilities because they're the same publisher, though the
abstract path uses pybliometrics while the PDF path goes through the
shared requests.Session.

P11 — preview-PDF detection + XML fallback
==========================================

Elsevier's TDM API returns a 1-page preview PDF (still 200 OK, still
`%PDF` magic bytes) when the requestor's institutional entitlement
covers some articles but not this specific one. The signal is the
`x-els-status` response header: `WARNING - Response limited to first
page because requestor not entitled to resource`. Without inspecting
that header, the fetcher silently caches a 1-page preview as if it
were the full text, and downstream coding runs against the preview.

The XML endpoint at the same URL has broader entitlement at most
institutions: papers that returned WARNING on `Accept: application/pdf`
return `x-els-status: OK` with full body text on `Accept: text/xml`.
The fix: check the header, fall back to XML on WARNING, render the
extracted body to a text-only PDF via reportlab, and annotate the
cache filename so audits can tell a real PDF from a TDM-recovered one.
"""

from __future__ import annotations

import logging
import os
import re
import urllib.parse
from pathlib import Path

from fetchers.base import AbstractFetcher, PdfFetcher

logger = logging.getLogger(__name__)

_ELSEVIER_BASE = "https://api.elsevier.com/content/article/doi"
_ELSEVIER_PREFIXES = (
    "10.1016/", "10.1006/", "10.1053/", "10.1054/",
    "10.1067/", "10.1074/", "10.1078/", "10.1383/",
)

# Suffix appended to cache filenames when the PDF was reconstructed
# from the XML endpoint after the PDF endpoint returned a preview.
# Audits group on this suffix to surface "TDM-recovered" items
# distinctly from natively-fetched PDFs.
_TDM_RECOVERED_SUFFIX = "-tdm-recovered"


def _doi_safe(doi: str) -> str:
    return doi.replace("/", "_").replace(":", "_")


def _cache_pdf_path(cache_dir: str | Path, doi: str, *, recovered: bool = False) -> Path:
    suffix = _TDM_RECOVERED_SUFFIX if recovered else ""
    return Path(cache_dir) / f"{_doi_safe(doi)}{suffix}.pdf"


def _is_preview_warning(els_status: str) -> bool:
    """True when Elsevier's `x-els-status` header signals a partial /
    preview response. Matches both the canonical wording ("Response
    limited to first page because requestor not entitled to resource")
    and the shorter "not entitled" forms Elsevier sometimes returns.
    """
    if not els_status:
        return False
    s = els_status.strip()
    return s.startswith("WARNING") or "not entitled" in s.lower()


def _extract_xml_body(xml_bytes: bytes) -> str:
    """Pull the article body from an Elsevier full-text XML response.

    Strips namespaces and concatenates all text nodes under the
    `<body>` element. Returns an empty string if no body is found —
    callers must treat that as "XML fallback also failed".
    """
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml_bytes)
    except Exception as e:  # noqa: BLE001
        logger.debug("Elsevier XML parse failed: %s", e)
        return ""
    # Strip namespaces from every tag for tolerant element lookups.
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    body = None
    for el in root.iter("body"):
        body = el
        break
    if body is None:
        return ""
    parts: list[str] = []
    for el in body.iter():
        if el.text and el.text.strip():
            parts.append(el.text.strip())
        if el.tail and el.tail.strip():
            parts.append(el.tail.strip())
    return " ".join(parts)


def _render_text_pdf(text: str, out_path: Path, *, title: str = "") -> None:
    """Write `text` to `out_path` as a plain text-only PDF via reportlab.

    Layout is intentionally minimal — wrapped paragraphs, no styling.
    The point is that downstream `pdftotext` can recover the body for
    coding. Raises ImportError if reportlab is not installed; callers
    should catch and report a sensible error in that case.
    """
    # reportlab is declared in enrich_pdfs.py's PEP 723 deps — pulled in
    # automatically when fetchers run via `uv run`. Static analyzers
    # without site-packages on hand can't resolve it; suppress the
    # lookup error rather than vendoring stubs.
    from reportlab.lib.pagesizes import letter  # type: ignore[import-not-found]
    from reportlab.lib.styles import getSampleStyleSheet  # type: ignore[import-not-found]
    from reportlab.platypus import (  # type: ignore[import-not-found]
        Paragraph,
        SimpleDocTemplate,
        Spacer,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(out_path), pagesize=letter)
    styles = getSampleStyleSheet()
    flowables: list = []
    if title:
        flowables.append(Paragraph(_escape_xml(title), styles["Title"]))
        flowables.append(Spacer(1, 12))
    # reportlab Paragraph wraps and respects basic markup. Split on
    # blank lines / sentence-like breaks; very long single paragraphs
    # get broken on punctuation to keep the layout sane.
    paragraphs = re.split(r"\n\s*\n+|(?<=\.\s)\s{2,}", text)
    for p in paragraphs:
        p = p.strip()
        if p:
            flowables.append(Paragraph(_escape_xml(p), styles["BodyText"]))
            flowables.append(Spacer(1, 6))
    if not flowables:
        # Avoid reportlab's "no story" error on empty input.
        flowables.append(Paragraph("(empty body)", styles["BodyText"]))
    doc.build(flowables)


def _escape_xml(s: str) -> str:
    """reportlab Paragraph treats `<` / `&` as markup — escape them."""
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
    )


class ScienceDirectSource(AbstractFetcher, PdfFetcher):
    """Abstract via pybliometrics.sciencedirect.ArticleRetrieval;
    PDF via https://api.elsevier.com/content/article/doi/{doi}."""

    name = "sciencedirect"
    direct_access_domains = ("sciencedirect.com", "elsevier.com")

    def _api_key(self) -> str:
        return (
            getattr(self.config, "elsevier_api_key", None)
            or os.environ.get("ELSEVIER_API_KEY", "")
        )

    def fetch_abstract(self, doi: str, *, title=None, cache_dir=None) -> str | None:
        try:
            from pybliometrics.utils.startup import init
            init()
            from pybliometrics.sciencedirect import ArticleRetrieval
        except Exception as e:
            logger.debug("pybliometrics import/init failed: %s", e)
            return None

        try:
            a = ArticleRetrieval(doi, view="FULL")
        except Exception as e:
            logger.debug("ScienceDirect ArticleRetrieval(%s) failed: %s", doi, e)
            return None

        if a.abstract:
            text = str(a.abstract).strip()
            if text:
                return text

        raw = str(a.originalText) if a.originalText else ""
        for pattern in (
            r"<abstract[^>]*>(.*?)</abstract>",
            r"<ce:abstract-sec[^>]*>(.*?)</ce:abstract-sec>",
        ):
            match = re.search(pattern, raw, re.DOTALL | re.IGNORECASE)
            if match:
                text = re.sub(r"<[^>]+>", " ", match.group(1))
                text = re.sub(r"\s+", " ", text).strip()
                if len(text) > 100:
                    return text
        return None

    def fetch_pdf(
        self, doi: str, *, cache_dir, bypass_prefix_filter: bool = False,
    ) -> tuple[Path, str] | None:
        if (not bypass_prefix_filter
                and not any(doi.startswith(p) for p in _ELSEVIER_PREFIXES)):
            return None
        key = self._api_key()
        if not key:
            return None
        # Prefer a recovered cache if one exists; that's the higher-
        # quality artefact for this DOI from a previous run.
        recovered_path = _cache_pdf_path(cache_dir, doi, recovered=True)
        if recovered_path.exists():
            return recovered_path, f"cache://{recovered_path}"
        path = _cache_pdf_path(cache_dir, doi)
        if path.exists():
            return path, f"cache://{path}"

        url = f"{_ELSEVIER_BASE}/{urllib.parse.quote(doi, safe='')}"
        try:
            resp = self.http.get(
                url,
                headers={"X-ELS-APIKey": key, "Accept": "application/pdf"},
                timeout=30,
            )
        except Exception as e:
            logger.debug("elsevier PDF %s failed: %s", doi, e)
            return None
        if resp.status_code != 200 or resp.content[:4] != b"%PDF":
            return None

        # P11: per-article entitlement check. The PDF endpoint returns
        # 200 + valid PDF bytes even for preview-only responses; the
        # `x-els-status` header is the only signal that distinguishes
        # them. On WARNING, fall back to the XML endpoint (broader
        # entitlement at most institutions).
        els_status = resp.headers.get("x-els-status", "") or resp.headers.get("X-ELS-Status", "")
        if _is_preview_warning(els_status):
            logger.info(
                "elsevier PDF %s returned preview (x-els-status=%r); "
                "trying XML fallback", doi, els_status,
            )
            recovered = self._fetch_xml_fallback(doi, key, url, cache_dir)
            if recovered is not None:
                return recovered, f"{url} (xml-fallback)"
            # Preview was the only thing on offer — refuse to cache.
            # The cascade caller (enrich_pdfs._try_cascade) logs the
            # failure; downstream P11 audit surfaces these to the user.
            return None

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
        return path, url

    def _fetch_xml_fallback(
        self, doi: str, key: str, url: str, cache_dir,
    ) -> Path | None:
        """Pull the article via the XML endpoint and render to a text-only PDF.

        Only called when the PDF endpoint returns `x-els-status: WARNING`.
        Returns the cache path of the recovered text-only PDF, or None
        if the XML endpoint is also unentitled / empty.
        """
        # _fetch_xml_fallback is only reached after a successful PDF
        # call, so self.http is guaranteed non-None at this point. The
        # assert documents the precondition for static analyzers that
        # don't flow-narrow through the caller.
        assert self.http is not None
        try:
            xml_resp = self.http.get(
                url,
                headers={"X-ELS-APIKey": key, "Accept": "text/xml"},
                timeout=30,
            )
        except Exception as e:
            logger.debug("elsevier XML fallback %s failed: %s", doi, e)
            return None
        if xml_resp.status_code != 200:
            return None
        xml_status = xml_resp.headers.get("x-els-status", "") or xml_resp.headers.get("X-ELS-Status", "")
        if _is_preview_warning(xml_status):
            return None
        body = _extract_xml_body(xml_resp.content)
        if not body or len(body) < 500:
            # An entitled XML response with a truly empty body is
            # vanishingly rare — treat as not-recovered rather than
            # caching a near-empty PDF.
            return None
        out_path = _cache_pdf_path(cache_dir, doi, recovered=True)
        try:
            _render_text_pdf(body, out_path)
        except ImportError:
            logger.warning(
                "reportlab not installed; cannot render XML body to PDF for %s. "
                "Install via `uv run` (PEP 723) or `pip install reportlab`.",
                doi,
            )
            return None
        except Exception as e:  # noqa: BLE001
            logger.debug("reportlab render failed for %s: %s", doi, e)
            return None
        return out_path
