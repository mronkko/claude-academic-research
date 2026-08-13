"""Web of Science Expanded API search."""

from __future__ import annotations

import time

import http_client

from .base import (
    CREDENTIAL_REQUIRED,
    SearchContext,
    SearchSource,
    empty_row,
    resolve_credential,
)

WOS_ENDPOINT = "https://api.clarivate.com/api/wos"

_WOS_KEY_HINT = (
    "The Starter API does not support IS= ISSN filters, so it is not a "
    "substitute — formal searches need the Expanded tier."
)


class WosSearch(SearchSource):
    name = "wos"
    supports_journal_scope = True
    supports_block_queries = False

    def credentials_error(self, ctx: SearchContext) -> str | None:
        _, err = resolve_credential(
            "WOS_API_KEY_EXTENDED", mode=CREDENTIAL_REQUIRED,
            label="WoS Expanded", hint=_WOS_KEY_HINT,
        )
        return err

    def run(self, config, ctx: SearchContext) -> list[dict]:
        api_key, err = resolve_credential(
            "WOS_API_KEY_EXTENDED", mode=CREDENTIAL_REQUIRED,
            label="WoS Expanded", hint=_WOS_KEY_HINT,
        )
        if err:
            raise RuntimeError(err)
        rows: list[dict] = []
        for label, _scopus_core, wos_core in config.QUERY_DEFS:
            q = self._full_query(wos_core, ctx)
            print(f"  WoS    {label}: ", end="", flush=True)
            data = self._fetch_page(ctx, api_key, q, first_record=1, count=1)
            total = data.get("QueryResult", {}).get("RecordsFound", 0)
            first = 1
            page_size = 100
            while first <= total:
                data = self._fetch_page(ctx, api_key, q, first_record=first,
                                        count=page_size)
                recs_data = (data.get("Data", {}).get("Records", {})
                             .get("records", ""))
                if not recs_data:
                    break
                recs = recs_data.get("REC", [])
                if isinstance(recs, dict):
                    recs = [recs]
                for rec in recs:
                    rows.append(self._extract_record(rec, label))
                first += page_size
                time.sleep(0.3)
            print(f"{len(rows)} results so far  (API total for query: {total})",
                  flush=True)
        return rows

    def _full_query(self, core: str, ctx: SearchContext) -> str:
        issn_part = " OR ".join(ctx.issns)
        return f"{core} AND IS=({issn_part}) AND PY={ctx.from_year}-{ctx.to_year}"

    def _fetch_page(self, ctx: SearchContext, api_key: str, query: str,
                    first_record: int, count: int = 100) -> dict:
        data = http_client.get_json(
            ctx.http(),
            WOS_ENDPOINT,
            headers={"X-ApiKey": api_key},
            params={
                "databaseId": "WOS",
                "usrQuery": query,
                "count": count,
                "firstRecord": first_record,
            },
            timeout=60,
        )
        if data is None:
            raise RuntimeError(
                "WoS Expanded rejected the request (401/403 means the key "
                "lacks Expanded entitlement; 400 means the query is "
                "malformed), or its retries were exhausted. Query: "
                f"{query}"
            )
        return data

    def _extract_record(self, rec: dict, label: str) -> dict:
        uid = rec.get("UID", "")
        static = rec.get("static_data", {})
        dynamic = rec.get("dynamic_data", {})

        titles = static.get("summary", {}).get("titles", {}).get("title", [])
        title = next((t["content"] for t in titles
                      if t.get("type") == "item"), "")
        source = next((t["content"] for t in titles
                       if t.get("type") == "source"), "")

        names = static.get("summary", {}).get("names", {}).get("name", [])
        if isinstance(names, dict):
            names = [names]
        authors = "; ".join(
            n.get("display_name", n.get("full_name", ""))
            for n in names if n.get("role") == "author"
        )

        year = str(static.get("summary", {})
                         .get("pub_info", {})
                         .get("pubyear", ""))

        id_list = (dynamic.get("cluster_related", {})
                          .get("identifiers", {})
                          .get("identifier", []))
        if isinstance(id_list, dict):
            id_list = [id_list]
        doi = next((i["value"].strip().lower() for i in id_list
                    if i.get("type") == "doi"), "")
        issn = next((i["value"].strip() for i in id_list
                     if i.get("type") == "issn"), "")

        abstracts = (static.get("fullrecord_metadata", {})
                           .get("abstracts", {})
                           .get("abstract", {}))
        abstract = ""
        if isinstance(abstracts, dict):
            ab_texts = abstracts.get("abstract_text", {}).get("p", "")
            if isinstance(ab_texts, list):
                abstract = " ".join(ab_texts)
            else:
                abstract = str(ab_texts)

        cited_by = 0
        times_cited = (dynamic.get("citation_related", {})
                              .get("tc_list", {})
                              .get("silo_tc", {}))
        if isinstance(times_cited, dict):
            cited_by = int(times_cited.get("local_count", 0))

        row = empty_row()
        row.update({
            "db": self.name,
            "query": label,
            "doi": doi,
            "title": title,
            "authors": authors,
            "year": year,
            "source": source,
            "issn": issn,
            "cited_by": cited_by,
            "wos_id": uid,
            "abstract": abstract,
        })
        return row
