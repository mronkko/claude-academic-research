#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyzotero>=1.16",
#     "tenacity>=8.0",
#     "urllib3>=2.0",
#     "httpx>=0.25",
#     "pymupdf>=1.24",
# ]
# ///
"""Turn the coder's verbatim evidence quotes into Zotero PDF highlights.

For each item tagged `<prefix>/fulltext:include`, read the quotes the
coder stored in the item's `SLR Coding` note (the `evidence_quotes` field:
a JSON list of `{field, quote}`), find each one in the PDF the coder
read, and create one highlight per quote — coloured by theme, commented
with the field and its coded value, tagged `<prefix>/ai-highlight` so a
human can filter or delete them all.

A quote that matches nothing is reported `not_found`, which is also a
faithfulness check on the model: it claimed the words were verbatim. A
quote on more than one page is `ambiguous` and, by default, not
highlighted.

Matching is on the PDF's own text layer (PyMuPDF words), compared as
letters and digits only, so line-break hyphenation, ligatures, curly
quotes and spacing differences cannot defeat it. The same words give the
highlight rectangles, which is why the pdftotext cache is not used here.

Idempotent: a highlight is keyed by a hash of (attachment, page, quote)
carried in an `ai-hl:<hash>` tag; re-running skips the ones that exist.
`--replace` deletes this script's highlights on the attachment first.

Usage:
    uv run highlight_quotes.py --user --collection ABCD1234 \\
        --zotero-storage ~/Zotero [--dry-run] [--keys K1,K2]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import screening_common  # noqa: E402
import tag_prefix  # noqa: E402
import zotero_io  # noqa: E402

AI_TAG_SUFFIX = "ai-highlight"
HASH_TAG = "ai-hl:"
LOG_COLUMNS = ["item_key", "attachment_key", "field", "quote", "page", "status", "detail"]

#: Theme → highlight colour. Zotero's own annotation palette.
THEME_COLORS = {
    "assumptions": "#ffd400",     # yellow
    "implementation": "#5fb236",  # green
    "setting": "#2ea8e5",         # blue
    "overclaims": "#ff6666",      # red
    "other": "#a28ae5",           # purple
}

_QUOTE_TRANSLATION = str.maketrans({
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "−": "-", "­": "",
})


def squash(text: str) -> str:
    """Letters and digits only, case-folded, NFKC-normalised.

    NFKC turns ligatures (ﬁ, ﬂ, ﬃ) into their letters. Dropping every
    non-alphanumeric character is what makes hyphenation at a line break,
    stray spaces and punctuation style irrelevant to a match. Cheap and
    deliberately blunt: a 10–30 word quote is distinctive enough that two
    different passages do not collide.
    """
    text = unicodedata.normalize("NFKC", text).translate(_QUOTE_TRANSLATION)
    return "".join(c for c in text.casefold() if c.isalnum())


@dataclass
class PageIndex:
    """One page's squashed text and where each character came from."""

    page: int                       # 1-indexed
    text: str = ""
    word_of_char: list[int] = field(default_factory=list)
    words: list[tuple] = field(default_factory=list)   # PyMuPDF word tuples


def index_page(page_number: int, words: list[tuple]) -> PageIndex:
    """Build a `PageIndex` from PyMuPDF `get_text("words")` tuples."""
    idx = PageIndex(page=page_number, words=words)
    chars: list[str] = []
    for wi, w in enumerate(words):
        s = squash(w[4])
        chars.append(s)
        idx.word_of_char.extend([wi] * len(s))
    idx.text = "".join(chars)
    return idx


def find_in_page(idx: PageIndex, quote: str) -> list[list[tuple]]:
    """Every occurrence of `quote` on the page, as lists of word tuples."""
    needle = squash(quote)
    if len(needle) < 12:      # too short to be evidence; refuse to guess
        return []
    hits: list[list[tuple]] = []
    start = idx.text.find(needle)
    while start != -1:
        first = idx.word_of_char[start]
        last = idx.word_of_char[start + len(needle) - 1]
        hits.append(idx.words[first:last + 1])
        start = idx.text.find(needle, start + len(needle))
    return hits


def line_rects(words: list[tuple], page_height: float) -> list[list[float]]:
    """One rectangle per text line, in PDF user space (origin bottom-left)."""
    lines: dict[tuple, list[float]] = {}
    for w in words:
        key = (w[5], w[6])        # block, line
        r = lines.setdefault(key, [w[0], w[1], w[2], w[3]])
        r[0], r[1] = min(r[0], w[0]), min(r[1], w[1])
        r[2], r[3] = max(r[2], w[2]), max(r[3], w[3])
    return [
        [round(r[0], 3), round(page_height - r[3], 3),
         round(r[2], 3), round(page_height - r[1], 3)]
        for r in lines.values()
    ]


def quote_hash(attachment_key: str, page: int, quote: str) -> str:
    raw = f"{attachment_key}|{page}|{squash(quote)}"
    return hashlib.sha1(raw.encode()).hexdigest()[:10]


def sort_index(page_index: int, char_offset: int, top: float) -> str:
    return f"{page_index:05d}|{char_offset:06d}|{int(top):05d}"


@dataclass
class Located:
    status: str                 # found | not_found | ambiguous
    page: int = 0
    rects: list[list[float]] = field(default_factory=list)
    offset: int = 0
    top: float = 0.0
    pages: list[int] = field(default_factory=list)
    height: float = 0.0


def locate_quote(pages: list[PageIndex], heights: dict[int, float], quote: str) -> Located:
    """Where `quote` is in the document: one page, several, or nowhere."""
    found: list[tuple[PageIndex, list[tuple]]] = []
    for idx in pages:
        for hit in find_in_page(idx, quote):
            found.append((idx, hit))
    if not found:
        return Located("not_found")
    page_numbers = sorted({idx.page for idx, _ in found})
    if len(page_numbers) > 1:
        return Located("ambiguous", pages=page_numbers)
    idx, hit = found[0]        # same page, repeated: take the first
    h = heights[idx.page]
    return Located(
        "found", page=idx.page, rects=line_rects(hit, h),
        offset=idx.text.find(squash(quote)), top=h - max(w[3] for w in hit),
        pages=[idx.page], height=h,
    )


def read_pdf(path: Path) -> tuple[list[PageIndex], dict[int, float]]:
    import pymupdf

    pages: list[PageIndex] = []
    heights: dict[int, float] = {}
    with pymupdf.open(str(path)) as doc:
        for i, page in enumerate(doc, 1):
            heights[i] = float(page.rect.height)
            pages.append(index_page(i, page.get_text("words")))
    return pages, heights


def parse_quotes(raw: str) -> list[dict]:
    """The `evidence_quotes` field value → [{field, quote}], tolerant."""
    if not raw or not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    out = []
    for e in data if isinstance(data, list) else []:
        if isinstance(e, dict) and str(e.get("quote", "")).strip():
            out.append({"field": str(e.get("field", "")).strip(),
                        "quote": str(e["quote"]).strip()})
    return out


def build_annotation(
    attachment_key: str, located: Located, entry: dict, value: str,
    theme: str, ns: str,
) -> dict:
    page_index = located.page - 1
    comment = entry["field"] + (f": {value}" if value else "")
    return {
        "itemType": "annotation",
        "parentItem": attachment_key,
        "annotationType": "highlight",
        "annotationText": entry["quote"],
        "annotationComment": comment[:500],
        "annotationColor": THEME_COLORS.get(theme, THEME_COLORS["other"]),
        "annotationPageLabel": str(located.page),
        "annotationSortIndex": sort_index(page_index, located.offset, located.top),
        "annotationPosition": json.dumps(
            {"pageIndex": page_index, "rects": located.rects}),
        "tags": [
            {"tag": f"{ns.rstrip('/')}/{AI_TAG_SUFFIX}"},
            {"tag": HASH_TAG + quote_hash(attachment_key, located.page, entry["quote"])},
        ],
    }


def choose_attachment(
    item: dict, children: list[dict], payload: dict, storage: Path | None,
) -> tuple[str, Path | None, str]:
    """(attachment_key, path, problem). `problem` is '' when one was chosen.

    Several readable PDFs and no recorded hash is a refusal, not a guess:
    a highlight on the wrong PDF is silent.
    """
    import fulltext_code

    candidates: list[tuple[str, Path]] = []
    for att in children:
        d = att.get("data", {})
        if d.get("itemType") != "attachment" or d.get("contentType") != "application/pdf":
            continue
        path = fulltext_code._find_pdf_path(
            item, {item["key"]: [att]}, zotero_storage=storage)
        if path is not None:
            candidates.append((att.get("key") or d.get("key", ""), path))
    if not candidates:
        return "", None, "no_pdf"
    want = payload.get("pdf_sha256", "")
    if want:
        for key, path in candidates:
            if hashlib.sha256(path.read_bytes()).hexdigest() == want:
                return key, path, ""
        return "", None, "pdf_changed_since_coding"
    if len(candidates) == 1:
        return candidates[0][0], candidates[0][1], ""
    return "", None, "ambiguous_attachment"


def process_item(
    zot, item: dict, payload: dict, ns: str, storage: Path | None,
    themes: dict[str, str], *, dry_run: bool, replace: bool,
) -> list[dict]:
    item_key = item["key"]
    rows: list[dict] = []

    def row(status, entry=None, att="", page=0, detail=""):
        rows.append({
            "item_key": item_key, "attachment_key": att,
            "field": (entry or {}).get("field", ""),
            "quote": (entry or {}).get("quote", ""),
            "page": page or "", "status": status, "detail": detail,
        })

    quotes = parse_quotes((payload.get("fields") or {}).get("evidence_quotes", ""))
    if not quotes:
        row("no_quotes")
        return rows
    att_key, path, problem = choose_attachment(
        item, zot.children(item_key), payload, storage)
    if problem:
        row(problem)
        return rows
    pages, heights = read_pdf(path)
    if not any(p.text for p in pages):
        row("no_text_layer", att=att_key)
        return rows

    ai_tag = f"{ns.rstrip('/')}/{AI_TAG_SUFFIX}"
    existing = zot.list_annotations(att_key)
    mine = [a for a in existing
            if any(t.get("tag") == ai_tag for t in a.get("data", {}).get("tags", []))]
    if replace and mine and not dry_run:
        for a in mine:
            zot.delete_item(a["key"])
        mine = []
    have = {t["tag"] for a in mine for t in a["data"].get("tags", [])
            if t.get("tag", "").startswith(HASH_TAG)}

    batch: list[tuple[dict, dict, Located]] = []
    for entry in quotes:
        loc = locate_quote(pages, heights, entry["quote"])
        if loc.status != "found":
            row(loc.status, entry, att_key, detail=",".join(map(str, loc.pages)))
            continue
        h = HASH_TAG + quote_hash(att_key, loc.page, entry["quote"])
        if h in have:
            row("skipped_existing", entry, att_key, loc.page)
            continue
        # One passage may support several fields, listed "a; b". The
        # comment shows each field's coded value; the first field picks
        # the colour.
        names = [n.strip() for n in entry["field"].split(";") if n.strip()]
        coded = payload.get("fields") or {}
        value = "; ".join(
            str(coded.get(n, "")) for n in names if coded.get(n, "")
        )[:120] if len(names) > 1 else str(coded.get(entry["field"], ""))[:120]
        theme = themes.get(names[0] if names else entry["field"], "other")
        batch.append((entry, build_annotation(att_key, loc, entry, value, theme, ns), loc))
    if batch and not dry_run:
        zot.create_highlights([b[1] for b in batch])
    for entry, _ann, loc in batch:
        row("would_create" if dry_run else "created", entry, att_key, loc.page)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    zotero_io.add_library_args(parser)
    parser.add_argument("--collection", required=True, help="Zotero collection key.")
    parser.add_argument("--out", default="output/highlight_log.csv",
                        help="CSV log (default: output/highlight_log.csv).")
    tag_prefix.add_argument(parser)
    parser.add_argument("--config", default="./screening_config.py")
    parser.add_argument("--zotero-storage", default="",
                        help="Zotero data directory holding storage/<key>/<file>.")
    parser.add_argument("--keys", default="", help="Comma-separated item keys to limit to.")
    parser.add_argument("--theme", action="append", default=[], metavar="FIELD=THEME",
                        help="Colour theme per coded field "
                             f"({', '.join(THEME_COLORS)}); repeatable.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--replace", action="store_true",
                        help="Delete this script's highlights on each attachment first.")
    args = parser.parse_args()

    ns = screening_common.load_tag_namespace(args.tag_prefix, args.config)
    themes = dict(t.split("=", 1) for t in args.theme if "=" in t)
    storage = Path(args.zotero_storage).expanduser() if args.zotero_storage else None
    zot = zotero_io.ZoteroClient.from_args(args)
    include_tag = f"{tag_prefix.family(ns, 'fulltext')}include"
    items = zot.items_with_tag(args.collection, include_tag)
    if args.keys:
        wanted = {k.strip() for k in args.keys.split(",") if k.strip()}
        items = [i for i in items if i["key"] in wanted]
    marker = zotero_io.slr_coding_marker(ns)
    print(f"{len(items)} included item(s) in {args.collection}", flush=True)

    log: list[dict] = []
    for item in items:
        payload = None
        for child in zot.children(item["key"]):
            body = child.get("data", {}).get("note", "") or ""
            if child.get("data", {}).get("itemType") == "note" and marker in body:
                payload = zotero_io.parse_slr_coding_note(body)
                if payload:
                    break
        if not payload:
            log.append({"item_key": item["key"], "attachment_key": "", "field": "",
                        "quote": "", "page": "", "status": "no_coding_note", "detail": ""})
            continue
        log.extend(process_item(zot, item, payload, ns, storage, themes,
                                dry_run=args.dry_run, replace=args.replace))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=LOG_COLUMNS)
        w.writeheader()
        w.writerows(log)
    counts: dict[str, int] = {}
    for r in log:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("status counts: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print(f"log: {out}")
    return 0


if __name__ == "__main__":
    from core.console import enable_utf8_output
    enable_utf8_output()
    sys.exit(main())
