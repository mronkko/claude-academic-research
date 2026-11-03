"""Merge pdftotext's two outputs so two-column pages read in order.

`pdftotext -layout` keeps a table's columns aligned, which is what lets a
model tell which model column a coefficient belongs to. On a page set in
two columns it also puts both columns on one line, so every sentence is
interleaved with the other column's: on the methods-misuse pilot, 26% of
the coder's verbatim quotes were not contiguous in the text it was sent,
all of them on two-column articles. pdftotext's default (reading-order)
mode gets every one of those quotes right but flattens tables into one
cell per line.

So each page is taken from whichever output suits it:

  - a page without two prose columns: `-layout`, as before;
  - a two-column page with no table: the reading-order text;
  - a two-column page with a table: the reading-order text, then the
    page's table blocks verbatim from `-layout` between `TABLE_START` and
    `TABLE_END`, so the prose reads in order and the table stays aligned.

A full-width head above the columns (title, authors) is taken from
`-layout` rather than from the reading-order text: poppler's reading-order
mode splits a widely letter-spaced title into one word per paragraph and
scrambles its order, while `-layout` has it intact (see `_with_layout_head`).

Both outputs hold every word, so a misjudged page costs table alignment
or reading order on that page, never text. The two must have the same
page count (one form feed per page); if they do not, `-layout` is kept.

Measured against the reading-order text on 40 held-out articles of the
review: prose paragraphs contiguous 37% -> 91%, table rows kept verbatim
96%, no word lost. Stdlib only.
"""

from __future__ import annotations

import re
from collections import Counter

#: Marks the aligned copy of a two-column page's tables.
TABLE_START = "[TABLES ON THIS PAGE, AS LAID OUT]"
TABLE_END = "[END OF TABLES]"

_GAP = 2                       # spaces a column gutter is wide, at least
_MIN_PROSE_LINES = 4           # lines with prose on both sides of it
_TABLE_ROWS = 5                # rows of 3+ cells that make a page "with a table"
_WORD = re.compile(r"[^\W\d_]{3,}")
_CELLS = re.compile(r" {3,}")


def _cells(line: str) -> int:
    return len([c for c in _CELLS.split(line.strip()) if c])


def _prose(s: str) -> bool:
    """Words set as running text: three or more, and no cell-sized gap
    inside (a table whose cells hold words has one between cells)."""
    s = s.strip()
    return len(_WORD.findall(s)) >= 3 and "   " not in s


def _two_sided(line: str, c: int) -> bool:
    return (len(line) > c + _GAP and line[c:c + _GAP] == " " * _GAP
            and _prose(line[:c]) and _prose(line[c:]))


def gutter(page: str) -> int | None:
    """The character column where a `-layout` page's two prose columns
    meet, or None for a page that is not set in two columns.

    Chosen from lines with words on *both* sides of a gap, so the gaps
    between a table's columns never win. The search runs over the middle
    of the text block, whose left edge is the median line start rather
    than the minimum: a rotated margin watermark ("This document is
    copyrighted by ...") lands at the far left of every page.
    """
    lines = [ln for ln in page.split("\n") if ln.strip()]
    if len(lines) < 10:
        return None
    starts = sorted(len(ln) - len(ln.lstrip()) for ln in lines)
    left = starts[len(starts) // 2]
    width = max(len(ln) for ln in lines) - left
    best, best_n = None, 0
    for c in range(left + int(width * 0.3), left + int(width * 0.7)):
        n = sum(1 for ln in lines if _two_sided(ln, c))
        if n > best_n:
            best, best_n = c, n
    return best if best_n >= _MIN_PROSE_LINES else None


def table_rows(page: str) -> int:
    """Lines split into three or more cells: a table's rows."""
    return sum(1 for ln in page.split("\n") if _cells(ln) >= 3)


def table_blocks(page: str) -> list[str]:
    """The page's tables, verbatim: blank-line-separated blocks of `-layout`
    text with rows of three or more cells."""
    out = []
    for block in re.split(r"\n[ \t]*\n", page):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        rows = sum(1 for ln in lines if _cells(ln) >= 3)
        if rows >= 2 or (rows == 1 and len(lines) <= 3):
            out.append(block.strip("\n").rstrip())
    return out


def _with_layout_head(layout: str, reading: str, c: int) -> str:
    """`reading`, with the full-width head set from `layout`.

    The head is every line above the first line that is two-sided at the
    gutter: a title, author block or running head spanning the page.
    Poppler's reading-order mode can emit such a block word by word and
    out of order ("Intra-cultural / Implications / Variation: / for ..."),
    where `-layout` keeps it intact. The head's words open the
    reading-order text too, so they are matched off its start as a
    multiset and the remainder is appended to the layout head.

    Strict by design: if a word that is not in the head turns up before
    every head word has been matched, the two outputs do not line up the
    way this assumes and `reading` is returned unchanged, which is the
    behaviour before this existed. Nothing is dropped either way.
    """
    lines = layout.split("\n")
    first = next((i for i, ln in enumerate(lines) if _two_sided(ln, c)), 0)
    head = [" ".join(ln.split()) for ln in lines[:first]]
    while head and not head[0]:
        head.pop(0)
    while head and not head[-1]:
        head.pop()
    need = Counter(w for ln in head for w in ln.split())
    left = sum(need.values())
    if not left:
        return reading
    for m in re.finditer(r"\S+", reading):
        if need[m.group()] <= 0:
            return reading
        need[m.group()] -= 1
        left -= 1
        if not left:
            tail = reading[m.end():].lstrip("\n")
            return "\n".join(head) + "\n\n" + tail
    return reading


def merge_page(layout: str, reading: str) -> str:
    c = gutter(layout)
    if c is None:
        return layout
    reading = _with_layout_head(layout, reading, c)
    if table_rows(layout) < _TABLE_ROWS:
        return reading
    tables = "\n\n".join(table_blocks(layout))
    return f"{reading.rstrip()}\n\n{TABLE_START}\n{tables}\n{TABLE_END}\n"


def merge(layout: str, reading: str) -> str:
    """One text from pdftotext's `-layout` and reading-order outputs."""
    lp, rp = layout.split("\f"), reading.split("\f")
    if len(lp) != len(rp):
        return layout
    return "\f".join(merge_page(lay, rd) for lay, rd in zip(lp, rp, strict=True))
