"""PubMed search via the NCBI E-utilities (esearch + efetch).

Biomedical and dental reviews search PubMed first, and PubMed's native
query language is the thing a methods section reports: MeSH headings,
field tags (`[tiab]`, `[MeSH Terms]`), publication-type filters and
Boolean operators. This source therefore takes the query *verbatim* from
`PUBMED_QUERY_DEFS` in `search_config.py` — a list of `(label, query)`
pairs — and only adds the year window.

What it does NOT do, by design:

- **No journal scope.** `JOURNALS` / ISSN filtering is a social-science
  device (rank lists such as ABS or FT50). A PubMed search is scoped by
  MeSH, field tags and filters in the query itself, so `ctx.issns` is
  ignored. Leave `JOURNALS = {}` for a biomedical review.
- **No citation stream.** PubMed's cited-by relation needs PMIDs, and the
  citation stream is seeded by DOI. Use OpenAlex or Semantic Scholar for
  snowballing.
- **No PMID column.** The shared row schema has none. Records are
  deduplicated by DOI, then by title + first author, so a PubMed record
  with no DOI is still deduplicated against the other databases, but it
  is imported to Zotero from the title-level record.

Credentials are optional. `NCBI_API_KEY` raises the rate limit from 3 to
10 requests per second; `NCBI_EMAIL` (or `CROSSREF_MAILTO`) identifies
the caller, which NCBI asks for.

Reference: https://www.ncbi.nlm.nih.gov/books/NBK25499/
"""

from __future__ import annotations

import os
import re
import time
import xml.etree.ElementTree as ET

from core.providers import redact

from .base import (
    CREDENTIAL_OPTIONAL,
    SearchContext,
    SearchSource,
    empty_row,
    resolve_credential,
)

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

#: Records fetched per efetch call. NCBI accepts up to 10,000, but
#: smaller pages keep each response parseable and each failure cheap.
FETCH_BATCH = 200

#: Seconds between requests: NCBI allows 3/s anonymously, 10/s with a key.
SLEEP_ANON = 0.34
SLEEP_KEYED = 0.11

#: PubMed `PublicationType` → Crossref vocabulary (see
#: `base.CROSSREF_TYPES`). PubMed indexes almost only journal content, so
#: anything not listed is mapped to `journal-article` rather than "".
#: Letters, editorials and comments are still journal items; whether they
#: are eligible is a screening decision, not a search one.
_PUBTYPE_TO_CROSSREF = {
    "journal article": "journal-article",
    "review": "journal-article",
    "systematic review": "journal-article",
    "meta-analysis": "journal-article",
    "randomized controlled trial": "journal-article",
    "clinical trial": "journal-article",
    "case reports": "journal-article",
    "letter": "journal-article",
    "editorial": "journal-article",
    "comment": "journal-article",
    "preprint": "posted-content",
    "dissertation": "dissertation",
    "technical report": "report",
}


class PubMedSearch(SearchSource):
    name = "pubmed"
    supports_journal_scope = False
    supports_block_queries = False
    supports_citation_search = False

    # -- credentials ------------------------------------------------------

    def credentials_error(self, ctx: SearchContext) -> str | None:
        # Both credentials are optional; PubMed always runs anonymously.
        resolve_credential("NCBI_API_KEY", mode=CREDENTIAL_OPTIONAL)
        return None

    # -- run --------------------------------------------------------------

    def run(self, config, ctx: SearchContext) -> list[dict]:
        key, _ = resolve_credential("NCBI_API_KEY", mode=CREDENTIAL_OPTIONAL)
        email = (os.environ.get("NCBI_EMAIL", "") or ctx.mailto or "").strip()
        sleep_s = SLEEP_KEYED if key else SLEEP_ANON

        rows: list[dict] = []
        for label, core in config.PUBMED_QUERY_DEFS:
            term = self.full_query(core, ctx)
            print(f"  PubMed {label}: ", end="", flush=True)
            count, web_env, query_key = self._esearch(term, key, email, ctx)
            print(f"{count} results", flush=True)
            for start in range(0, count, FETCH_BATCH):
                time.sleep(sleep_s)
                xml_text = self._efetch(
                    web_env, query_key, start, key, email, ctx,
                )
                for article in self.parse_articles(xml_text):
                    article["query"] = label
                    rows.append(article)
            time.sleep(sleep_s)
        return rows

    # -- query ------------------------------------------------------------

    @staticmethod
    def full_query(core: str, ctx: SearchContext) -> str:
        """The user's query, parenthesised, with the publication-year window.

        The core is wrapped in parentheses before anything is appended so
        a top-level `OR` or `NOT` cannot escape the date limit — the same
        precedence trap the Scopus searcher guards against.
        """
        window = (
            f'("{ctx.from_year}/01/01"[Date - Publication] : '
            f'"{ctx.to_year}/12/31"[Date - Publication])'
        )
        return f"({core.strip()}) AND {window}"

    # -- HTTP -------------------------------------------------------------

    def _params(self, key: str, email: str) -> dict:
        params = {"tool": "claude-academic-research"}
        if key:
            params["api_key"] = key
        if email:
            params["email"] = email
        return params

    def _get(self, endpoint: str, params: dict, ctx: SearchContext) -> str:
        url = f"{EUTILS}/{endpoint}"
        try:
            response = ctx.http().get(url, params=params, timeout=60)
        except Exception as exc:  # noqa: BLE001 - re-raised, redacted
            raise RuntimeError(redact(f"PubMed {endpoint} failed: {exc}")) from exc
        if response.status_code != 200:
            raise RuntimeError(redact(
                f"PubMed {endpoint} returned HTTP {response.status_code}: "
                f"{response.text[:300]}"
            ))
        return response.text

    def _esearch(
        self, term: str, key: str, email: str, ctx: SearchContext,
    ) -> tuple[int, str, str]:
        """Run the search on NCBI's history server.

        Returns `(count, WebEnv, query_key)`. The history server holds the
        result set so efetch can page through it without re-sending PMIDs.
        """
        params = self._params(key, email)
        params.update({
            "db": "pubmed",
            "term": term,
            "usehistory": "y",
            "retmax": 0,
            "retmode": "xml",
        })
        root = ET.fromstring(self._get("esearch.fcgi", params, ctx))
        error = root.findtext("ERROR")
        if error:
            raise RuntimeError(f"PubMed rejected the query: {error}")
        count = int(root.findtext("Count") or 0)
        web_env = root.findtext("WebEnv") or ""
        query_key = root.findtext("QueryKey") or ""
        if count and not (web_env and query_key):
            raise RuntimeError("PubMed esearch returned no history token")
        return count, web_env, query_key

    def _efetch(
        self, web_env: str, query_key: str, start: int,
        key: str, email: str, ctx: SearchContext,
    ) -> str:
        params = self._params(key, email)
        params.update({
            "db": "pubmed",
            "query_key": query_key,
            "WebEnv": web_env,
            "retstart": start,
            "retmax": FETCH_BATCH,
            "rettype": "abstract",
            "retmode": "xml",
        })
        return self._get("efetch.fcgi", params, ctx)

    # -- parsing ----------------------------------------------------------

    def parse_articles(self, xml_text: str) -> list[dict]:
        """PubMed efetch XML → rows in the common schema.

        Only `PubmedArticle` elements are read. `PubmedBookArticle` is
        skipped: book chapters are not what a PubMed journal search is for
        and the row schema's book handling needs a ISBN-level record.
        """
        root = ET.fromstring(xml_text)
        return [
            self._article_to_row(el) for el in root.findall("PubmedArticle")
        ]

    def _article_to_row(self, el: ET.Element) -> dict:
        citation = el.find("MedlineCitation")
        article = citation.find("Article") if citation is not None else None
        row = empty_row()
        row["db"] = self.name
        if article is None:
            return row

        row["title"] = _text(article.find("ArticleTitle"))
        row["abstract"] = _abstract(article.find("Abstract"))
        row["authors"] = _authors(article.find("AuthorList"))

        journal = article.find("Journal")
        if journal is not None:
            row["source"] = _text(journal.find("Title"))
            row["issn"] = _text(journal.find("ISSN"))
            issue = journal.find("JournalIssue")
            if issue is not None:
                row["volume"] = _text(issue.find("Volume"))
                row["issue"] = _text(issue.find("Issue"))
                row["year"] = _year(issue.find("PubDate"))
        row["pages"] = _text(article.find("Pagination/MedlinePgn"))
        if not row["year"]:
            row["year"] = _year(article.find("ArticleDate"))

        row["doi"] = _doi(el, article)
        row["type"] = _crossref_type(article)
        return row


# ---------------------------------------------------------------------------
# XML helpers (module-level so they can be unit-tested without a source)
# ---------------------------------------------------------------------------


def _text(el: ET.Element | None) -> str:
    """All text inside `el`, markup flattened (`<i>`, `<sub>`, …)."""
    if el is None:
        return ""
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def _abstract(el: ET.Element | None) -> str:
    """Structured abstracts keep their section labels: `METHODS: …`."""
    if el is None:
        return ""
    parts = []
    for node in el.findall("AbstractText"):
        body = _text(node)
        if not body:
            continue
        label = (node.get("Label") or "").strip()
        parts.append(f"{label}: {body}" if label else body)
    return " ".join(parts)


def _authors(el: ET.Element | None) -> str:
    """`Last, First; Last, First` — the shape a Zotero creator needs.

    A collective author (`CollectiveName`) has no family/given split and is
    written as a bare name.
    """
    if el is None:
        return ""
    names = []
    for author in el.findall("Author"):
        last = _text(author.find("LastName"))
        fore = _text(author.find("ForeName")) or _text(author.find("Initials"))
        if last and fore:
            names.append(f"{last}, {fore}")
        elif last:
            names.append(last)
        else:
            collective = _text(author.find("CollectiveName"))
            if collective:
                names.append(collective)
    return "; ".join(names)


def _year(el: ET.Element | None) -> str:
    """Four-digit year from a `PubDate` or `ArticleDate`.

    `PubDate` sometimes carries only `MedlineDate` ("2019 Jul-Aug"), so the
    year is the first four digits found there.
    """
    if el is None:
        return ""
    year = _text(el.find("Year"))
    if re.fullmatch(r"\d{4}", year):
        return year
    match = re.search(r"\b(\d{4})\b", _text(el.find("MedlineDate")))
    return match.group(1) if match else ""


def _doi(pubmed_article: ET.Element, article: ET.Element) -> str:
    """The DOI, from `ELocationID` first and `ArticleIdList` second."""
    for loc in article.findall("ELocationID"):
        if (loc.get("EIdType") or "").lower() == "doi":
            return _text(loc).lower()
    for node in pubmed_article.findall("PubmedData/ArticleIdList/ArticleId"):
        if (node.get("IdType") or "").lower() == "doi":
            return _text(node).lower()
    return ""


def _crossref_type(article: ET.Element) -> str:
    """Map the first recognised `PublicationType` onto Crossref's vocabulary."""
    for node in article.findall("PublicationTypeList/PublicationType"):
        mapped = _PUBTYPE_TO_CROSSREF.get(_text(node).lower())
        if mapped:
            return mapped
    return "journal-article"
