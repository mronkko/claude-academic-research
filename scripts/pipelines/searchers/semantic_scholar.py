"""Semantic Scholar search via the Graph API's bulk-search endpoint.

Same block-query pattern as the OpenAlex source: run `BLOCK_A_TERMS`
and `BLOCK_B_TERMS` separately and merge. Semantic Scholar does NOT
reliably filter results by ISSN at the API level, so the source
post-filters client-side against `ctx.issns` — this is noisier than
Scopus / WoS, and Semantic Scholar is best used as a complementary
signal rather than the primary search database.

SEMANTIC_SCHOLAR_API_KEY is optional. Free unauthenticated requests
work but at a much lower rate limit (1 rps shared across all
unauthenticated callers). An API key moves you into the per-user
higher tier and is strongly recommended for systematic searches.
"""

from __future__ import annotations

import os
import time

import requests

from .base import SearchContext, SearchSource, empty_row

BULK_ENDPOINT = "https://api.semanticscholar.org/graph/v1/paper/search/bulk"
PER_PAGE = 1000          # bulk-search max
RATE_LIMIT_SLEEP = 0.5   # unauthenticated tier is aggressive


class SemanticScholarSearch(SearchSource):
    name = "semantic_scholar"
    supports_journal_scope = False   # no reliable API-level ISSN filter
    supports_block_queries = True

    def credentials_error(self, ctx: SearchContext) -> str | None:
        # Free tier works; key only recommended. Never a hard error.
        return None

    def run(self, config, ctx: SearchContext) -> list[dict]:
        blocks: list[tuple[str, list[str]]] = []
        if getattr(config, "BLOCK_A_TERMS", None):
            blocks.append(("block_a", config.BLOCK_A_TERMS))
        if getattr(config, "BLOCK_B_TERMS", None):
            blocks.append(("block_b", config.BLOCK_B_TERMS))
        if not blocks:
            return []

        api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "")
        issn_set = {i.strip() for i in ctx.issns if i.strip()}

        rows: list[dict] = []
        for label, terms in blocks:
            # Semantic Scholar bulk-search syntax uses `|` for OR between
            # quoted phrases, `&` for AND, `-` for negation. Escape each
            # term with quotes so phrases stay together.
            query = " | ".join(f'"{t}"' for t in terms)
            print(f"  Semantic Scholar {label}: ", end="", flush=True)
            papers = self._fetch_all(query, ctx, api_key)
            # Client-side ISSN filter — S2 does not do this server-side.
            kept = [p for p in papers
                    if self._paper_matches_issn(p, issn_set)]
            print(f"{len(kept)} results (from {len(papers)} unfiltered)",
                  flush=True)
            for paper in kept:
                rows.append(self._paper_to_row(paper, label))
        return rows

    def _paper_matches_issn(self, paper: dict, issn_set: set[str]) -> bool:
        if not issn_set:
            return True
        journal = paper.get("journal") or {}
        external = paper.get("externalIds") or {}
        candidates: list[str] = []
        if isinstance(journal.get("name"), str):
            # S2 doesn't expose ISSN on the journal field; fall back to
            # external ids where possible.
            pass
        for field in ("ISSN", "ISSNs"):
            val = external.get(field)
            if isinstance(val, str):
                candidates.append(val.strip())
            elif isinstance(val, list):
                candidates.extend(str(v).strip() for v in val)
        return any(c in issn_set for c in candidates)

    def _fetch_all(self, query: str, ctx: SearchContext,
                   api_key: str) -> list[dict]:
        headers: dict = {}
        if api_key:
            headers["x-api-key"] = api_key
        papers: list[dict] = []
        token: str | None = None
        while True:
            params: dict = {
                "query": query,
                "year": f"{ctx.from_year}-{ctx.to_year}",
                "publicationTypes": "JournalArticle",
                "fields": ",".join([
                    "title", "abstract", "year", "venue",
                    "authors", "externalIds", "citationCount",
                    "openAccessPdf", "journal",
                ]),
            }
            if token:
                params["token"] = token
            resp = requests.get(BULK_ENDPOINT, params=params,
                                headers=headers, timeout=60)
            if resp.status_code == 429:
                # Unauthenticated tier throttles aggressively; back off.
                time.sleep(5)
                continue
            resp.raise_for_status()
            data = resp.json()
            batch = data.get("data") or []
            papers.extend(batch)
            token = data.get("token")
            if not token or not batch:
                break
            time.sleep(RATE_LIMIT_SLEEP)
            # The bulk endpoint caps at 1000 pages of 1000 rows; stop
            # well before that for sanity on an exploratory search.
            if len(papers) >= 10000:
                break
        return papers

    def _paper_to_row(self, paper: dict, label: str) -> dict:
        external = paper.get("externalIds") or {}
        doi = (external.get("DOI") or "").strip().lower()
        authors_list = paper.get("authors") or []
        authors = "; ".join(a.get("name", "") for a in authors_list
                            if a.get("name"))
        journal = paper.get("journal") or {}
        oa = paper.get("openAccessPdf") or {}

        row = empty_row()
        row.update({
            "db": self.name,
            "query": label,
            "doi": doi,
            "title": paper.get("title", "") or "",
            "authors": authors,
            "year": str(paper.get("year", "") or ""),
            "source": journal.get("name", "") or paper.get("venue", "") or "",
            "issn": "",  # not reliably exposed
            "cited_by": paper.get("citationCount", 0) or 0,
            "s2_paper_id": paper.get("paperId", "") or "",
            "abstract": paper.get("abstract", "") or "",
            "oa_status": oa.get("license", "") if oa else "",
            "oa_url": oa.get("url", "") if oa else "",
        })
        return row
