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

#: Where a leading notice starts. The year is optional: "© Academy of
#: Management Annals.As research…" has none.
_NOTICE_START = re.compile(
    r"^\s*(?:Copyright\s*)?(?:©|\(c\)|Copyright\s*\d{4})", re.IGNORECASE,
)

#: A year fused straight onto the first word: "© 2018A variable…".
_FUSED_YEAR = re.compile(r"^\s*©\s*\d{4}(?=[A-Z])")

#: How far into the text a leading notice may run. Real ones are well
#: under 250 characters; the cap stops a match from reaching into the
#: abstract's own sentences.
_NOTICE_MAX = 250

#: The unambiguous ends of a leading notice, tried in this order.
_ENDS = (
    re.compile(r"All rights reserved\.", re.IGNORECASE),
    # "…Springer Nature.Given…", "© 2014.If…": a sentence end with no
    # space before a capital, which ordinary prose never has. A
    # lower-case letter or digit before the dot keeps "B.V" out.
    re.compile(r"[a-z0-9)]\.(?=[A-Z])"),
)

#: A publisher-name ending followed by the abstract's first word, with
#: or without the space: "…Elsevier Ltd We…", "…Elsevier LtdCOVID-19…",
#: "…Elsevier B.V.This paper…". "B.V." always closes a publisher name.
_PUBLISHER_END = re.compile(
    r"(?:\b(?:Ltd|Inc|LLC|GmbH|Nature|Sons|Society|Association|Press|"
    r"Publishing|Publications|Group|Authors?)\b\.?(?=\s+[A-Z])"
    r"|(?:Ltd|Inc|LLC|GmbH|Authors)\.?(?=[A-Z])"
    r"|\bB\.V\.(?=\s*[A-Z]))"
)

#: The second half of a two-part notice.
_PUBLISHED_BY = re.compile(r"^\s*Published by\b")

#: A trailing notice: from a notice that follows a sentence end, to the
#: end of the text. Covers "© 2006 …", "Copyright (C) 2001 …", the
#: mis-encoded "Copyright ? 2006 …", "Crown Copyright …", a doubled
#: "© 2013 © 2013 …", and EBSCO's "ABSTRACT FROM AUTHOR Copyright of …
#: is the property of …" boilerplate (~500 characters, hence the cap).
#: "Copyright" alone must be followed by a symbol, a year or "of", so
#: "…Copyright infringement is common." is prose and stays.
_TRAILING_NOTICE = re.compile(
    r"(?<=[.!?)\]])\s*"
    r"(?:\[?ABSTRACT FROM (?:AUTHOR|PUBLISHER)\]?\s*)?"
    r"(?:Crown\s+)?"
    r"(?:©|\([cC]\)|(?i:Copyright)\s*(?:©|\([cC]\)|\S?\s*(?=\d{4})|(?=of\b)))"
    r"(?s:.){0,700}$"
)

#: A short "© <year> <holder>." closing the text with no sentence end
#: before it ("…outside of work hours © 2014 American Psychological
#: Association."). The year and the short, dot-free holder are what make
#: it safe without the sentence-end anchor.
_BARE_TRAILING_NOTICE = re.compile(
    r"\s+(?:(?i:Copyright)\s*)?©\s*\d{4}[^©.]{0,120}\.?\s*$"
)

#: Harvard Business Publishing's closing line.
_HBP_TAIL = re.compile(r"\s*\(Copyright applies to all Abstracts\.?\)\s*$")

#: Markup some sources send entity-escaped, which unescaping reveals:
#: HTML comments (Word's `<!--[if gte mso 9]>…<![endif]-->`) and a
#: closed list of tag names. Not a general `<…>` strip, which would eat
#: "firms with <50 employees … >".
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_TAGS = re.compile(
    r"</?(?:p|span|div|br|i|b|em|strong|sup|sub|u|small|font|xml|"
    r"o:[A-Za-z]+|w:[A-Za-z]+|jats:[a-z-]+|mml:[a-z]+)\b[^<>]*>",
)

#: A leading "Abstract" heading, fused or not. Only when the next word
#: is capitalised, so "Abstract reasoning predicts…" survives.
#: Lower-case and plural forms occur too ("abstractScholars…",
#: "Abstracts Research Summary…"); a sentence never opens with a
#: lower-case "abstract", so those are safe.
_HEADING = re.compile(
    r"^\s*(?:ABSTRACTS?|Abstracts?|abstracts?)\s*[:.\-–—]?\s*(?=[A-Z0-9])"
)


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
    text = _TAGS.sub(" ", _COMMENT.sub(" ", text))
    text = _HEADING.sub("", text, count=1)
    if copyright:
        text = _strip_hint(text, _unescape(copyright))
    text = _FUSED_YEAR.sub("", text, count=1)
    text = _strip_leading_notice(text)
    text = _HBP_TAIL.sub("", text)
    text = _TRAILING_NOTICE.sub("", text)
    text = _BARE_TRAILING_NOTICE.sub("", text)
    # A notice can sit in front of the heading ("© 2020 …. Abstract …").
    text = _HEADING.sub("", text, count=1)
    text = " ".join(text.split())
    return text or None
