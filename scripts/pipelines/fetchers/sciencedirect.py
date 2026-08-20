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

from fetchers import _pdf_validate
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

# Zotero tag applied to items whose attached PDF is XML-recovered
# text rather than the publisher's native PDF (see _TDM_RECOVERED_SUFFIX
# above). Follows the `<noun>:<status>` warning-tag convention used by
# `predatory:flag` / `retracted:flag` — surfaced by audit_zotero_library.py
# so users can review extraction quality before/during full-text coding.
TDM_RECOVERED_TAG = "pdf:tdm-recovered"


def _doi_safe(doi: str) -> str:
    return doi.replace("/", "_").replace(":", "_")


def _cache_pdf_path(cache_dir: str | Path, doi: str, *, recovered: bool = False) -> Path:
    suffix = _TDM_RECOVERED_SUFFIX if recovered else ""
    return Path(cache_dir) / f"{_doi_safe(doi)}{suffix}.pdf"


def is_tdm_recovered_path(path: str | Path) -> bool:
    """True when `path` is a cache file produced by the XML-fallback
    recovery path (filename ends with `_TDM_RECOVERED_SUFFIX`)."""
    return Path(path).stem.endswith(_TDM_RECOVERED_SUFFIX)


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


#: Elsevier elements that annotate the body rather than continue it.
#: Both are placed **at the point their marker appears**, so a plain
#: document-order text walk splices the whole annotation between a
#: clause and its continuation. `<ce:table-footnote>` has the identical
#: shape and the identical problem, one level down inside a table.
#:
#: Tables and figures are deliberately *not* here. They are content
#: rather than annotation, and Elsevier anchors them at block level via
#: `<ce:float-anchor>` rather than mid-sentence, so they do not break
#: the sentence they sit in. `<ce:bib-reference>` is not here either:
#: it lives in the bibliography, outside `<body>`. Revisit if a real
#: article shows otherwise.
_ANNOTATION_TAGS = frozenset({"footnote", "table-footnote"})

#: Heading that separates relocated annotations from the body prose.
_FOOTNOTES_HEADING = "Footnotes"


def _element_text(el, skip: frozenset = frozenset()) -> list[str]:
    """Text under `el` in document order, skipping `skip` subtrees.

    A skipped child's **tail is still collected**. That tail is the
    remainder of the sentence the annotation was anchored in — dropping
    it along with the annotation would weld the words on either side of
    the marker together, trading one corruption for another.
    """
    parts: list[str] = []
    if el.text and el.text.strip():
        parts.append(el.text.strip())
    for child in el:
        if child.tag not in skip:
            parts.extend(_element_text(child, skip))
        if child.tail and child.tail.strip():
            parts.append(child.tail.strip())
    return parts


def _collect_annotations(el) -> list[str]:
    """Every annotation subtree under `el`, in document order.

    Does not descend into an annotation it has already collected, so a
    footnote nested inside another is rendered once, as part of its
    parent, rather than twice.
    """
    out: list[str] = []
    for child in el:
        if child.tag in _ANNOTATION_TAGS:
            text = " ".join(_element_text(child))
            if text:
                out.append(text)
        else:
            out.extend(_collect_annotations(child))
    return out


def _extract_xml_parts(xml_bytes: bytes) -> tuple[str, list[str]]:
    """Split an Elsevier full-text XML response into (prose, annotations).

    Returns `("", [])` if the response has no `<body>` or does not
    parse — callers must treat that as "XML fallback also failed".
    """
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml_bytes)
    except Exception as e:  # noqa: BLE001
        logger.debug("Elsevier XML parse failed: %s", e)
        return "", []
    # Strip namespaces from every tag for tolerant element lookups.
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    body = None
    for el in root.iter("body"):
        body = el
        break
    if body is None:
        return "", []
    return (
        " ".join(_element_text(body, _ANNOTATION_TAGS)),
        _collect_annotations(body),
    )


def _extract_xml_body(xml_bytes: bytes) -> str:
    """Readable article text from an Elsevier full-text XML response.

    The body prose comes first, with footnotes lifted out of it and
    re-attached as an endnote block under a `Footnotes` heading.

    Relocating rather than dropping them is deliberate. In the law
    reviews this fallback is most often used on, footnotes carry a
    large share of the argument — one sampled article was 35 %
    footnote by character count — so discarding them would lose real
    content. Leaving them where the XML puts them is what this
    replaced: each one landed between a clause and its continuation,
    and every annotated sentence came out broken in two.

    In-text markers (`<ce:cross-ref>` superscripts) stay where they
    are, so a reader can still match a marker to its note.
    """
    return _assemble_body(*_extract_xml_parts(xml_bytes))


def _assemble_body(prose: str, notes: list[str]) -> str:
    """Join body prose and relocated annotations into one document."""
    if not prose and not notes:
        return ""
    if not notes:
        return prose
    return "\n\n".join([prose, _FOOTNOTES_HEADING, *notes])


def _plugin_version() -> str:
    """This plugin's version string, or "unknown".

    Read from `.claude-plugin/plugin.json` at the repo root rather than
    hardcoded, so the stamp on a recovered PDF cannot drift from the
    release that produced it.
    """
    try:
        import json
        manifest = (
            Path(__file__).resolve().parents[3]
            / ".claude-plugin" / "plugin.json"
        )
        version = json.loads(manifest.read_text(encoding="utf-8")).get("version")
        return str(version) if version else "unknown"
    except Exception:  # noqa: BLE001 — a stamp must never fail a download
        return "unknown"


def _recovery_note(n_annotations: int) -> str:
    """The provenance line stamped onto a recovered PDF.

    Names the version because the transformation changed in 0.15.0 and
    files produced before it cannot otherwise be told apart: every
    recovery ever made carries the same `-tdm-recovered` suffix, so a
    corpus mixes silently-corrupted text with corrected text and
    nothing in either file says which it is. Re-fetching is the only
    remedy for the old ones, and this is what makes them identifiable.
    """
    made_by = (
        f"Text recovered from Elsevier full-text XML by "
        f"claude-academic-research {_plugin_version()}."
    )
    if not n_annotations:
        return f"{made_by} No footnotes were present in the body."
    return (
        f"{made_by} {n_annotations} footnote(s) were moved out of the "
        f"body text into the Footnotes section at the end; in-text "
        f"markers are unchanged. This is not the publisher's PDF."
    )


def _render_text_pdf(
    text: str, out_path: Path, *, title: str = "", note: str = "",
) -> None:
    """Write `text` to `out_path` as a plain text-only PDF via reportlab.

    Layout is intentionally minimal — wrapped paragraphs, no styling.
    The point is that downstream `pdftotext` can recover the body for
    coding. Raises ImportError if reportlab is not installed; callers
    should catch and report a sensible error in that case.

    `note` is a provenance line rendered above the body. A recovered
    PDF is otherwise indistinguishable from a native one to anything
    that does not look closely — it is well-formed, passes size and
    text-length checks, and reads as a normal article — so the file
    should say how it was made and what was moved.
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
    if note:
        flowables.append(Paragraph(_escape_xml(note), styles["Italic"]))
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
    doi_prefixes = _ELSEVIER_PREFIXES
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
            # Validate before serving: an entry written by an earlier,
            # unvalidated run may be truncated, and returning it unchecked
            # made the corruption permanent — every later run
            # short-circuited on the bad file instead of re-fetching.
            _defect = _pdf_validate.file_defect(path)
            if _defect is None:
                return path, f"cache://{path}"
            logger.warning("discarding cached PDF for %s — %s", doi, _defect)
            path.unlink(missing_ok=True)

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
        _defect = _pdf_validate.response_defect(resp)
        if _defect is not None:
            # None (not an exception) so the cascade falls through to the
            # next source — a truncated copy at one provider is often
            # served intact by another.
            logger.warning("%s: rejected PDF for %s — %s", self.name, doi, _defect)
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

        **Opt-in, via `[elsevier] render_xml_to_pdf`.** What this produces
        is not a publisher PDF but a file this tool generates: plain text,
        no figures, no layout, tables flattened. Attaching that to
        someone's Zotero library without being asked is a surprising
        thing to do — a user opening it later has no reason to expect a
        synthesized document, and it is easily mistaken for the real
        article. Default off; when off the XML endpoint is not called at
        all, so no Elsevier quota is spent discovering text we would then
        refuse to write.
        """
        if not getattr(self.config, "elsevier_render_xml_to_pdf", False):
            logger.info(
                "elsevier %s returned a first-page preview; full text is "
                "available via the XML endpoint but PDF synthesis is off. "
                "Set [elsevier] render_xml_to_pdf = true (or re-run the "
                "setup wizard) to recover it as a generated text-only PDF.",
                doi,
            )
            return None
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
        prose, annotations = _extract_xml_parts(xml_resp.content)
        body = _assemble_body(prose, annotations)
        if not body or len(body) < 500:
            # An entitled XML response with a truly empty body is
            # vanishingly rare — treat as not-recovered rather than
            # caching a near-empty PDF.
            return None
        out_path = _cache_pdf_path(cache_dir, doi, recovered=True)
        try:
            _render_text_pdf(
                body, out_path, note=_recovery_note(len(annotations)),
            )
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
