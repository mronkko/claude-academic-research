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

Richer entries — `{field, quote, status, severity, comment}`, as an
issue-list codebook writes them — get the entry's own comment
(`A02 · present · minor — <comment>`), and with `--entry-tags` the tags
`<prefix>/issue:<field>`, `<prefix>/status:<status>`,
`<prefix>/severity:<severity>` and `<prefix>/layer:<layer>`. The layer
comes from the id's letter prefix via `--layer D=design` (or
`HIGHLIGHT_LAYERS` in screening_config.py); an `OTHER-n` entry takes the
`layer` its `other_findings` record names, else `other`. `--color-by
severity` colours by severity instead of theme. An entry flagged
`supplement_may_cover` (coded from the article alone, but the detail may
be in a supplement) is also tagged `<prefix>/supplement:may-cover`, and
the flag is a column of the highlight log. Entries quoting the same
passage on the same page become one highlight carrying all their tags,
with their comments joined. `HIGHLIGHT_COLOR_BY` and `HIGHLIGHT_ENTRY_TAGS`
in screening_config.py set the defaults of the two flags.

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
import os
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
LOG_COLUMNS = ["item_key", "attachment_key", "field", "quote", "page", "status", "detail",
               "supplement_may_cover"]

#: Theme → highlight colour. Zotero's own annotation palette.
THEME_COLORS = {
    "assumptions": "#ffd400",     # yellow
    "implementation": "#5fb236",  # green
    "setting": "#2ea8e5",         # blue
    "overclaims": "#ff6666",      # red
    "other": "#a28ae5",           # purple
    # One colour per numbered assumption, for reviews that code several.
    "a0": "#aaaaaa",              # gray
    "a1": "#f19837",              # orange
    "a2": "#e56eee",              # magenta
    "a3": "#ffd400",              # yellow
}

#: `--color-by severity`: a present entry by its severity, an exemplary one
#: green, an uncodebooked `OTHER-n` finding purple, anything else gray.
SEVERITY_COLORS = {
    "major": "#ff6666",           # red
    "moderate": "#f19837",        # orange
    "minor": "#ffd400",           # yellow
    "exemplary": "#5fb236",       # green
    "other": "#a28ae5",           # purple
    "unknown": "#aaaaaa",         # gray
}
COLOR_BY = ("theme", "severity")
COMMENT_LIMIT = 4000
_ENTRY_KEYS = ("status", "severity", "comment", "location")

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


@dataclass
class HighlightStyle:
    """How entries become colours and tags; the defaults are the old behaviour."""

    color_by: str = "theme"                 # theme | severity
    entry_tags: bool = False                # issue/status/severity/layer tags
    layers: dict[str, str] = field(default_factory=dict)   # id prefix → layer


def _config_module(config_path: str):
    path = Path(config_path)
    if not path.is_file():
        return None
    return screening_common.load_config_module(str(path), "screening_config")


def style_from_config(config_path: str) -> HighlightStyle:
    """`HIGHLIGHT_COLOR_BY`, `HIGHLIGHT_ENTRY_TAGS`, `HIGHLIGHT_LAYERS`."""
    mod = _config_module(config_path)
    style = HighlightStyle()
    if mod is None:
        return style
    color_by = getattr(mod, "HIGHLIGHT_COLOR_BY", "") or "theme"
    if color_by not in COLOR_BY:
        raise SystemExit(f"HIGHLIGHT_COLOR_BY must be one of {COLOR_BY}, not {color_by!r}")
    style.color_by = color_by
    style.entry_tags = bool(getattr(mod, "HIGHLIGHT_ENTRY_TAGS", False))
    style.layers = dict(getattr(mod, "HIGHLIGHT_LAYERS", {}) or {})
    return style


def themes_from_config(config_path: str) -> dict[str, str]:
    """Field → theme from the project's `screening_config.py`.

    Either a `"theme"` key on a `FULLTEXT_CODING_FIELDS` entry, or a
    `HIGHLIGHT_THEMES` dict (which wins on a clash). Missing file, missing
    names and unknown themes all mean "no theme"; an unknown theme falls
    back to the `other` colour when used.
    """
    mod = _config_module(config_path)
    if mod is None:
        return {}
    themes = {
        f["name"]: f["theme"]
        for f in getattr(mod, "FULLTEXT_CODING_FIELDS", [])
        if f.get("name") and f.get("theme")
    }
    themes.update(getattr(mod, "HIGHLIGHT_THEMES", {}) or {})
    return themes


def quote_hash(attachment_key: str, page: int, quote: str) -> str:
    raw = f"{attachment_key}|{page}|{squash(quote)}"
    return hashlib.sha1(raw.encode()).hexdigest()[:10]


def sort_index(page_index: int, char_offset: int, top: float) -> str:
    # Zotero rejects a negative field; a rect above the page top (an offset
    # CropBox) would otherwise format as e.g. "-0127".
    return f"{page_index:05d}|{max(char_offset, 0):06d}|{max(int(top), 0):05d}"


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


def _json_list(raw) -> list:
    """A coded field holding a JSON list, as a string or already decoded."""
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        data = json.loads(raw, strict=False)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def parse_quotes(raw) -> list[dict]:
    """The `evidence_quotes` field value → [{field, quote, ...}], tolerant.

    `status`, `severity`, `comment` and `location` are kept when present.
    """
    out = []
    for e in _json_list(raw):
        if isinstance(e, dict) and str(e.get("quote", "")).strip():
            entry = {"field": str(e.get("field", "")).strip(),
                     "quote": str(e["quote"]).strip()}
            for k in _ENTRY_KEYS:
                if e.get(k) not in (None, ""):
                    entry[k] = str(e[k]).strip()
            if _truthy(e.get("supplement_may_cover")):
                entry["supplement_may_cover"] = True
            out.append(entry)
    return out


def _truthy(value) -> bool:
    """A JSON boolean, or the string a model sometimes writes instead."""
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1")
    return value is True


def parse_other_layers(raw) -> dict[str, str]:
    """`other_findings` → {"OTHER-1": "analysis", ...}."""
    return {
        str(f["id"]).strip(): str(f["layer"]).strip()
        for f in _json_list(raw)
        if isinstance(f, dict) and f.get("id") and f.get("layer")
    }


def _field_names(entry: dict) -> list[str]:
    # One passage may support several fields, listed "a; b".
    return [n.strip() for n in entry["field"].split(";") if n.strip()] or [entry["field"]]


def _is_other(name: str) -> bool:
    return name.upper().startswith("OTHER")


def entry_comment(entry: dict, value: str) -> str:
    """`A02 · present · minor — <comment>`, else the old `field: value`."""
    if not entry.get("comment"):
        return entry["field"] + (f": {value}" if value else "")
    head = " · ".join(
        x for x in (entry["field"], entry.get("status"), entry.get("severity")) if x)
    return f"{head} — {entry['comment']}"


def entry_layer(name: str, style: HighlightStyle, other_layers: dict[str, str]) -> str:
    if _is_other(name):
        return other_layers.get(name, "other")
    prefix = name.rstrip("0123456789")
    return style.layers.get(prefix, "") if prefix != name else ""


def entry_tags(entry: dict, ns: str, style: HighlightStyle,
               other_layers: dict[str, str]) -> list[str]:
    """issue/status/severity/layer tags for one entry; [] unless `entry_tags`."""
    if not style.entry_tags:
        return []
    base = ns.rstrip("/")
    tags = []
    for name in _field_names(entry):
        tags.append(f"{base}/issue:{name}")
        layer = entry_layer(name, style, other_layers)
        if layer:
            tags.append(f"{base}/layer:{layer}")
    for k in ("status", "severity"):
        if entry.get(k):
            tags.append(f"{base}/{k}:{entry[k]}")
    if entry.get("supplement_may_cover"):
        tags.append(f"{base}/supplement:may-cover")
    return tags


_SEVERITY_RANK = ["major", "moderate", "minor", "exemplary", "other", "unknown"]


def severity_class(entry: dict) -> str:
    """The `SEVERITY_COLORS` key for one entry."""
    if all(_is_other(n) for n in _field_names(entry)):
        return "other"
    if entry.get("status") == "exemplary":
        return "exemplary"
    sev = entry.get("severity", "")
    return sev if sev in ("major", "moderate", "minor") else "unknown"


def merged_color(entries: list[dict], themes: dict[str, str], style: HighlightStyle) -> str:
    """Theme colour of the first entry, or the most severe entry's colour."""
    if style.color_by == "severity":
        worst = min((severity_class(e) for e in entries), key=_SEVERITY_RANK.index)
        return SEVERITY_COLORS[worst]
    theme = themes.get(_field_names(entries[0])[0], "other")
    return THEME_COLORS.get(theme, THEME_COLORS["other"])


def build_annotation(
    attachment_key: str, located: Located, entry: dict, value: str,
    theme: str, ns: str, *, comment: str | None = None, color: str | None = None,
    extra_tags: list[str] = (),
) -> dict:
    page_index = located.page - 1
    if comment is None:
        comment = entry_comment(entry, value)
    return {
        "itemType": "annotation",
        "parentItem": attachment_key,
        "annotationType": "highlight",
        "annotationText": entry["quote"],
        "annotationComment": comment[:COMMENT_LIMIT],
        "annotationColor": color or THEME_COLORS.get(theme, THEME_COLORS["other"]),
        "annotationPageLabel": str(located.page),
        "annotationSortIndex": sort_index(page_index, located.offset, located.top),
        "annotationPosition": json.dumps(
            {"pageIndex": page_index, "rects": located.rects}),
        "tags": [
            {"tag": f"{ns.rstrip('/')}/{AI_TAG_SUFFIX}"},
            {"tag": HASH_TAG + quote_hash(attachment_key, located.page, entry["quote"])},
            *({"tag": t} for t in extra_tags),
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
    style: HighlightStyle | None = None,
) -> list[dict]:
    style = style or HighlightStyle()
    item_key = item["key"]
    rows: list[dict] = []

    def row(status, entry=None, att="", page=0, detail=""):
        rows.append({
            "item_key": item_key, "attachment_key": att,
            "field": (entry or {}).get("field", ""),
            "quote": (entry or {}).get("quote", ""),
            "page": page or "", "status": status, "detail": detail,
            "supplement_may_cover": "true" if (entry or {}).get(
                "supplement_may_cover") else "",
        })

    quotes = parse_quotes((payload.get("fields") or {}).get("evidence_quotes", ""))
    if not quotes:
        row("no_quotes")
        return rows
    children = zot.children(item_key)
    att_key, path, problem = choose_attachment(item, children, payload, storage)
    if problem:
        row(problem)
        return rows
    # A quote not in the article is looked for in the appendix/supplement
    # attachments, which the coder read after it (fulltext_code appends them).
    import fulltext_code
    supplements = [
        (a.get("key") or a.get("data", {}).get("key", ""), p)
        for a in children if fulltext_code.is_supplement_attachment(a)
        for p in [fulltext_code._find_pdf_path(
            item, {item_key: [a]}, zotero_storage=storage, supplement=True)]
        if p is not None
    ]
    leftover = quotes
    for i, (key, pdf) in enumerate([(att_key, path)] + supplements):
        if not leftover:
            break
        leftover = _highlight_on(zot, key, pdf, leftover, payload, ns, themes,
                                 row, dry_run=dry_run, replace=replace,
                                 last=i == len(supplements), style=style)
    return rows


def _highlight_on(
    zot, att_key: str, path: Path, quotes: list[dict], payload: dict, ns: str,
    themes: dict[str, str], row, *, dry_run: bool, replace: bool, last: bool,
    style: HighlightStyle,
) -> list[dict]:
    """Highlight `quotes` found in one attachment; return those not found.

    Not-found and ambiguous quotes are logged only on the `last` attachment
    tried, so a quote that turns up in an appendix is not also reported
    missing from the article.
    """
    pages, heights = read_pdf(path)
    if not any(p.text for p in pages):
        row("no_text_layer", att=att_key)
        return quotes

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

    # Entries quoting the same passage on the same page share a hash and
    # become one highlight: grouped here, in first-seen order.
    groups: dict[str, tuple[Located, list[dict]]] = {}
    leftover: list[dict] = []
    for entry in quotes:
        loc = locate_quote(pages, heights, entry["quote"])
        if loc.status != "found":
            if last:
                row(loc.status, entry, att_key, detail=",".join(map(str, loc.pages)))
            else:
                leftover.append(entry)
            continue
        h = HASH_TAG + quote_hash(att_key, loc.page, entry["quote"])
        if h in have:
            row("skipped_existing", entry, att_key, loc.page)
            continue
        groups.setdefault(h, (loc, []))[1].append(entry)

    coded = payload.get("fields") or {}
    other_layers = parse_other_layers(coded.get("other_findings", ""))
    batch: list[tuple[list[dict], dict, Located]] = []
    for loc, entries in groups.values():
        comments, tags = [], []
        for entry in entries:
            # The fallback comment shows each field's coded value.
            names = _field_names(entry)
            value = "; ".join(
                str(coded.get(n, "")) for n in names if coded.get(n, ""))[:120]
            c = entry_comment(entry, value)
            if c not in comments:
                comments.append(c)
            tags += [t for t in entry_tags(entry, ns, style, other_layers) if t not in tags]
        first = entries[0]
        theme = themes.get(_field_names(first)[0], "other")
        batch.append((entries, build_annotation(
            att_key, loc, first, "", theme, ns, comment="\n\n".join(comments),
            color=merged_color(entries, themes, style), extra_tags=tags), loc))
    if batch and not dry_run:
        zot.create_highlights([b[1] for b in batch])
    for entries, _ann, loc in batch:
        detail = f"merged {len(entries)}" if len(entries) > 1 else ""
        for entry in entries:
            row("would_create" if dry_run else "created", entry, att_key, loc.page,
                detail=detail)
    return leftover


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
                        help="Colour theme per coded field, overriding screening_config.py "
                             f"({', '.join(THEME_COLORS)}); repeatable.")
    parser.add_argument("--color-by", choices=COLOR_BY, default=None,
                        help="Colour by the field's theme or by the entry's severity "
                             "(major red, moderate orange, minor yellow, exemplary green, "
                             "OTHER-n purple). Default: HIGHLIGHT_COLOR_BY, else theme.")
    parser.add_argument("--entry-tags", action=argparse.BooleanOptionalAction, default=None,
                        help="Tag each highlight <prefix>/issue:<field>, status:, severity: "
                             "and layer:. Default: HIGHLIGHT_ENTRY_TAGS, else off.")
    parser.add_argument("--layer", action="append", default=[], metavar="PREFIX=LAYER",
                        help="Layer tag for ids with this letter prefix, e.g. D=design, "
                             "adding to HIGHLIGHT_LAYERS; repeatable.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--replace", action="store_true",
                        help="Delete this script's highlights on each attachment first.")
    args = parser.parse_args()

    ns = screening_common.load_tag_namespace(args.tag_prefix, args.config)
    themes = themes_from_config(args.config)
    themes.update(t.split("=", 1) for t in args.theme if "=" in t)
    style = style_from_config(args.config)
    if args.color_by:
        style.color_by = args.color_by
    if args.entry_tags is not None:
        style.entry_tags = args.entry_tags
    style.layers.update(t.split("=", 1) for t in args.layer if "=" in t)
    # Same default as fulltext_code.py. Without it a run with no flag found
    # no stored PDF at all and reported every item no_pdf.
    storage = Path(
        args.zotero_storage
        or os.environ.get("ZOTERO_DATA_DIR")
        or (Path.home() / "Zotero")
    ).expanduser()
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
                                dry_run=args.dry_run, replace=args.replace,
                                style=style))

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
