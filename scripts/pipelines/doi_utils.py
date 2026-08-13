"""Canonical DOI string handling for the pipeline scripts.

Three modules used to carry their own copy of this logic — `enrich_dois.py`,
`fetchers/doi_resolver.py` (whose comment conceded the prefix list was "kept
in sync rather than imported"), and `import_to_zotero.py` (which reached into
zotero-mcp's private `tools._helpers._normalize_doi`). This module is the one
definition they all import.

**Two functions, not one.** The strict/lenient split is load-bearing:

- :func:`normalize_doi` validates and returns ``None`` for anything that is
  not a well-formed DOI. Right for *identity* — deduplicating library items,
  deciding whether two rows describe the same paper. A malformed string
  should not match anything.
- :func:`strip_doi_prefixes` cleans without judging. Right for *cache keys*
  and for repair paths that need to know something was cleaned. Collapsing
  every non-conforming DOI to ``""`` would make them all collide on one
  cache entry, and would leave `enrich_dois --fix-malformed` with nothing to
  write back.

Stdlib-only, and it must stay that way: `doi_resolver.py` sits below the
orchestrators, and `enrich_dois.py` does not declare `zotero-mcp-server` in
its PEP 723 dependency block.

Attribution: :func:`normalize_doi` follows the semantics of zotero-mcp's
``_normalize_doi`` (MIT-licensed, ``src/zotero_mcp/tools/_helpers.py``) —
prefix stripping, trailing-punctuation stripping, and the same DOI shape
regex — reimplemented locally so the pipeline does not depend on a private
symbol of a private module.
"""

from __future__ import annotations

import re

#: URL / scheme prefixes that show up wrapped around DOIs in search-database
#: exports and in Zotero's own DOI field.
DOI_PREFIX_STRIPS: tuple[str, ...] = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
    "doi:",
)

#: A well-formed DOI: registrant prefix `10.NNNN` then a non-empty suffix.
DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")

_DOI_IN_URL_RE = re.compile(r"doi\.org/(10\.\d{4,9}/[^\s?#]+)", flags=re.IGNORECASE)

#: Punctuation that trails a DOI when it has been copied out of prose or a
#: reference list ("... (doi:10.1234/abc).").
_TRAILING_PUNCT = ".,);]"


def strip_doi_prefixes(raw: str) -> tuple[str, bool]:
    """Strip URL / ``doi:`` prefixes and surrounding whitespace.

    Returns ``(clean, changed)``. ``changed`` is True whenever anything was
    removed — a prefix, leading whitespace, or trailing whitespace — which is
    what `enrich_dois --fix-malformed` uses to decide whether to PATCH the
    Zotero field back to canonical form.

    Lenient by design: a string that is not a valid DOI comes back cleaned
    rather than rejected. Use :func:`normalize_doi` when validity matters.
    """
    original = raw or ""
    s = original.strip()
    for prefix in DOI_PREFIX_STRIPS:
        if s.lower().startswith(prefix.lower()):
            s = s[len(prefix):].strip()
            break
    return s, s != original


def normalize_doi(raw: str | None) -> str | None:
    """Return the canonical bare DOI, or ``None`` if `raw` is not one.

    Handles ``doi:`` prefixes, ``http(s)://(dx.)doi.org/`` URLs, and trailing
    punctuation picked up from prose. Anything that does not end up matching
    :data:`DOI_RE` returns ``None`` — including empty input.
    """
    if not raw:
        return None
    s = str(raw).strip()
    if s.lower().startswith("doi:"):
        s = s[len("doi:"):].strip()
    if s.lower().startswith(("http://", "https://")):
        match = _DOI_IN_URL_RE.search(s)
        if not match:
            return None
        s = match.group(1)
    s = s.rstrip(_TRAILING_PUNCT)
    return s if DOI_RE.match(s) else None


def doi_key(raw: str | None) -> str:
    """Case-insensitive identity key for a DOI, or ``""`` when invalid.

    ``""`` is falsy on purpose so callers can keep the
    ``if doi and doi in doi_map`` shape: an unparseable DOI must not match
    another unparseable DOI.
    """
    return (normalize_doi(raw) or "").lower()


def doi_cache_key(raw: str) -> str:
    """Lenient lowercase key for on-disk caches.

    Unlike :func:`doi_key` this never discards input, so two *different*
    non-conforming DOIs keep distinct cache entries instead of colliding on
    ``""`` and serving each other's cached lookups.
    """
    return strip_doi_prefixes(raw)[0].lower() if raw else ""
