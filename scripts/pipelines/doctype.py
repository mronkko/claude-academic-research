"""Recognise records that are not research articles but are typed as one.

Crossref and the databases that copy it register several kinds of record
as `journal-article` that no review wants to screen:

- **Supplements.** APA registers each article's supplemental material as
  a separate work: DOI = the article's DOI + ``.supp``, title
  "Supplemental Material for <article title>", no authors. In one real
  search they were 22% of the *Journal of Applied Psychology* records —
  inflating the corpus and showing up as author-less "articles".
- **Front matter.** "Editorial Board" (Elsevier), "Issue Information"
  (Wiley), "Featured Cover", "Acknowledgment of Ad Hoc Reviewers" (APA),
  "Call for Papers: …" — about 6% of the same search.
- **Notices.** SAGE's "Publication Notice" items are retraction or
  withdrawal notices. They are *about* an article, so they are not front
  matter; they get their own class so nobody mistakes them for
  housekeeping.

The decision (2026-10-15) is to **keep** these records — imported into
Zotero, never dropped — and tag them ``doctype:<class>``. The tag is
global, not review-namespaced: like ``pdf:*`` and ``retracted:flag`` it
states a fact about the item, which is the same for every review sharing
the library. Screening then skips doctype-tagged items and reports how
many, so PRISMA can show them as removed before screening.

Conservative by design. Exact titles are matched exactly (case- and
whitespace-insensitive), and the one prefix rule for front matter only
fires on an author-less record: a call for papers has no byline, while a
real article whose title happens to begin "Call for papers…" does.

Stdlib-only: imported by `search.py`, `import_to_zotero.py`, the
screening orchestrators and `tag_doctypes.py`, whose PEP 723 blocks
differ.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable

SUPPLEMENT = "supplement"
FRONT_MATTER = "front-matter"
NOTICE = "notice"

#: Every class, in reporting order.
DOCTYPES = (SUPPLEMENT, FRONT_MATTER, NOTICE)

#: Global tag family. Deliberately not under `TAG_PREFIX` — see the
#: module docstring.
TAG_FAMILY = "doctype:"

_SUPP_DOI_SUFFIX = ".supp"
_SUPP_TITLE_PREFIX = "supplemental material for "

#: Whole titles that are front matter whoever the publisher. Normalised
#: form (see `_norm_title`). Each one is attested in a real search.
_FRONT_MATTER_TITLES = frozenset({
    "editorial board",
    "issue information",
    "featured cover",
    "acknowledgment of ad hoc reviewers",
    "acknowledgement of ad hoc reviewers",
    "acknowledgments of ad hoc reviewers",
    "acknowledgements of ad hoc reviewers",
    "acknowledgment",
    "acknowledgments",
})

#: Title prefixes that mark front matter on an author-less record only.
_FRONT_MATTER_PREFIXES = ("call for papers", "call for proposals")

_NOTICE_TITLES = frozenset({"publication notice"})

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _norm_title(title: str | None) -> str:
    """Lower-case, HTML-free, single-spaced, without trailing punctuation."""
    t = _TAG_RE.sub("", html.unescape(title or ""))
    t = _WS_RE.sub(" ", t).strip().casefold()
    return t.rstrip(" .:;")


def _norm_doi(doi: str | None) -> str:
    s = (doi or "").strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/",
                   "https://dx.doi.org/", "http://dx.doi.org/", "doi:"):
        if s.startswith(prefix):
            s = s[len(prefix):].strip()
            break
    return s


def is_supplement_doi(doi: str | None) -> bool:
    """True for an APA-style supplement DOI (`<parent>.supp`)."""
    d = _norm_doi(doi)
    return d.endswith(_SUPP_DOI_SUFFIX) and len(d) > len(_SUPP_DOI_SUFFIX)


def parent_doi(doi: str | None) -> str:
    """The parent article's DOI for a supplement DOI, lower-cased; else ""."""
    d = _norm_doi(doi)
    return d[: -len(_SUPP_DOI_SUFFIX)] if is_supplement_doi(d) else ""


def classify(
    title: str | None, doi: str | None = "", *, has_authors: bool = False,
) -> str:
    """The doctype class of a record, or "" for an ordinary one.

    `has_authors` only matters for the front-matter prefix rule; pass it
    whenever the caller knows. Defaulting to False errs towards tagging,
    which is safe for the exact-title rules and is why the prefix rule
    lists only phrases no research article starts with in practice.
    """
    t = _norm_title(title)
    # The prefix carries its trailing space, so a bare "Supplemental
    # material for" with nothing after it does not match.
    if is_supplement_doi(doi) or t.startswith(_SUPP_TITLE_PREFIX):
        return SUPPLEMENT
    if t in _NOTICE_TITLES:
        return NOTICE
    if t in _FRONT_MATTER_TITLES:
        return FRONT_MATTER
    if not has_authors and t.startswith(_FRONT_MATTER_PREFIXES):
        return FRONT_MATTER
    return ""


def tag_for(doctype: str) -> str:
    """`doctype:<class>` — the global Zotero tag recording `doctype`."""
    if doctype not in DOCTYPES:
        raise ValueError(f"unknown doctype {doctype!r}; expected one of {DOCTYPES}")
    return f"{TAG_FAMILY}{doctype}"


def from_tags(tags: Iterable) -> str:
    """The doctype a Zotero tag list records, or "".

    Accepts tag strings or Zotero's `{"tag": ...}` dicts. An unknown
    `doctype:` value still counts — a hand-added `doctype:erratum` is a
    user saying "not a research article", and screening should honour it.
    """
    for t in tags or ():
        tag = t.get("tag", "") if isinstance(t, dict) else str(t)
        if tag.startswith(TAG_FAMILY) and len(tag) > len(TAG_FAMILY):
            return tag[len(TAG_FAMILY):]
    return ""


def classify_zotero_item(item: dict) -> str:
    """`classify` applied to a Zotero item (top-level or its `data`)."""
    d = item.get("data", item)
    return classify(
        d.get("title", ""), d.get("DOI", ""),
        has_authors=bool(d.get("creators")),
    )


def split_items(items: Iterable[dict]) -> tuple[list[dict], dict[str, int]]:
    """`(kept, skipped_counts)`: drop doctype-tagged Zotero items.

    Only the tag decides — an item that merely *looks* like a supplement
    stays in, so tagging remains the one auditable act. Callers can use
    `untagged_lookalikes` to warn about those.
    """
    kept: list[dict] = []
    skipped: dict[str, int] = {}
    for item in items:
        dt = from_tags(item.get("data", {}).get("tags", []))
        if dt:
            skipped[dt] = skipped.get(dt, 0) + 1
        else:
            kept.append(item)
    return kept, skipped


def untagged_lookalikes(items: Iterable[dict]) -> int:
    """How many untagged items `classify` would tag."""
    return sum(
        1 for it in items
        if not from_tags(it.get("data", {}).get("tags", []))
        and classify_zotero_item(it)
    )


def format_counts(counts: dict[str, int]) -> str:
    """`supplement 3, front-matter 2` — known classes first, then others."""
    order = [d for d in DOCTYPES if counts.get(d)] + sorted(
        k for k in counts if k not in DOCTYPES and counts[k]
    )
    return ", ".join(f"{k} {counts[k]}" for k in order)


def supplement_links(
    supplement_dois: Iterable[str], key_by_doi: dict[str, str],
) -> list[tuple[str, str]]:
    """`(supplement_key, parent_key)` for every supplement whose parent is known.

    `key_by_doi` maps lower-cased bare DOIs to Zotero keys — the library
    plus whatever the current run created. A supplement whose article is
    in neither stays tagged and unlinked; nothing is fetched to find it.
    """
    pairs: list[tuple[str, str]] = []
    for doi in supplement_dois:
        d = _norm_doi(doi)
        parent = parent_doi(d)
        if parent and d in key_by_doi and parent in key_by_doi:
            pair = (key_by_doi[d], key_by_doi[parent])
            if pair[0] != pair[1] and pair not in pairs:
                pairs.append(pair)
    return pairs


def link_supplements(
    pairs: list[tuple[str, str]], zot, *, cloud: bool = False,
) -> tuple[int, int]:
    """Relate each supplement and its article, both directions.

    `zot` is a `zotero_io.ZoteroClient` (duck-typed so this module stays
    importable without pyzotero). A failed pair is reported and skipped:
    the tag is already the load-bearing record, and the link is a
    convenience that must not fail an import. Returns `(linked, failed)`.
    """
    linked = failed = 0
    for supp_key, parent_key in pairs:
        try:
            zot.add_related(supp_key, [parent_key], cloud=cloud)
            zot.add_related(parent_key, [supp_key], cloud=cloud)
        except Exception as exc:  # noqa: BLE001 — see docstring
            failed += 1
            print(f"  WARNING: could not relate supplement {supp_key} to "
                  f"article {parent_key}: {exc}", flush=True)
        else:
            linked += 1
    if pairs:
        print(f"  Supplements linked to their article: {linked}"
              + (f" ({failed} failed)" if failed else ""), flush=True)
    return linked, failed
