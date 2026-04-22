"""Shared helpers for title-based fallback in abstract fetchers.

Multiple sources (WoS, Semantic Scholar) fall back to a title search
when a DOI lookup misses. They need to agree on how titles are
normalised before comparison — otherwise a candidate that differs only
in embedded HTML (`<i>`...`</i>` is common in WoS / Crossref records),
subtitle truncation, or whitespace wouldn't match even when it should.
"""

from __future__ import annotations

import re

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
