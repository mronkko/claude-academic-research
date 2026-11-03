"""Scopus search via pybliometrics."""

from __future__ import annotations

import os

from .base import (
    CREDENTIAL_REQUIRED,
    SearchContext,
    SearchSource,
    empty_row,
    resolve_credential,
)

#: Scopus `subtype` code → Crossref type vocabulary (see
#: `base.CROSSREF_TYPES`). Scopus ships a two-letter code alongside the
#: human-readable `subtypeDescription`; the code is the stable half.
#: `re` (review) and `sh` (short survey) are journal articles — they
#: describe the paper's genre, not where it was published. Codes with no
#: entry map to `""` so the record is filled from the DOI rather than
#: guessed at.
_SCOPUS_SUBTYPE_TO_CROSSREF = {
    "ar": "journal-article",
    "re": "journal-article",
    "sh": "journal-article",
    "ed": "journal-article",
    "le": "journal-article",
    "no": "journal-article",
    "cp": "proceedings-article",
    "ch": "book-chapter",
    "bk": "book",
}


class ScopusSearch(SearchSource):
    name = "scopus"
    supports_journal_scope = True
    supports_block_queries = False

    def credentials_error(self, ctx: SearchContext) -> str | None:
        # pybliometrics reads its own config at ~/.config/pybliometrics.cfg.
        # The env var is an optional fallback for some installs. Accept
        # either — init() below will fail clearly if neither is set.
        cfg = os.path.expanduser("~/.config/pybliometrics.cfg")
        if os.path.exists(cfg):
            return None
        _, err = resolve_credential("SCOPUS_API_KEY", mode=CREDENTIAL_REQUIRED)
        if err is None:
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
            try:
                if ctx.max_records:
                    results, total = self._first_page(
                        PybScopusSearch, q, ctx.max_records,
                    )
                else:
                    results = PybScopusSearch(q, download=True).results or []
                    total = len(results)
            except Exception as exc:  # noqa: BLE001
                if type(exc).__name__ == "Scopus401Error":
                    raise RuntimeError(
                        "Scopus refused the request: this API key may read "
                        "full records only from your institution's network "
                        "(campus or VPN), and this connection is not "
                        "recognised. Connect through the VPN and retry, or "
                        "leave Scopus out. "
                        f"(Elsevier said: {exc})"
                    ) from exc
                raise
            if ctx.max_records:
                self.record_total(label, total)
                print(f"{len(results)} results (API total: {total})",
                      flush=True)
            else:
                print(f"{len(results)} results", flush=True)
            for r in results:
                rows.append(self._result_to_row(r, label))
        return rows

    @staticmethod
    def _first_page(search_cls, query: str, limit: int) -> tuple[list, int]:
        """The first `limit` results of a query and Scopus's hit total.

        pybliometrics offers no page limit: constructed with
        `download=True` it pages through every result, 25 per request in
        the COMPLETE view, so a pilot of a broad query would cost
        hundreds of calls to keep ten rows. This works around that in
        two requests: `download=False` costs one request and yields only
        the total (and writes no cache file, so a later full download is
        unaffected), and one raw request fetches the first page, which is
        handed to the same object so its own `results` parser builds the
        records. That reaches into pybliometrics' private `_json`, so it
        is a stopgap until the library has a page-limit argument
        (BACKLOG D3); the live pilot is where a change would show.
        """
        from pybliometrics.utils import get_content
        from pybliometrics.utils.constants import URLS

        search = search_cls(query, download=False)
        total = search.get_results_size()
        if not total:
            return [], 0
        resp = get_content(
            URLS["ScopusSearch"], "ScopusSearch",
            {"query": query, "view": search._view,
             "count": min(limit, 25), "start": 0},
        )
        entries = resp.json().get("search-results", {}).get("entry", [])
        search._json = entries[:limit]
        return list(search.results or [])[:limit], total

    def _result_to_row(self, r, label: str) -> dict:
        """One pybliometrics result → the common row schema."""
        row = empty_row()
        row.update({
            "db": self.name,
            "query": label,
            "doi": (r.doi or "").strip().lower(),
            "title": r.title or "",
            # Scopus's `author_names` is already "Surname, Given;
            # Surname, Given" — the one database besides WoS that hands
            # over creators a Zotero item can be built from without
            # guessing where the family name ends.
            "authors": r.author_names or "",
            "year": r.coverDate[:4] if r.coverDate else "",
            "source": r.publicationName or "",
            "issn": r.issn or "",
            "volume": str(r.volume or "").strip(),
            "issue": str(r.issueIdentifier or "").strip(),
            "pages": str(r.pageRange or "").strip(),
            "type": self._crossref_type(r),
            "cited_by": r.citedby_count or 0,
            "scopus_id": r.eid or "",
            "abstract": r.description or "",
        })
        return row

    def _crossref_type(self, result) -> str:
        """Crossref type for one pybliometrics result.

        Reads the two-letter `subtype` code, falling back to the
        human-readable `subtypeDescription` when a record carries only
        that. Unknown values return `""`.
        """
        code = (getattr(result, "subtype", "") or "").strip().lower()
        if code in _SCOPUS_SUBTYPE_TO_CROSSREF:
            return _SCOPUS_SUBTYPE_TO_CROSSREF[code]
        described = (getattr(result, "subtypeDescription", "") or "").strip().lower()
        return {
            "article": "journal-article",
            "review": "journal-article",
            "short survey": "journal-article",
            "editorial": "journal-article",
            "letter": "journal-article",
            "note": "journal-article",
            "conference paper": "proceedings-article",
            "book chapter": "book-chapter",
            "book": "book",
        }.get(described, "")

    def _full_query(self, core: str, ctx: SearchContext) -> str:
        """The user's core query, scoped by journal and year.

        The core is parenthesised before anything is appended. Scopus
        binds `AND NOT` and a top-level `OR` looser than the `AND` that
        joins the scope clauses, so an unwrapped `a OR b` or `a AND NOT
        b` let half the query escape the ISSN and year limits.

        An empty `JOURNALS` yields no ISSN clause at all: `ISSN()` is a
        syntax error (HTTP 400), and an empty scope means "unscoped".
        """
        parts = [f"({core.strip()})"]
        issns = [i.strip() for i in ctx.issns if i and i.strip()]
        if issns:
            parts.append(f"ISSN({' OR '.join(issns)})")
        parts.append(
            f"PUBYEAR > {ctx.from_year - 1} AND PUBYEAR < {ctx.to_year + 1}"
        )
        return " AND ".join(parts)
