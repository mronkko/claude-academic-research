"""Web of Science — abstract retrieval via the WoS Expanded or Starter API.

Two-phase lookup strategy:
  1. Query by DOI (`DO=(<doi>)`). Most papers resolve here.
  2. Fallback by title (`TI=(<cleaned title>)`) when DOI lookup misses
     but a title is available. WoS sometimes indexes a paper under a
     different publisher DOI than the one in Zotero — common for AoM
     journals whose DOIs were reissued after publisher transfers
     (e.g. Annals `10.1080/...` in WoS vs `10.5465/...` in the
     library).

Requires `WOS_API_KEY_EXTENDED` (the Expanded tier). **The Starter tier
cannot supply abstracts**: its documents carry citations, identifiers,
keywords, links, names, source, title and types, and no abstract —
checked live on 2026-09-23 (`DO=(10.5465/amd.2015.0052)`, HTTP 200, no
`abstract` field). This module used to fall back to a Starter-only key
and read `hit["abstract"]`, which is never there, so every item came
back None — "WoS answered and has no abstract" — and was counted towards
`not_found`. With only a Starter key the source now reports itself
unavailable instead, which counts for nothing.

The same finding rules out falling back to Starter when the Expanded
key's daily quota runs out, although the two tiers do have separate
quotas.
"""

from __future__ import annotations

import logging
import os
import re

from fetchers._title_match import (
    ItemMeta,
    content_words,
    matches,
    record_agrees,
    strip_html,
)
from fetchers.base import (
    AbstractFetcher,
    AbstractWithheld,
    SourceUnavailable,
    answered,
)

logger = logging.getLogger(__name__)

_EXPANDED_URL = "https://api.clarivate.com/api/wos"
_STARTER_URL = "https://api.clarivate.com/apis/wos-starter/v1/documents"


#: Words WoS reads as operators in an unquoted query.
_OPERATORS = frozenset({"and", "or", "not", "near", "same"})


def _query_title(title: str, *, drop_operators: bool) -> str:
    """`title` as words only, fit for `TI=(…)`.

    WoS answers HTTP 400 to a title carrying punctuation its parser or
    its URL filter rejects: "10% cuts" ("Suspicious content detected in
    URL"), a stray curly quote ("Unclosed quoted string"), or a leading
    "And" / "To Strike or Not To Strike" read as operators ("Missing
    Field Value"). Ten items in one run (2026-09-23) failed that way on
    the title fallback; as words only, all ten queries succeed. The
    candidates are re-checked with `matches()` anyway.
    """
    words = re.findall(r"[^\W_]+", strip_html(title))
    if drop_operators:
        words = [w for w in words if w.lower() not in _OPERATORS]
    return " ".join(words)


#: A title with fewer content words than this is not searched for.
#: "Erratum", "Introduction", "Discrimination", "COMMENTARY", "Time to
#: get tough": `TI=` matches thousands of records, `matches()` accepts
#: any of them whose title merely starts that way, and on 2026-09-23 at
#: least 15 of 152 WoS abstracts in one library were another record's.
_MIN_TITLE_WORDS = 3

#: An item with no metadata: its title fallback can never be verified.
_NO_META = ItemMeta()


def _fallback_title(title: str | None) -> str | None:
    """`title`, or None when it is too generic to search WoS by."""
    if title and len(content_words(title)) >= _MIN_TITLE_WORDS:
        return title
    return None


class WosSource(AbstractFetcher):
    name = "wos"

    def _key_and_tier(self) -> tuple[str, str]:
        """Return (api_key, tier) where tier is 'expanded' or 'starter'."""
        extended = (
            getattr(self.config, "wos_api_key_extended", None)
            or os.environ.get("WOS_API_KEY_EXTENDED", "")
        )
        if extended:
            return extended, "expanded"
        starter = (
            getattr(self.config, "wos_api_key", None)
            or os.environ.get("WOS_API_KEY", "")
        )
        if starter:
            return starter, "starter"
        return "", ""

    def fetch_abstract(
        self, doi: str, *, title=None, cache_dir=None, meta=None,
    ) -> str | None:
        del cache_dir                 # WoS fetchers don't use the cache dir
        key, tier = self._key_and_tier()
        if not key or self.http is None:
            raise SourceUnavailable("no WoS API key configured")
        if tier != "expanded":
            raise SourceUnavailable(
                "only a WoS Starter key is configured, and the Starter API "
                "returns no abstracts; set [wos] expanded_key to use WoS here",
            )
        return self._fetch_expanded(
            doi, _fallback_title(title), key, meta or _NO_META,
        )

    # ------------------------------------------------------------------
    # Expanded tier (richer XML/JSON payload, real abstract element)
    # ------------------------------------------------------------------

    def _fetch_expanded(
        self, doi: str, title: str | None, key: str,
        meta: ItemMeta = _NO_META,
    ) -> str | None:
        headers = {"X-ApiKey": key, "Accept": "application/json"}

        # Phase 1: DOI.
        hits, withheld = self._expanded_query(f"DO=({doi})", headers, count=1)
        text = self._expanded_abstract(hits[0]) if hits else None
        if text:
            return text

        # Phase 2: title fallback.
        if not title:
            if withheld:
                raise AbstractWithheld(
                    "WoS holds the record outside this subscription's "
                    "entitlement",
                )
            return None
        cleaned_title = _query_title(title, drop_operators=True)
        if not cleaned_title:
            return None
        # WoS `TI=(...)` with unquoted tokens does keyword-AND matching,
        # which survives subtitle-length and HTML-tag mismatches between
        # Zotero and WoS.  A quoted phrase would require exact match and
        # silently drops to 0 hits when anything differs (publisher
        # added/removed a subtitle, or has <i> embedded in the stored
        # title).  The shortlist is then re-filtered in Python via
        # `matches()` so false-positive keyword hits don't return the
        # wrong abstract.
        hits, title_withheld = self._expanded_query(
            f"TI=({cleaned_title[:200]})", headers, count=5,
        )
        for rec in hits:
            rec_title = self._expanded_title(rec)
            if not (rec_title and matches(rec_title, title)):
                continue
            why = record_agrees(meta, **self._expanded_facts(rec))
            if why:
                logger.info("wos: title hit for %s refused — %s", doi, why)
                continue
            text = self._expanded_abstract(rec)
            if text:
                return text
        if withheld or title_withheld:
            raise AbstractWithheld(
                "WoS holds a matching record outside this subscription's "
                "entitlement",
            )
        return None

    def _extract_expanded_abstract_from_query(
        self, query: str, headers: dict,
    ) -> str | None:
        hits = self._expanded_search(query, headers, count=1)
        if not hits:
            return None
        return self._expanded_abstract(hits[0])

    def _expanded_search(
        self, query: str, headers: dict, *, count: int,
    ) -> list[dict]:
        return self._expanded_query(query, headers, count=count)[0]

    def _expanded_query(
        self, query: str, headers: dict, *, count: int,
    ) -> tuple[list[dict], bool]:
        """(viewable records, whether a found record was withheld)."""
        # A failed request raises: returning "no hits" logs the item
        # not_found on the strength of a 429 or a timeout. The likely
        # story of 6I9S5R8C, logged not_found by a run on 2026-09-23
        # although WoS holds it and answers "withheld" when asked again.
        resp = self.http.get(
            _EXPANDED_URL,
            headers=headers,
            params={
                "databaseId": "WOK",
                "usrQuery": query,
                "count": count,
                "firstRecord": 1,
            },
            timeout=30,
        )
        if not answered(resp, "wos"):
            return [], False
        data = resp.json() or {}
        found = data.get("QueryResult", {}).get("RecordsFound", 0)
        if found == 0:
            return [], False
        # `RecordsFound` can be positive while `records` is an empty
        # *string*: the record exists but is outside this subscription's
        # entitlement. Seen live for 10.18311/sdmimd/2019/y on both the
        # DOI and the title query; unguarded, `.get` on that string raised
        # AttributeError and every run logged lookup_failed for an
        # answered question. Any non-dict level means "nothing viewable".
        node = data
        for level in ("Data", "Records", "records"):
            node = node.get(level) if isinstance(node, dict) else None
        rec = node.get("REC") if isinstance(node, dict) else None
        if rec is None:
            return [], True
        recs = rec if isinstance(rec, list) else [rec]
        return [r for r in recs if isinstance(r, dict)], False

    @staticmethod
    def _expanded_facts(rec: dict) -> dict:
        """Year, author surnames and source title, for `record_agrees`."""
        summary = rec.get("static_data", {}).get("summary", {})
        year = (summary.get("pub_info") or {}).get("pubyear")
        names = (summary.get("names") or {}).get("name") or []
        if not isinstance(names, list):
            names = [names]
        surnames: set[str] = set()
        for n in names:
            if isinstance(n, dict):
                surnames.add(str(n.get("last_name") or ""))
                surnames.add(str((n.get("preferred_name") or {}).get("last_name") or ""))
                surnames.add(str(n.get("wos_standard") or "").split(",")[0])
        titles = (summary.get("titles") or {}).get("title") or []
        if not isinstance(titles, list):
            titles = [titles]
        venue = next(
            (str(t.get("content", "")) for t in titles
             if isinstance(t, dict) and t.get("type") == "source"),
            "",
        )
        try:
            year = int(year) if year else None
        except (TypeError, ValueError):
            year = None
        return {"year": year, "surnames": surnames, "venue": venue}

    @staticmethod
    def _expanded_title(rec: dict) -> str:
        titles = (
            rec.get("static_data", {})
            .get("summary", {})
            .get("titles", {})
            .get("title", [])
        )
        if not isinstance(titles, list):
            titles = [titles]
        for t in titles:
            if isinstance(t, dict) and t.get("type") == "item":
                return str(t.get("content", ""))
        return ""

    @staticmethod
    def _expanded_abstract(rec: dict) -> str | None:
        block = (
            rec.get("static_data", {})
            .get("fullrecord_metadata", {})
            .get("abstracts", {})
        )
        if not isinstance(block, dict) or block.get("count", 0) == 0:
            return None
        inner = block.get("abstract", {})
        if isinstance(inner, list):
            inner = inner[0] if inner else {}
        text = inner.get("abstract_text", {}).get("p", "") if isinstance(inner, dict) else ""
        if isinstance(text, list):
            text = " ".join(str(p) for p in text)
        text = str(text).strip()
        return text if len(text) > 40 else None
