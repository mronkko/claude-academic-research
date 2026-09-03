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

import base64
import logging
import os
import re
import urllib.parse
import zlib
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


#: Elements that start a new block rather than continuing the current
#: one. Kept out of a paragraph's inline text and emitted in their own
#: right, so a list does not dissolve into the sentence before it.
_NESTED_BLOCK_TAGS = frozenset({"list", "displayed-quote", "table"})

#: Deepest heading level rendered. Elsevier nests three deep in
#: practice; anything further is rendered at the third level rather
#: than inventing styles nobody has a stylesheet for.
_MAX_HEADING_LEVEL = 3


def _inline_text(el, also_skip: frozenset = frozenset()) -> str:
    """One block's worth of running text, annotations and sub-blocks out."""
    return " ".join(
        _element_text(el, _ANNOTATION_TAGS | _NESTED_BLOCK_TAGS | also_skip)
    )


def _sub_blocks(el, depth: int) -> list[tuple[str, str]]:
    """Block-level descendants of `el`, in document order.

    Elsevier nests lists and block quotes *inside* the paragraph that
    introduces them, so they have to be lifted out after that
    paragraph's own text rather than found among its siblings.
    """
    out: list[tuple[str, str]] = []
    for child in el:
        if child.tag in _ANNOTATION_TAGS:
            continue
        if child.tag in _NESTED_BLOCK_TAGS:
            out.extend(_element_blocks(child, depth))
        else:
            out.extend(_sub_blocks(child, depth))
    return out


def _prose_blocks(
    el, kind: str, depth: int, also_skip: frozenset = frozenset(),
) -> list[tuple[str, str]]:
    text = _inline_text(el, also_skip)
    blocks = [(kind, text)] if text else []
    return blocks + _sub_blocks(el, depth)


def _table_blocks(el) -> list[tuple[str, str]]:
    """A table as caption plus one block per row.

    Table cells hold inline markup rather than `<ce:para>`, so the
    generic block walk finds nothing to emit and the whole table
    vanishes — which is what happened when structural extraction first
    replaced the flat text join here. Rows are rendered pipe-separated:
    crude, but it keeps the cell boundaries that a wall of concatenated
    cell text destroys.
    """
    blocks: list[tuple[str, str]] = []
    caption = el.find("caption")
    if caption is not None:
        text = _inline_text(caption)
        if text:
            blocks.append(("p", text))
    for row in el.iter("row"):
        cells = [c for c in (_inline_text(e) for e in row.iter("entry")) if c]
        if cells:
            blocks.append(("row", "  |  ".join(cells)))
    return blocks


def _element_blocks(el, depth: int = 1) -> list[tuple[str, str]]:
    """Turn one element into `(kind, text)` blocks.

    `kind` is `h1`–`h3`, `p`, `li` or `quote` — the vocabulary
    `_render_text_pdf` maps onto paragraph styles. Extracting structure
    rather than a single string is what lets the recovered PDF keep its
    headings: `<ce:section-title>` used to be concatenated straight into
    the first sentence of its own section, so every article opened
    "Introduction Fairness and discrimination in algorithmic systems
    are…" and ran on from there as one undifferentiated block.
    """
    tag = el.tag
    if tag in _ANNOTATION_TAGS:
        return []
    if tag == "section":
        blocks: list[tuple[str, str]] = []
        title = el.find("section-title")
        if title is not None:
            heading = _inline_text(title)
            label = el.find("label")
            if label is not None and (label.text or "").strip():
                # Appendices carry their designation in a sibling label.
                heading = f"{label.text.strip()} {heading}".strip()
            if heading:
                blocks.append((f"h{min(depth, _MAX_HEADING_LEVEL)}", heading))
        for child in el:
            if child.tag in ("section-title", "label"):
                continue
            blocks.extend(_element_blocks(child, depth + 1))
        return blocks
    if tag in ("para", "simple-para"):
        return _prose_blocks(el, "p", depth)
    if tag == "displayed-quote":
        return _prose_blocks(el, "quote", depth)
    if tag == "list-item":
        # `<ce:label>` here is the bullet glyph itself, which the
        # renderer supplies — keeping it would print two bullets.
        # Footnote labels take a different path and are kept.
        return _prose_blocks(el, "li", depth, frozenset({"label"}))
    if tag == "table":
        return _table_blocks(el)
    # Containers with no block semantics of their own: descend.
    out: list[tuple[str, str]] = []
    for child in el:
        out.extend(_element_blocks(child, depth))
    return out


def _parse_namespace_free(xml_bytes: bytes):
    """Parse the response and strip namespaces from every tag.

    Elsevier mixes four namespaces across one document; stripping them
    once up front is what lets every lookup below be a plain tag name.
    Returns None if the payload does not parse.
    """
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml_bytes)
    except Exception as e:  # noqa: BLE001
        logger.debug("Elsevier XML parse failed: %s", e)
        return None
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _first_text(root, tag: str) -> str:
    for el in root.iter(tag):
        text = " ".join("".join(el.itertext()).split())
        if text:
            return text
    return ""


def _all_text(root, tag: str) -> list[str]:
    out: list[str] = []
    for el in root.iter(tag):
        text = " ".join("".join(el.itertext()).split())
        if text and text not in out:
            out.append(text)
    return out


def _extract_metadata(root) -> dict:
    """The front-matter facts a journal PDF prints on its first page.

    All of this rides along in the same response the body comes from,
    and none of it used to reach the recovered file — which is why a
    recovered PDF opened straight into the article's first sentence,
    with no title, no authors and no DOI anywhere in it. That is also
    why title-similarity checks on these files had so little to work
    with.
    """
    coredata = next(root.iter("coredata"), None)
    if coredata is None:
        return {}
    affiliations: list[str] = []
    for group in root.iter("author-group"):
        for aff in group.iter("affiliation"):
            textfn = aff.find("textfn")
            if textfn is None:
                continue
            text = " ".join("".join(textfn.itertext()).split())
            label = aff.find("label")
            if label is not None and (label.text or "").strip():
                text = f"({label.text.strip()}) {text}"
            if text and text not in affiliations:
                affiliations.append(text)
    return {
        "title": _first_text(coredata, "title"),
        "authors": _all_text(coredata, "creator"),
        "affiliations": affiliations,
        "journal": _first_text(coredata, "publicationName"),
        "volume": _first_text(coredata, "volume"),
        "issue": _first_text(coredata, "issueIdentifier"),
        "pages": (_first_text(coredata, "pageRange")
                  or _first_text(coredata, "articleNumber")),
        "date": (_first_text(coredata, "coverDisplayDate")
                 or _first_text(coredata, "coverDate")),
        "doi": _first_text(coredata, "doi"),
        "issn": _first_text(coredata, "issn"),
        "abstract": _first_text(coredata, "description"),
        "keywords": _all_text(coredata, "subject"),
    }


def _extract_article(
    xml_bytes: bytes,
) -> tuple[dict, list[tuple[str, str]], list[str]]:
    """One parse: (metadata, body blocks, relocated annotations)."""
    root = _parse_namespace_free(xml_bytes)
    if root is None:
        return {}, [], []
    body = next(root.iter("body"), None)
    if body is None:
        return _extract_metadata(root), [], []
    return (
        _extract_metadata(root),
        _element_blocks(body),
        _collect_annotations(body),
    )


def _extract_xml_blocks(xml_bytes: bytes) -> tuple[list[tuple[str, str]], list[str]]:
    """Split an Elsevier full-text XML response into (blocks, annotations).

    Returns `([], [])` if the response has no `<body>` or does not
    parse — callers must treat that as "XML fallback also failed".
    """
    _meta, blocks, notes = _extract_article(xml_bytes)
    return blocks, notes


def _extract_xml_parts(xml_bytes: bytes) -> tuple[str, list[str]]:
    """`_extract_xml_blocks` flattened to plain prose, for text checks."""
    blocks, notes = _extract_xml_blocks(xml_bytes)
    return "\n\n".join(text for _kind, text in blocks), notes


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
    return _assemble_body(*_extract_xml_blocks(xml_bytes))


def _assemble_body(blocks: list[tuple[str, str]], notes: list[str]) -> str:
    """Flatten blocks and relocated annotations into one plain string.

    The string form is what the length check and any text-only consumer
    sees; `_render_text_pdf` takes the blocks themselves so it can keep
    the headings.
    """
    return "\n\n".join(
        [text for _kind, text in blocks]
        + ([_FOOTNOTES_HEADING, *notes] if notes else [])
    )


def _document_flow(
    blocks: list[tuple[str, str]], notes: list[str],
) -> list[tuple[str, str]]:
    """Body blocks plus the relocated footnotes, as one block list."""
    if not notes:
        return list(blocks)
    return [
        *blocks,
        ("h1", _FOOTNOTES_HEADING),
        *(("note", note) for note in notes),
    ]


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


#: The release whose XML→PDF transformation is the current one. A cached
#: recovery stamped below this was produced by the older transformation —
#: no front matter (no title, authors, abstract or DOI anywhere in the
#: document) and footnotes spliced into the middle of sentences — and has
#: to be re-fetched rather than served.
#:
#: Raise this only when the transformation itself changes in a way that
#: makes older output wrong. It is not the plugin version: bumping it for
#: an unrelated release would invalidate every cache on every machine and
#: spend a publisher's API quota re-fetching files that were already fine.
_CURRENT_RECOVERY_VERSION: tuple[int, ...] = (0, 15, 0)

#: Matches the version in `_recovery_note`'s "…by claude-academic-research
#: 0.15.1." — in the PDF's Info dictionary, and in the page text.
_STAMP_RE = re.compile(rb"claude-academic-research\s+(\d+(?:\.\d+)+)")

#: A PDF string literal. reportlab escapes `(` and `)` inside text, so
#: these never nest.
_PDF_STRING_RE = re.compile(rb"\((?:[^()\\]|\\.)*\)", re.DOTALL)

#: Cap on decoded content-stream bytes. A recovered PDF is text; anything
#: claiming to be much larger is not worth decompressing to read a stamp.
_MAX_DECODED_BYTES = 8 * 1024 * 1024


def _parse_version(text: str) -> tuple[int, ...] | None:
    """`"0.15.1"` -> `(0, 15, 1)`; None for anything unparseable.

    `_plugin_version` returns the string `"unknown"` when the manifest
    cannot be read, and that must not compare as a version.
    """
    parts = text.strip().split(".")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def _decoded_page_text(data: bytes) -> bytes:
    """The text drawn on the page, recovered from the content streams.

    reportlab compresses page content (ASCII85 then Flate), so the stamp
    is not in the file as plain bytes — a naive search finds nothing. Both
    codecs are stdlib, which matters: `_pdf_validate`'s docstring makes the
    case that a validator running inside every fetcher must not depend on
    an optional binary or a PDF library, or it silently no-ops on exactly
    the machines that need it.

    Text is returned as the string literals joined by spaces rather than
    as the raw stream, because a paragraph is drawn one line per operator
    and the stamp can wrap between them.

    Any failure to decode yields b"" and so reads as "unstamped", which
    re-fetches. That is the safe direction: a wasted fetch costs quota, a
    wrongly-trusted cache entry costs correctness.
    """
    chunks: list[bytes] = []
    total = 0
    for match in re.finditer(rb"stream[\r\n]+", data):
        start = match.end()
        end = data.find(b"endstream", start)
        if end < 0:
            continue
        raw = data[start:end].strip()
        for decode in (
            lambda b: zlib.decompress(base64.a85decode(b, adobe=True)),
            zlib.decompress,
        ):
            try:
                decoded = decode(raw)
            except Exception:  # noqa: BLE001 — wrong codec for this stream
                continue
            chunks.append(decoded)
            total += len(decoded)
            break
        if total > _MAX_DECODED_BYTES:
            break
    blob = b"\n".join(chunks)
    return b" ".join(m.group()[1:-1] for m in _PDF_STRING_RE.finditer(blob))


def stamped_recovery_version(path: str | Path) -> tuple[int, ...] | None:
    """The plugin version that produced a recovered PDF, or None.

    Looks in the Info dictionary first — reportlab writes it uncompressed,
    so it is a plain byte search, and it keeps working if reportlab ever
    changes how it encodes page content. Falls back to the page text,
    which is where the only stamp lives in files written by 0.15.0 and
    0.15.1: those predate the Info-dictionary entry, and re-fetching a
    corpus of them needlessly would spend real publisher quota.
    """
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    match = _STAMP_RE.search(data) or _STAMP_RE.search(_decoded_page_text(data))
    if not match:
        return None
    return _parse_version(match.group(1).decode("ascii", "replace"))


def _stale_recovery_reason(path: str | Path) -> str | None:
    """Why a cached recovery must not be served, or None if it may be.

    Deliberately says nothing about file size. Short recovered articles
    are real — correspondence items and conference abstracts render to
    3-4KB — and a byte floor rejects them for being what they are.
    """
    version = stamped_recovery_version(path)
    if version is None:
        return (
            "no version stamp, so it predates "
            f"{_version_str(_CURRENT_RECOVERY_VERSION)}, when the XML "
            "transformation changed"
        )
    if version < _CURRENT_RECOVERY_VERSION:
        return (
            f"produced by {_version_str(version)}, before the XML "
            f"transformation changed in "
            f"{_version_str(_CURRENT_RECOVERY_VERSION)}"
        )
    return None


def _version_str(version: tuple[int, ...]) -> str:
    return ".".join(str(p) for p in version)


#: Block kind -> (stylesheet name, space after, left indent).
#: `note` is deliberately the same size as body text: the footnotes are
#: content in the articles this fallback serves, not marginalia.
_BLOCK_STYLES: dict[str, tuple[str, int, int]] = {
    "h1": ("Heading1", 10, 0),
    "h2": ("Heading2", 8, 0),
    "h3": ("Heading3", 6, 0),
    "p": ("BodyText", 6, 0),
    "li": ("BodyText", 4, 18),
    "quote": ("BodyText", 8, 24),
    "row": ("BodyText", 2, 18),
    "note": ("BodyText", 4, 0),
}


def _cover_flowables(meta: dict, note: str, styles, Paragraph, Spacer):
    """The front page a journal PDF would have: who, where, when, what.

    Built from `<coredata>`, which arrives in the same response as the
    body. Without it a recovered file opened cold on the article's first
    sentence — no title, no authors, no DOI anywhere in the document —
    so nothing downstream could identify the file from its own contents.
    """
    small = styles["BodyText"].clone("CoverSmall")
    small.fontSize = 8.5
    small.leading = 11
    flow: list = []

    if meta.get("journal"):
        flow.append(Paragraph(_escape_xml(meta["journal"]), small))
        flow.append(Spacer(1, 4))
    if meta.get("title"):
        flow.append(Paragraph(_escape_xml(meta["title"]), styles["Title"]))
        flow.append(Spacer(1, 8))
    if meta.get("authors"):
        flow.append(Paragraph(
            _escape_xml(", ".join(meta["authors"])), styles["BodyText"],
        ))
        flow.append(Spacer(1, 4))
    for affiliation in meta.get("affiliations", []):
        flow.append(Paragraph(_escape_xml(affiliation), small))
    if meta.get("affiliations"):
        flow.append(Spacer(1, 8))

    # "Computer Law & Security Review 41 (July 2021) 105567"
    citation = meta.get("journal", "")
    if meta.get("volume"):
        citation += f" {meta['volume']}"
    if meta.get("issue"):
        citation += f"({meta['issue']})"
    if meta.get("date"):
        citation += f" ({meta['date']})"
    if meta.get("pages"):
        citation += f" {meta['pages']}"
    if citation.strip():
        flow.append(Paragraph(_escape_xml(citation.strip()), small))
    if meta.get("doi"):
        flow.append(Paragraph(
            _escape_xml(f"https://doi.org/{meta['doi']}"), small,
        ))
    if meta.get("issn"):
        flow.append(Paragraph(_escape_xml(f"ISSN {meta['issn']}"), small))
    flow.append(Spacer(1, 12))

    if meta.get("abstract"):
        flow.append(Paragraph("Abstract", styles["Heading2"]))
        flow.append(Paragraph(_escape_xml(meta["abstract"]), styles["BodyText"]))
        flow.append(Spacer(1, 8))
    if meta.get("keywords"):
        flow.append(Paragraph(
            _escape_xml("Keywords: " + "; ".join(meta["keywords"])), small,
        ))
        flow.append(Spacer(1, 12))
    if note:
        flow.append(Paragraph(_escape_xml(note), styles["Italic"]))
    return flow


def _render_text_pdf(
    blocks: list[tuple[str, str]] | str,
    out_path: Path,
    *,
    title: str = "",
    note: str = "",
    meta: dict | None = None,
) -> None:
    """Write `blocks` to `out_path` as a text-only PDF via reportlab.

    Takes the `(kind, text)` blocks from `_extract_xml_blocks` so the
    article keeps its shape: headings as headings, list items indented,
    block quotes set in. A plain string is still accepted and rendered
    as undifferentiated body paragraphs, which is what every caller got
    before the structure existed.

    Styling stays minimal — this is a text carrier for `pdftotext`, not
    a facsimile of the publisher's typesetting. But a 190,000-character
    article with no headings at all is hard to read and hard to
    navigate, and the structure costs nothing: it is in the XML already.

    Raises ImportError if reportlab is not installed; callers should
    catch and report a sensible error in that case.

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
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
    )

    if isinstance(blocks, str):
        # Legacy string input: split as the old renderer did.
        blocks = [
            ("p", para.strip())
            for para in re.split(r"\n\s*\n+|(?<=\.\s)\s{2,}", blocks)
            if para.strip()
        ]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # The note also goes in the Info dictionary, which reportlab writes
    # uncompressed. The visible line on page 1 is for the reader; this
    # copy is for `stamped_recovery_version`, which has to decide whether
    # a cache entry is current without a PDF library on hand.
    doc = SimpleDocTemplate(
        str(out_path), pagesize=letter, subject=note or None,
    )
    styles = getSampleStyleSheet()
    flowables: list = []
    if meta:
        flowables.extend(
            _cover_flowables(meta, note, styles, Paragraph, Spacer)
        )
        flowables.append(PageBreak())
    else:
        if title:
            flowables.append(Paragraph(_escape_xml(title), styles["Title"]))
            flowables.append(Spacer(1, 12))
        if note:
            flowables.append(Paragraph(_escape_xml(note), styles["Italic"]))
            flowables.append(Spacer(1, 12))

    for kind, text in blocks:
        if not text.strip():
            continue
        style_name, gap, indent = _BLOCK_STYLES.get(kind, _BLOCK_STYLES["p"])
        style = styles[style_name]
        if indent:
            # A named clone per indent level — reportlab styles are
            # shared objects, so mutating one would indent everything.
            style = style.clone(f"{style_name}Indent{indent}")
            style.leftIndent = indent
        body = _escape_xml(text)
        if kind == "li":
            body = f"\u2022 {body}"
        flowables.append(Paragraph(body, style))
        flowables.append(Spacer(1, gap))

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
        # quality artefact for this DOI from a previous run — but only
        # once it has been checked, for the reason the non-recovered
        # branch below states, and one more besides. A recovery written
        # before 0.15.0 has no front matter and its footnotes are spliced
        # mid-sentence, and nothing in the filename says so. Served
        # unchecked, it turns a deliberate re-fetch into a no-op that
        # reports success, which is indistinguishable from the real thing
        # in the return value.
        recovered_path = _cache_pdf_path(cache_dir, doi, recovered=True)
        if recovered_path.exists():
            _defect = _pdf_validate.file_defect(recovered_path)
            _stale = None if _defect else _stale_recovery_reason(recovered_path)
            if _defect is not None:
                # Corrupt: worthless to anyone, and keeping it would make
                # the corruption permanent. Same call the branch below makes.
                logger.warning(
                    "discarding corrupt recovered PDF for %s — %s", doi, _defect,
                )
                recovered_path.unlink(missing_ok=True)
            elif _stale is not None:
                # Outdated, not corrupt — the text is real, just badly
                # shaped. Refuse to serve it so the run has to go and get
                # a current one, but leave it on disk: if the publisher is
                # now unreachable, deleting it would take away the only
                # copy the user has, and returning None at least says so.
                logger.warning(
                    "not serving cached recovered PDF for %s — %s; re-fetching",
                    doi, _stale,
                )
            else:
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
        meta, blocks, annotations = _extract_article(xml_resp.content)
        body = _assemble_body(blocks, annotations)
        if not body or len(body) < 500:
            # An entitled XML response with a truly empty body is
            # vanishingly rare — treat as not-recovered rather than
            # caching a near-empty PDF.
            return None
        out_path = _cache_pdf_path(cache_dir, doi, recovered=True)
        try:
            _render_text_pdf(
                _document_flow(blocks, annotations), out_path,
                note=_recovery_note(len(annotations)), meta=meta,
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
