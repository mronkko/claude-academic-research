"""OpenAlex REST API search.

Runs two block queries (Block A terms, Block B terms) separately and
merges, because OpenAlex's `search=` parameter is relevance-ranked —
a combined A+B query loses recall on papers that match one block
strongly and the other weakly. Free tier; no API key required.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from urllib.parse import quote

import http_client

from .base import DISCOVERY_CITATION, SearchContext, SearchSource, empty_row

OPENALEX_API = "https://api.openalex.org"
PER_PAGE = 200           # OpenAlex max
RATE_LIMIT_SLEEP = 0.2   # polite pool delay between requests


def _filter_safe(term: str) -> str:
    """A search term made safe to sit inside an OpenAlex `filter=` value.

    Commas separate filters and `|` separates alternatives, so either
    inside a term cut the expression short (a 400, or a silently
    different query). A double quote would close the phrase early.
    """
    return " ".join(
        str(term).replace(",", " ").replace("|", " ").replace('"', " ").split()
    )


def _page_by_cursor(
    fetch: Callable[[str], dict],
) -> tuple[list[dict], int | None]:
    """Collect every result of a cursor-paged OpenAlex query.

    Returns the works and `meta.count` from the first page (None when
    the API did not report one).
    """
    works: list[dict] = []
    total: int | None = None
    cursor = "*"
    while cursor:
        data = fetch(cursor)
        if total is None:
            count = (data.get("meta") or {}).get("count")
            total = count if isinstance(count, int) else None
        results = data.get("results", [])
        if not results:
            break
        works.extend(results)
        cursor = (data.get("meta") or {}).get("next_cursor") or ""
        if cursor:
            time.sleep(RATE_LIMIT_SLEEP)
    return works, total


def _count_line(fetched: int, total: int | None, noun: str) -> str:
    """`312 results (API total: 312)`, flagged when the two disagree."""
    if total is None:
        return f"{fetched} {noun}"
    line = f"{fetched} {noun} (API total: {total})"
    if fetched < total:
        line += (f"  WARNING: {total - fetched} fewer than OpenAlex reported "
                 f"— the result set changed or paging stopped early")
    return line

#: OpenAlex work `type` → Crossref type vocabulary (see
#: `base.CROSSREF_TYPES`). OpenAlex renamed `journal-article` to plain
#: `article` in 2024 while keeping the rest of Crossref's spelling, so
#: most of this table is identity and the entries that matter are the
#: two renames and the editorial/letter/erratum family, which are
#: journal articles wherever they are indexed. Anything unlisted maps to
#: `""` — an honest "no idea", which sends the record down the
#: identifier-fill path rather than mislabelling it an article.
_OPENALEX_TYPE_TO_CROSSREF = {
    "article": "journal-article",
    "review": "journal-article",
    "editorial": "journal-article",
    "letter": "journal-article",
    "erratum": "journal-article",
    "book-chapter": "book-chapter",
    "book": "book",
    "monograph": "monograph",
    "dissertation": "dissertation",
    "report": "report",
    "preprint": "posted-content",
    "reference-entry": "reference-entry",
}


class OpenAlexSearch(SearchSource):
    name = "openalex"
    supports_journal_scope = True
    supports_block_queries = True
    supports_citation_search = True

    def run(self, config, ctx: SearchContext) -> list[dict]:
        filter_str = self._build_filter(ctx.issns, ctx.from_year, ctx.to_year)

        blocks: list[tuple[str, list[str]]] = []
        if getattr(config, "BLOCK_A_TERMS", None):
            blocks.append(("block_a", config.BLOCK_A_TERMS))
        if getattr(config, "BLOCK_B_TERMS", None):
            blocks.append(("block_b", config.BLOCK_B_TERMS))
        if not blocks:
            return []  # nothing to search

        rows: list[dict] = []
        for label, terms in blocks:
            query = " OR ".join(f'"{t}"' for t in terms)
            block_filter = filter_str
            if ctx.search_fields == "title_abstract":
                # OpenAlex expresses a field-limited search as a filter,
                # not as `search=`, so the terms move out of the query
                # parameter entirely. Left in both places they would be
                # ANDed with themselves across different fields. A comma
                # inside a filter value ends that filter, so each term is
                # made filter-safe first (see `_filter_safe`).
                filter_query = " OR ".join(
                    f'"{_filter_safe(t)}"' for t in terms
                )
                block_filter = (
                    f"{filter_str},title_and_abstract.search:{filter_query}"
                )
                query = ""
            print(f"  OpenAlex {label}: ", end="", flush=True)
            works, total = self._fetch_all(query, block_filter, ctx)
            print(_count_line(len(works), total, "results"), flush=True)
            for w in works:
                rows.append(self._work_to_row(w, label))
        return rows

    def run_citations(self, seeds: list[str], ctx: SearchContext) -> list[dict]:
        """Works citing each seed DOI, via OpenAlex's `cites:` filter."""
        rows: list[dict] = []
        for doi in seeds:
            work_id = self._resolve_work_id(doi, ctx)
            if not work_id:
                print(
                    f"  OpenAlex cites:{doi}: seed not found in OpenAlex "
                    f"— no citing works can be listed for it",
                    flush=True,
                )
                continue
            filter_str = (
                f"cites:{work_id},"
                f"publication_year:{ctx.from_year}-{ctx.to_year},"
                f"type:article"
            )
            # OpenAlex ANDs comma-joined filters, so venue scope rides
            # inside the same query and the out-of-scope citing works are
            # never transferred: measured on one seed, 1670 open-scope
            # against 68 scoped. Guarded on a non-empty ISSN list because
            # `issn:` with no value is a filter OpenAlex rejects, and
            # `--citation-journal-scope on` with no JOURNALS is merely
            # pointless, not a reason to fail a run.
            if ctx.citation_journal_scope and ctx.issns:
                scope = "|".join(i.strip() for i in ctx.issns if i.strip())
                if scope:
                    filter_str += f",primary_location.source.issn:{scope}"
            print(f"  OpenAlex cites:{doi} ({work_id}): ", end="", flush=True)
            works = self._fetch_all_cursor(filter_str, ctx)
            print(f"{len(works)} citing works", flush=True)
            for w in works:
                row = self._work_to_row(w, f"cites:{doi}")
                row["discovery_source"] = DISCOVERY_CITATION
                rows.append(row)
        return rows

    def _resolve_work_id(self, doi: str, ctx: SearchContext) -> str:
        """The short OpenAlex id (`W2075867231`) for a DOI, or "".

        `cites:` takes an OpenAlex work id, not a DOI, so a citation
        search is always two calls: resolve, then page. Only a 404 means
        "OpenAlex does not hold this seed" and yields "" — reported, and
        the run goes on with its other seeds. Any other failure (a 429
        that outlasted the retries, a 403, a 5xx) raises: it used to be
        read as "not found" too, which dropped the seed's whole citation
        stream while the run reported success.

        The DOI is percent-encoded into the path: a SICI DOI's `<>#;`
        would otherwise end the path or start a fragment.
        """
        params: dict = {"select": "id"}
        if ctx.mailto:
            params["mailto"] = ctx.mailto
        bare = doi.strip().lower()
        url = f"{OPENALEX_API}/works/doi:{quote(bare, safe='/')}"
        response = ctx.http().get(url, params=params, timeout=60)
        if response.status_code == 404:
            return ""
        if not response.ok:
            raise RuntimeError(
                f"OpenAlex could not resolve citation seed {bare} "
                f"(HTTP {response.status_code}); its citing works were not "
                f"listed. Re-run once the API answers."
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"OpenAlex returned a non-JSON answer for seed {bare}"
            ) from exc
        return str((data or {}).get("id", "")).rsplit("/", 1)[-1]

    def _fetch_all_cursor(self, filter_str: str,
                          ctx: SearchContext) -> list[dict]:
        """Page a filter-only query (the citation stream) by cursor.

        Page-number paging stops at OpenAlex's 10,000-result ceiling. A
        seminal method paper — the kind a protocol names as a seed
        precisely because everything applying the method cites it — can
        have more citing works than that, and silently returning the
        first 10,000 would understate the search while looking complete.
        """
        works, _total = _page_by_cursor(
            lambda cursor: self._fetch_page_cursor(filter_str, cursor, ctx),
        )
        return works

    def _fetch_page_cursor(self, filter_str: str, cursor: str,
                           ctx: SearchContext) -> dict:
        return self._fetch_page("", filter_str, cursor, ctx)

    def _build_filter(self, issns: list[str], from_year: int,
                      to_year: int) -> str:
        """The keyword stream's journal/year/type filter.

        An empty ISSN list yields no `issn:` clause: `issn:` with no
        value is a filter OpenAlex rejects (HTTP 400), and an empty
        `JOURNALS` means "unscoped", not "fail".
        """
        parts = []
        scope = "|".join(i.strip() for i in issns if i and i.strip())
        if scope:
            parts.append(f"primary_location.source.issn:{scope}")
        parts.append(f"publication_year:{from_year}-{to_year}")
        parts.append("type:article")
        return ",".join(parts)

    def _fetch_all(self, query: str, filter_str: str,
                   ctx: SearchContext) -> tuple[list[dict], int | None]:
        """Every work for a keyword query, and OpenAlex's own total.

        Cursor paging, like the citation stream: `page=` paging stops at
        10,000 results, and the loop used to stop there without a word,
        printing the truncated count as if it were the whole answer. The
        API total is returned so the caller can print it beside the
        number actually fetched.
        """
        return _page_by_cursor(
            lambda cursor: self._fetch_page(query, filter_str, cursor, ctx),
        )

    def _fetch_page(self, query: str, filter_str: str, cursor: str,
                    ctx: SearchContext) -> dict:
        params: dict = {
            "filter": filter_str,
            "cursor": cursor,
            "per_page": PER_PAGE,
            "select": ",".join([
                "id", "doi", "title", "publication_year", "publication_date",
                "cited_by_count", "type", "authorships", "biblio",
                "primary_location", "open_access", "abstract_inverted_index",
            ]),
        }
        # A citation search has no search terms — the `cites:` filter is
        # the whole query. Sending `search=` empty makes OpenAlex
        # relevance-rank against nothing and drops results.
        if query:
            params["search"] = query
        if ctx.mailto:
            params["mailto"] = ctx.mailto
        data = http_client.get_json(
            ctx.http(), f"{OPENALEX_API}/works",
            params=params, timeout=60,
        )
        if data is None:
            raise RuntimeError(
                f"OpenAlex rejected the request at cursor {cursor!r} (or "
                f"its retries were exhausted). Check the filter expression: "
                f"{filter_str}"
            )
        return data

    def _work_to_row(self, w: dict, label: str) -> dict:
        doi = (w.get("doi") or "").replace("https://doi.org/", "")
        primary = w.get("primary_location") or {}
        src = primary.get("source") or {}
        oa = w.get("open_access") or {}
        authorships = w.get("authorships") or []
        authors = "; ".join(
            a.get("author", {}).get("display_name", "")
            for a in authorships
            if a.get("author", {}).get("display_name")
        )
        abstract = self._reconstruct_abstract(w.get("abstract_inverted_index"))
        year_str = str(w.get("publication_year", "") or "")
        biblio = w.get("biblio") or {}

        row = empty_row()
        row.update({
            "db": self.name,
            "query": label,
            "doi": doi,
            "title": w.get("title", "") or "",
            "authors": authors,
            "year": year_str,
            "source": src.get("display_name", "") or "",
            "issn": src.get("issn_l", "") or "",
            "volume": str(biblio.get("volume") or ""),
            "issue": str(biblio.get("issue") or ""),
            "pages": self._page_range(biblio),
            "type": _OPENALEX_TYPE_TO_CROSSREF.get(
                str(w.get("type") or "").strip().lower(), "",
            ),
            "cited_by": w.get("cited_by_count", 0) or 0,
            "openalex_id": w.get("id", "") or "",
            "abstract": abstract,
            "oa_status": oa.get("oa_status", "") or "",
            "oa_url": oa.get("oa_url", "") or "",
        })
        return row

    def _page_range(self, biblio: dict) -> str:
        """`first_page`/`last_page` as one `123-145` string.

        OpenAlex splits the range into two fields and often knows only
        the first page; Zotero's `pages` takes either shape, so a lone
        first page is emitted alone rather than as `123-`.
        """
        first = str(biblio.get("first_page") or "").strip()
        last = str(biblio.get("last_page") or "").strip()
        if first and last and first != last:
            return f"{first}-{last}"
        return first or last

    def _reconstruct_abstract(self, inverted_index: dict | None) -> str:
        """Rebuild plaintext from OpenAlex inverted index.

        Note: OpenAlex abstracts are often reconstructed from GROBID
        full-text parsing and may contain body-text fragments rather
        than the paper's real abstract. Downstream `enrich_abstracts.py`
        re-fetches proper abstracts from Crossref / Semantic Scholar /
        Scopus; this is a best-effort starting point for the search
        CSV only.
        """
        if not inverted_index:
            return ""
        positions: list[tuple[int, str]] = []
        for word, ps in inverted_index.items():
            for p in ps:
                positions.append((p, word))
        positions.sort()
        return " ".join(w for _, w in positions)
