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

import time

from .base import (
    CREDENTIAL_OPTIONAL,
    DISCOVERY_CITATION,
    SearchContext,
    SearchSource,
    empty_row,
    normalize_journal_title,
    resolve_credential,
)

BULK_ENDPOINT = "https://api.semanticscholar.org/graph/v1/paper/search/bulk"
GRAPH_BASE = "https://api.semanticscholar.org/graph/v1"
PER_PAGE = 1000          # bulk-search max
CITATIONS_PER_PAGE = 1000  # /citations max
#: The `/citations` endpoint refuses `offset + limit > 10000`.
CITATIONS_MAX = 10000
RATE_LIMIT_SLEEP = 0.5   # unauthenticated tier is aggressive

#: Semantic Scholar `publicationTypes` entry → Crossref type vocabulary
#: (see `base.CROSSREF_TYPES`). S2 attaches several types to one paper
#: ("JournalArticle", "Review"); the first that maps wins. The search
#: itself filters to JournalArticle, so this table mostly matters for
#: papers S2 double-labels.
_S2_TYPE_TO_CROSSREF = {
    "journalarticle": "journal-article",
    "review": "journal-article",
    "editorial": "journal-article",
    "lettersandcomments": "journal-article",
    "conference": "proceedings-article",
    "book": "book",
    "booksection": "book-chapter",
}


class SemanticScholarSearch(SearchSource):
    name = "semantic_scholar"
    supports_journal_scope = False   # no reliable API-level ISSN filter
    supports_block_queries = True
    supports_citation_search = True

    def __init__(self) -> None:
        # Set once a 403 proves SEMANTIC_SCHOLAR_API_KEY is being rejected,
        # so the second block query (run() calls _fetch_all twice) doesn't
        # re-send the dead key and re-print the warning.
        self._key_rejected = False

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

        # Optional: a key lifts the rate limit, but anonymous calls work.
        api_key, _ = resolve_credential(
            "SEMANTIC_SCHOLAR_API_KEY", mode=CREDENTIAL_OPTIONAL,
        )
        if not api_key:
            # Said before the first request, not after the first stall.
            # The unauthenticated tier is 1 rps shared across every
            # anonymous caller on the internet, so a bulk search
            # routinely spends minutes inside the retry policy's
            # backoff; `http_client.VerboseRetry` narrates each wait,
            # and this explains why they are happening.
            print(
                "  NOTE: no SEMANTIC_SCHOLAR_API_KEY — using the shared "
                "unauthenticated tier (~1 request/second for all anonymous "
                "callers). Expect 429s and multi-minute backoff waits on a "
                "bulk search; that is throttling, not a hang. `/setup` "
                "registers a free key.",
                flush=True,
            )
        rows: list[dict] = []
        for label, terms in blocks:
            # Semantic Scholar bulk-search syntax uses `|` for OR between
            # quoted phrases, `&` for AND, `-` for negation. Escape each
            # term with quotes so phrases stay together.
            query = " | ".join(f'"{t}"' for t in terms)
            print(f"  Semantic Scholar {label}: ", end="", flush=True)
            papers = self._fetch_all(query, ctx, api_key)
            # Client-side scope filter — S2 does not do this server-side.
            kept = [p for p in papers if self._paper_in_scope(p, ctx)]
            print(f"{len(kept)} results (from {len(papers)} unfiltered)",
                  flush=True)
            if papers and not kept:
                # The silence that hid the ISSN bug for a whole release.
                # A scope filter that rejects a non-empty result set is
                # reporting a mismatch between the config and what the
                # API returns; an empty literature looks identical in the
                # count and is a completely different situation.
                print(
                    f"  WARNING: the journal scope filter rejected all "
                    f"{len(papers)} paper(s) this query returned, so this "
                    f"database contributes nothing to the corpus. That is "
                    f"a config/API mismatch, not an empty literature: "
                    f"Semantic Scholar returns no ISSN, so scope is "
                    f"matched on JOURNALS titles, and a title that does "
                    f"not match what S2 calls the journal drops every "
                    f"paper in it. Check a few titles against the `source` "
                    f"column of a run with no journal scope.",
                    flush=True,
                )
            for paper in kept:
                rows.append(self._paper_to_row(paper, label))
        return rows

    def run_citations(self, seeds: list[str], ctx: SearchContext) -> list[dict]:
        """Papers citing each seed DOI, via the Graph API `/citations` endpoint.

        The endpoint takes no year or type filter, so both are applied
        client-side. It also refuses `offset + limit > 10000`; a seed
        past that ceiling is reported rather than silently truncated,
        because a citation stream that quietly returns its first 10,000
        hits looks exactly like one that returned everything.
        """
        api_key, _ = resolve_credential(
            "SEMANTIC_SCHOLAR_API_KEY", mode=CREDENTIAL_OPTIONAL,
        )
        rows: list[dict] = []
        for doi in seeds:
            print(f"  Semantic Scholar cites:{doi}: ", end="", flush=True)
            papers = self._fetch_citations(doi, ctx, api_key)
            kept = [p for p in papers if self._in_year_window(p, ctx)]
            print(
                f"{len(kept)} citing works "
                f"(from {len(papers)} before the year filter)",
                flush=True,
            )
            for paper in kept:
                row = self._paper_to_row(paper, f"cites:{doi}")
                row["discovery_source"] = DISCOVERY_CITATION
                rows.append(row)
        return rows

    def _in_year_window(self, paper: dict, ctx: SearchContext) -> bool:
        """Year bounds, applied here because `/citations` has no filter.

        A paper with no year is kept. S2 leaves `year` null on records it
        has not fully resolved, and dropping those would silently narrow
        the stream on a metadata gap rather than on the protocol's dates.
        """
        year = paper.get("year")
        if year is None:
            return True
        try:
            return ctx.from_year <= int(year) <= ctx.to_year
        except (TypeError, ValueError):
            return True

    def _fetch_citations(self, doi: str, ctx: SearchContext,
                         api_key: str) -> list[dict]:
        headers: dict = {}
        if api_key and not self._key_rejected:
            headers["x-api-key"] = api_key
        url = f"{GRAPH_BASE}/paper/DOI:{doi.strip()}/citations"
        papers: list[dict] = []
        offset = 0
        while True:
            limit = min(CITATIONS_PER_PAGE, CITATIONS_MAX - offset)
            if limit <= 0:
                print(
                    f"\n    WARNING: stopped at the endpoint's "
                    f"{CITATIONS_MAX}-record ceiling for {doi}; this seed "
                    f"has more citing works than /citations will return. "
                    f"Run the same seed through OpenAlex, which pages past "
                    f"it with a cursor.",
                    flush=True,
                )
                break
            params = {
                "offset": offset,
                "limit": limit,
                "fields": ",".join([
                    "title", "abstract", "year", "venue", "authors",
                    "externalIds", "citationCount", "openAccessPdf",
                    "journal", "publicationTypes",
                ]),
            }
            resp = ctx.http().get(
                url, params=params, headers=headers, timeout=60,
            )
            if resp.status_code == 404:
                print("seed not indexed by Semantic Scholar — ", end="",
                      flush=True)
                break
            if resp.status_code == 403 and headers.get("x-api-key"):
                # Same fallback the bulk endpoint makes: a 403 with a key
                # attached means the key is dead, not that the call is
                # disallowed. Warn once, continue unauthenticated.
                print(
                    "\n  WARNING: SEMANTIC_SCHOLAR_API_KEY was rejected "
                    "(403 Forbidden). Continuing unauthenticated.",
                    flush=True,
                )
                self._key_rejected = True
                headers.pop("x-api-key", None)
                continue
            if resp.status_code == 429:
                raise RuntimeError(
                    "Semantic Scholar citation search stayed rate-limited "
                    "after retries. Re-run in a few minutes, or drop this "
                    "source from --databases and use OpenAlex for the "
                    "citation stream."
                )
            resp.raise_for_status()
            data = resp.json()
            batch = data.get("data") or []
            # Each entry wraps the citing paper: {"citingPaper": {...}}.
            papers.extend(
                entry.get("citingPaper") or {}
                for entry in batch if entry.get("citingPaper")
            )
            if len(batch) < limit:
                break
            offset += len(batch)
            time.sleep(RATE_LIMIT_SLEEP)
        return papers

    def _paper_in_scope(self, paper: dict, ctx: SearchContext) -> bool:
        """Is this paper inside the protocol's journal scope?

        S2 cannot filter by venue server-side, so scope is enforced here
        or not at all. It used to be enforced on ISSN alone, and S2
        returns no ISSN: not on the `journal` object, and not in
        `externalIds`, whose live keys are MAG, DOI, CorpusId, PubMed,
        PubMedCentral, DBLP and ArXiv. Every paper therefore failed, and
        this source contributed zero rows to every run that set
        `JOURNALS` — reported as a count of 0, indistinguishable from a
        query that found nothing.

        Title matching is what replaces it, against the titles `JOURNALS`
        already declares. ISSN is still checked first: it costs nothing,
        it is the stronger signal, and S2 may populate it one day.

        An unscoped context keeps everything — the citation stream passes
        no scope, deliberately.
        """
        issn_set = {i.strip() for i in ctx.issns if i.strip()}
        title_keys = ctx.journal_title_keys()
        if not issn_set and not title_keys:
            return True

        external = paper.get("externalIds") or {}
        candidates: list[str] = []
        for field in ("ISSN", "ISSNs"):
            val = external.get(field)
            if isinstance(val, str):
                candidates.append(val.strip())
            elif isinstance(val, list):
                candidates.extend(str(v).strip() for v in val)
        if any(c in issn_set for c in candidates):
            return True

        if not title_keys:
            # Caller declared an ISSN scope and no titles. Honour it as
            # given rather than silently becoming unscoped.
            return False
        journal = paper.get("journal") or {}
        return normalize_journal_title(journal.get("name")) in title_keys

    def _fetch_all(self, query: str, ctx: SearchContext,
                   api_key: str) -> list[dict]:
        headers: dict = {}
        if api_key and not self._key_rejected:
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
                    "openAccessPdf", "journal", "publicationTypes",
                ]),
            }
            if token:
                params["token"] = token
            # Straight through the shared session rather than
            # `http_client.get_json`, which collapses every 4xx to None —
            # the 403 below has to be told apart from the rest. The
            # session still carries the urllib3.Retry adapter, so 429 and
            # 5xx are retried with exponential backoff and Retry-After
            # honoured, bounded at 5 attempts. That is what replaced the
            # unbounded `sleep(5); continue` loop this endpoint used to
            # spin in against the throttled unauthenticated tier.
            resp = ctx.http().get(
                BULK_ENDPOINT, params=params, headers=headers, timeout=60,
            )
            if resp.status_code == 403 and headers.get("x-api-key"):
                # A 403 on this endpoint with a key attached means the key
                # itself is invalid/revoked (anonymous calls to the same
                # endpoint succeed) — not a scope/plan restriction. Warn
                # once and fall back to unauthenticated rather than
                # failing the whole search.
                print(
                    "  WARNING: SEMANTIC_SCHOLAR_API_KEY was rejected (403 "
                    "Forbidden) by the Semantic Scholar API — the key "
                    "appears invalid or revoked. Continuing this search "
                    "unauthenticated (lower, shared rate limit applies). "
                    "Rotate the key via `/setup` when convenient.",
                    flush=True,
                )
                self._key_rejected = True
                headers.pop("x-api-key", None)
                continue
            if resp.status_code == 429:
                # The adapter already retried this to exhaustion; spinning
                # here would re-create the unbounded loop.
                #
                # Which advice is right depends on what this request
                # actually carried. Telling someone to set the key they
                # already set — the message this used to print
                # unconditionally — sends them to /setup to rotate a
                # working credential while the real answer is to wait.
                if headers.get("x-api-key"):
                    raise RuntimeError(
                        "Semantic Scholar bulk search stayed rate-limited "
                        "after retries, with SEMANTIC_SCHOLAR_API_KEY "
                        "attached and accepted. The key is not the problem: "
                        "the bulk endpoint throttles per key, and a large "
                        "paginated query can exhaust it on its own. Re-run "
                        "in a few minutes, or narrow BLOCK_A_TERMS / "
                        "BLOCK_B_TERMS so fewer pages are needed."
                    )
                raise RuntimeError(
                    "Semantic Scholar bulk search stayed rate-limited after "
                    "retries on the shared unauthenticated tier. Set "
                    "SEMANTIC_SCHOLAR_API_KEY (see `/setup`) to get a "
                    "per-key rate limit."
                )
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
            # S2's `journal` sub-object carries volume and pages but no
            # issue — the one gap among the four databases.
            "volume": str(journal.get("volume", "") or "").strip(),
            "issue": "",
            "pages": str(journal.get("pages", "") or "").strip(),
            "type": self._crossref_type(paper),
            "cited_by": paper.get("citationCount", 0) or 0,
            "s2_paper_id": paper.get("paperId", "") or "",
            "abstract": paper.get("abstract", "") or "",
            "oa_status": oa.get("license", "") if oa else "",
            "oa_url": oa.get("url", "") if oa else "",
        })
        return row

    def _crossref_type(self, paper: dict) -> str:
        for pt in paper.get("publicationTypes") or []:
            mapped = _S2_TYPE_TO_CROSSREF.get(str(pt).strip().lower(), "")
            if mapped:
                return mapped
        return ""
