"""Normalise an abstract before it is written to Zotero.

Sources deliver abstracts with debris that screening then reads as part
of the text. Measured on one real library (2026-09-22, 22,229
abstracts): 2,715 carried a publisher copyright notice — Scopus returns
it inside the abstract, often fused onto the first sentence with no
space ("…part of Springer Nature.Given recent concerns…") — 624 held
HTML entities (`&amp;`), and 1,716 opened with a fused "Abstract"
heading ("AbstractWe study…").

Stdlib-only, so both `enrich_abstracts.py` and the one-off
`clean_abstracts.py` repair can use it without new dependencies.

The rule throughout is **remove only what is unambiguous**. A notice is
cut only where its end is certain — the source's own copyright string,
"All rights reserved.", a fused sentence boundary, or a publisher-name
ending. A notice whose end cannot be told from the abstract's first
sentence is kept: a stray "© 2019 Elsevier B.V." costs a reader
nothing, whereas a first sentence cut in half changes what the
abstract says.
"""

from __future__ import annotations

import html
import re

#: Where a leading notice starts.
_NOTICE_START = re.compile(
    r"^\s*(?:Copyright\s*)?(?:©|\(c\)|Copyright)\s*\d{4}", re.IGNORECASE,
)

#: How far into the text a leading notice may run. Real ones are well
#: under 250 characters; the cap stops a match from reaching into the
#: abstract's own sentences.
_NOTICE_MAX = 250

#: The unambiguous ends of a leading notice, tried in this order.
_ENDS = (
    re.compile(r"All rights reserved\.", re.IGNORECASE),
    # "…Springer Nature.Given…": a sentence end with no space before a
    # capital, which ordinary prose never has. The lower-case letter
    # before the dot keeps "B.V" out.
    re.compile(r"[a-z)]\.(?=[A-Z])"),
)

#: A publisher-name ending followed by the abstract's first word.
_PUBLISHER_END = re.compile(
    r"\b(?:Ltd|Inc|LLC|GmbH|Nature|Sons|Society|Association|Press|"
    r"Publishing|Publications|Group|Authors?)\b\.?(?=\s+[A-Z])"
)

#: The second half of a two-part notice.
_PUBLISHED_BY = re.compile(r"^\s*Published by\b")

#: A trailing notice: the last "©" and everything after it, but only
#: after a sentence end, so a notice that *opens* the text is left to
#: `_strip_leading_notice` and its stricter rules.
_TRAILING_NOTICE = re.compile(
    r"(?<=[.!?)\]])\s*(?:Copyright\s*)?(?:©|\(c\))[^©]{0,300}$"
)

#: A leading "Abstract" heading, fused or not. Only when the next word
#: is capitalised, so "Abstract reasoning predicts…" survives.
_HEADING = re.compile(r"^\s*(?:ABSTRACT|Abstract)\s*[:.\-–—]?\s*(?=[A-Z0-9])")


def _unescape(text: str) -> str:
    """Undo entity escaping, including the doubled `&amp;amp;` some
    sources send."""
    for _ in range(3):
        once = html.unescape(text)
        if once == text:
            break
        text = once
    return text


def _strip_hint(text: str, notice: str) -> str:
    notice = " ".join(notice.split())
    if not notice:
        return text
    flat = " ".join(text.split())
    if flat.startswith(notice):
        return flat[len(notice):]
    if flat.endswith(notice):
        return flat[: -len(notice)]
    return text


def _strip_leading_notice(text: str) -> str:
    if not _NOTICE_START.match(text):
        return text
    window = text[:_NOTICE_MAX]
    for pattern in _ENDS:
        m = pattern.search(window)
        if m:
            return text[m.end():]
    # The *first* publisher ending, not the last: a later one may be the
    # abstract's own ("…American Psychological Association Members…").
    m = _PUBLISHER_END.search(window)
    if m is None:
        return text
    rest = text[m.end():]
    # "© 2020 The Authors. Published by Elsevier Ltd." is one notice.
    if _PUBLISHED_BY.match(rest):
        m = _PUBLISHER_END.search(rest[:_NOTICE_MAX])
        if m is not None:
            rest = rest[m.end():]
    return rest


def clean_abstract(text: str | None, *, copyright: str | None = None) -> str | None:
    """`text` without entities, a leading heading or a copyright notice;
    None when nothing is left.

    `copyright` is the source's own notice string where it offers one
    (Scopus does, as `AbstractRetrieval.copyright`); it is removed
    exactly when it opens or closes the text, before any pattern is
    tried.
    """
    if text is None:
        return None
    text = _unescape(str(text))
    text = _HEADING.sub("", text, count=1)
    if copyright:
        text = _strip_hint(text, _unescape(copyright))
    text = _strip_leading_notice(text)
    text = _TRAILING_NOTICE.sub("", text)
    # A notice can sit in front of the heading ("© 2020 …. Abstract …").
    text = _HEADING.sub("", text, count=1)
    text = " ".join(text.split())
    return text or None
