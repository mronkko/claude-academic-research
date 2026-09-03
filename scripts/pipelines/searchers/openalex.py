"""OpenAlex REST API search.

Runs two block queries (Block A terms, Block B terms) separately and
merges, because OpenAlex's `search=` parameter is relevance-ranked —
a combined A+B query loses recall on papers that match one block
strongly and the other weakly. Free tier; no API key required.
"""

from __future__ import annotations

import time

import http_client

from .base import DISCOVERY_CITATION, SearchContext, SearchSource, empty_row

PER_PAGE = 200           # OpenAlex max
RATE_LIMIT_SLEEP = 0.2   # polite pool delay between requests

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
            print(f"  OpenAlex {label}: ", end="", flush=True)
            works = self._fetch_all(query, filter_str, ctx)
            print(f"{len(works)} results", flush=True)
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
        search is always two calls: resolve, then page. A seed that
        OpenAlex does not hold yields "" and is reported rather than
        raising — one unresolvable seed should not sink a run that has
        other seeds and a whole keyword stream behind it.
        """
        params: dict = {"select": "id"}
        if ctx.mailto:
            params["mailto"] = ctx.mailto
        data = http_client.get_json(
            ctx.http(),
            f"https://api.openalex.org/works/doi:{doi.strip().lower()}",
            params=params, timeout=60,
        )
        if not data:
            return ""
        return str(data.get("id", "")).rsplit("/", 1)[-1]

    def _fetch_all_cursor(self, filter_str: str,
                          ctx: SearchContext) -> list[dict]:
        """Page a filter-only query with a cursor rather than `page=`.

        Page-number paging stops at OpenAlex's 10,000-result ceiling.
        That is a real limit for this stream and not for the keyword one:
        a seminal method paper — the kind a protocol names as a seed
        precisely because everything applying the method cites it — can
        have more citing works than that, and silently returning the
        first 10,000 would understate the search while looking complete.
        """
        all_works: list[dict] = []
        cursor = "*"
        while cursor:
            data = self._fetch_page_cursor(filter_str, cursor, ctx)
            results = data.get("results", [])
            if not results:
                break
            all_works.extend(results)
            cursor = (data.get("meta") or {}).get("next_cursor") or ""
            if cursor:
                time.sleep(RATE_LIMIT_SLEEP)
        return all_works

    def _fetch_page_cursor(self, filter_str: str, cursor: str,
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
        if ctx.mailto:
            params["mailto"] = ctx.mailto
        data = http_client.get_json(
            ctx.http(), "https://api.openalex.org/works",
            params=params, timeout=60,
        )
        if data is None:
            raise RuntimeError(
                f"OpenAlex rejected the citation-search request (or its "
                f"retries were exhausted). Filter: {filter_str}"
            )
        return data

    def _build_filter(self, issns: list[str], from_year: int,
                      to_year: int) -> str:
        return (
            f"primary_location.source.issn:{'|'.join(issns)},"
            f"publication_year:{from_year}-{to_year},"
            f"type:article"
        )

    def _fetch_all(self, query: str, filter_str: str,
                   ctx: SearchContext) -> list[dict]:
        all_works: list[dict] = []
        page = 1
        total: int | None = None
        while True:
            data = self._fetch_page(query, filter_str, page, ctx)
            if total is None:
                total = data["meta"]["count"]
            results = data.get("results", [])
            if not results:
                break
            all_works.extend(results)
            if page * PER_PAGE >= min(total, 10000):
                break
            page += 1
            time.sleep(RATE_LIMIT_SLEEP)
        return all_works

    def _fetch_page(self, query: str, filter_str: str, page: int,
                    ctx: SearchContext) -> dict:
        params: dict = {
            "filter": filter_str,
            "page": page,
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
            ctx.http(), "https://api.openalex.org/works",
            params=params, timeout=60,
        )
        if data is None:
            raise RuntimeError(
                f"OpenAlex rejected the request for page {page} (or its "
                f"retries were exhausted). Check the filter expression: "
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
