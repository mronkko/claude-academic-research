"""Shared helpers for title-based fallback in abstract fetchers.

Multiple sources (WoS, Semantic Scholar) fall back to a title search
when a DOI lookup misses. They need to agree on how titles are
normalised before comparison — otherwise a candidate that differs only
in embedded HTML (`<i>`...`</i>` is common in WoS / Crossref records),
subtitle truncation, or whitespace wouldn't match even when it should.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Strip all `<tag>` and `</tag>` fragments including JATS-style self-closers.
_HTML_TAG_RE = re.compile(r"<[^>]+>")

# Anything that isn't ASCII a-z or 0-9, after lowercasing.
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def strip_html(text: str) -> str:
    """Remove `<tag>` wrappers — NOT full HTML parsing.

    Good enough for publisher title fields, which use a narrow set of
    styling tags (<i>, <sub>, <sup>, <b>, occasionally namespaced
    variants). Bad HTML returns gibberish but not an exception.
    """
    return _HTML_TAG_RE.sub(" ", text or "")


def normalise(title: str, *, max_chars: int = 80) -> str:
    """Reduce a title to a stable comparison key.

    Lowercases, strips HTML tags, replaces all runs of non-alphanumerics
    with nothing, then truncates. Two titles that differ only in
    formatting collapse to the same key; two titles with different
    content diverge within 80 chars.
    """
    cleaned = strip_html(title).lower()
    alnum = _NON_ALNUM_RE.sub("", cleaned)
    return alnum[:max_chars]


def matches(candidate: str, target: str, *, max_chars: int = 80) -> bool:
    """True when `candidate` is plausibly the same paper as `target`.

    Uses normalised-prefix equality: they agree on the first `max_chars`
    alphanumerics after HTML stripping. A lenient match — good for a
    fallback path that's already narrowed by the search engine to a
    small candidate set (≤5 hits).
    """
    a = normalise(candidate, max_chars=max_chars)
    b = normalise(target, max_chars=max_chars)
    if not a or not b:
        return False
    # Prefix equality in both directions — protects against one title
    # being a truncated form of the other.
    return a.startswith(b) or b.startswith(a)


#: Words too common to show that two texts are about the same thing.
_STOPWORDS = frozenset(
    "about after also among and are because been being between both but "
    "does during each from have into more most much only other over same "
    "some such than that their them then there these they this those "
    "through under upon very what when where which while with within "
    "without would your".split()
)


def content_words(text: str) -> set[str]:
    """Lower-cased words of four letters or more, stopwords out."""
    words = re.findall(r"[^\W\d_]{4,}", strip_html(text).lower())
    return {w for w in words if w not in _STOPWORDS}


def fold(text: str) -> str:
    """Lower-case ASCII letters and digits only: "Röth" -> "roth"."""
    flat = unicodedata.normalize("NFKD", text or "")
    flat = "".join(c for c in flat if not unicodedata.combining(c))
    return _NON_ALNUM_RE.sub("", flat.lower())


@dataclass(frozen=True)
class ItemMeta:
    """What a title-fallback hit is checked against: the Zotero item's
    year, creator surnames (folded) and venue (folded)."""

    year: int | None = None
    surnames: frozenset[str] = frozenset()
    venue: str = ""


_YEAR_RE = re.compile(r"\b(1[5-9]\d\d|20\d\d)\b")


def item_meta(data: dict | None) -> ItemMeta:
    """`ItemMeta` from a Zotero item's `data` dict."""
    data = data or {}
    m = _YEAR_RE.search(str(data.get("date") or ""))
    surnames = frozenset(
        f for c in data.get("creators") or []
        if (f := fold(c.get("lastName") or c.get("name") or ""))
    )
    venue = next(
        (fold(data[k]) for k in ("publicationTitle", "bookTitle",
                                 "proceedingsTitle", "seriesTitle")
         if data.get(k)),
        "",
    )
    return ItemMeta(year=int(m.group(1)) if m else None,
                    surnames=surnames, venue=venue)


def record_agrees(
    meta: ItemMeta, *, year: int | None, surnames: set[str], venue: str,
) -> str | None:
    """Why a title-matched record is not the item, or None if it is.

    Needs the year (within one) and then an author surname or the venue.
    Anything that cannot be checked counts against the record: the
    fallback exists for a DOI the index filed differently, and a paper
    like that has its metadata; one without it is the case that went
    wrong (see `WosSource`).
    """
    if meta.year is None or year is None:
        return "no year to compare"
    if abs(meta.year - year) > 1:
        return f"year {year}, item {meta.year}"
    if meta.surnames & {fold(s) for s in surnames if s}:
        return None
    v = fold(venue)
    if meta.venue and v and (meta.venue in v or v in meta.venue):
        return None
    return "no author or venue in common"
