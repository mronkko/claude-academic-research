"""Scopus search via pybliometrics."""

from __future__ import annotations

import os

from .base import SearchContext, SearchSource, empty_row


class ScopusSearch(SearchSource):
    name = "scopus"
    supports_journal_scope = True
    supports_block_queries = False

    def credentials_error(self, ctx: SearchContext) -> str | None:
        # pybliometrics reads its own config at ~/.config/pybliometrics.cfg.
        # The env var is an optional fallback for some installs. Accept
        # either — init() below will fail clearly if neither is set.
        cfg = os.path.expanduser("~/.config/pybliometrics.cfg")
        if os.path.exists(cfg) or os.environ.get("SCOPUS_API_KEY"):
            return None
        return ("Scopus: neither ~/.config/pybliometrics.cfg nor "
                "SCOPUS_API_KEY is set")

    def run(self, config, ctx: SearchContext) -> list[dict]:
        from pybliometrics import init as pyb_init
        from pybliometrics.scopus import ScopusSearch as PybScopusSearch
        pyb_init()

        rows: list[dict] = []
        for label, scopus_core, _wos_core in config.QUERY_DEFS:
            q = self._full_query(scopus_core, ctx)
            print(f"  Scopus {label}: ", end="", flush=True)
            results = PybScopusSearch(q, download=True).results or []
            print(f"{len(results)} results", flush=True)
            for r in results:
                row = empty_row()
                row.update({
                    "db": self.name,
                    "query": label,
                    "doi": (r.doi or "").strip().lower(),
                    "title": r.title or "",
                    "authors": r.author_names or "",
                    "year": r.coverDate[:4] if r.coverDate else "",
                    "source": r.publicationName or "",
                    "issn": r.issn or "",
                    "cited_by": r.citedby_count or 0,
                    "scopus_id": r.eid or "",
                    "abstract": r.description or "",
                })
                rows.append(row)
        return rows

    def _full_query(self, core: str, ctx: SearchContext) -> str:
        issn_part = " OR ".join(ctx.issns)
        return (
            f"{core} AND ISSN({issn_part}) "
            f"AND PUBYEAR > {ctx.from_year - 1} AND PUBYEAR < {ctx.to_year + 1}"
        )
