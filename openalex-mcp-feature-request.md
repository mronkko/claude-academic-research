# Feature request: expose author IDs and support author-ID filtering in works queries

**Target project:** `oksure/openalex-research-mcp` (npm: `openalex-research-mcp`)
**Observed on version:** 0.4.1 (latest as of 2026-04-01)
**Type:** Feature (primary) + small bug fixes (secondary)

## Summary

The works-listing tools return author **names and institutions only** — they drop
the OpenAlex **author ID** that the underlying API provides — and there is no way
to **filter works by author ID**. As a result, a very common and otherwise trivial
OpenAlex operation is impossible through this MCP server: *"give me all works in
journal X authored by any of these N known authors."* Today that can only be
approximated by fragile name + affiliation string-matching, which breaks on common
names and on OpenAlex's merged/duplicated author records.

This request asks for two changes: (1) include author OpenAlex IDs (and ORCIDs) in
works output, and (2) add an author-ID filter to `search_works`. An optional third
convenience tool is described.

## Motivation / concrete use case

We maintain reviewer profiles for a journal's editorial board: a fixed set of ~130
researchers, each already resolved to an OpenAlex author ID. We need to keep the
profiles current by finding, periodically, **all articles published in one specific
journal (by ISSN/source ID) that were authored by any member of that ID set.**

With the raw OpenAlex API this is ~3 calls:

```
GET https://api.openalex.org/works
    ?filter=authorships.author.id:A1|A2|...|A50,primary_location.source.id:S133599136
```

(OR-combine up to ~50 author IDs per call; batch the rest.) Through this MCP server
it cannot be done as an exact join at all, because:

- `search_works` can filter by `source_issn`/`source_id` but **not** by author ID, and
- the works it returns carry no author ID to join on locally.

## Current behavior (v0.4.1)

`search_works` (and the other works-returning tools) return each authorship as:

```json
{ "name": "Gordon W. Cheung", "institutions": ["University of Auckland"] }
```

The OpenAlex API response for the same work includes the author object with `id`
and `orcid`; the wrapper is discarding them. There is no `author_id` / `author_ids`
parameter on `search_works`.

## Requested functionality

### 1. Include author OpenAlex IDs (and ORCID) in works output  *(primary)*

For every tool that returns works, include the author's OpenAlex ID and ORCID in
each authorship object, e.g.:

```json
{
  "id": "https://openalex.org/A5057205670",
  "orcid": "https://orcid.org/0000-0003-1643-9408",
  "name": "Daniel McNeish",
  "institutions": ["Arizona State University"]
}
```

Affected tools (any that emit works): `search_works`, `get_author_works`,
`get_work`, `get_top_cited_works`, `find_seminal_papers`, `get_related_works`,
`search_works_in_venue`, `get_work_citations`, `get_work_references`, etc.
This change alone enables ID-clean joins on the client side.

### 2. Add an author-ID filter to `search_works`  *(primary)*

Add an optional parameter, e.g. `author_ids` (array of OpenAlex author IDs, ORCIDs,
or URLs). Map it to the OpenAlex filter `authorships.author.id:<id1>|<id2>|...`,
OR-combined, and **combinable with the existing** `source_issn` / `source_id` /
`from_year` / `to_year` / `type` filters (AND across filters).

Behavior notes:
- OpenAlex permits roughly **50 values per OR group**; if the caller passes more,
  the tool should either batch transparently (preferred) or document the cap and
  reject clearly rather than silently truncate.
- Accept bare IDs (`A5057205670`), full URLs, and ORCIDs, consistent with how
  `get_author_profile` / `get_author_works` already accept author identifiers.

### 3. (Optional convenience) `get_works_by_authors` tool

A dedicated tool that takes a list of author IDs plus optional venue/date filters and
hides the batching + pagination:

```
get_works_by_authors(
    author_ids: string[],          # required; any length, batched internally by ~50
    source_issn?: string,
    source_id?: string,
    from_year?: number,
    to_year?: number,
    per_page?: number,             # default 200
    sort?: string                  # e.g. publication_year:desc
)
```

Returns works (with author IDs per change #1), de-duplicated across batches. This is
the single-call ergonomic form of the use case above.

## OpenAlex API references (for the implementer)

- Filter by authors + source:
  `https://api.openalex.org/works?filter=authorships.author.id:A1|A2,primary_location.source.id:S133599136`
- `|` = OR within a filter key; `,` = AND across filter keys. ~50 values per OR group.
- The author ID and ORCID are already present in each `results[].authorships[].author`
  object of the raw response — no extra API call is needed; stop dropping them.
- Use `select=id,doi,title,publication_year,publication_date,type,authorships,primary_location`
  to keep payloads small when returning many works.
- Pagination: `per_page` max 200; basic page-number paging is fine for result sets
  under 10,000; use cursor paging (`cursor=*`) for larger sweeps.

## Acceptance criteria

- [ ] `search_works` (and the other works tools) include each author's OpenAlex ID,
      and ORCID when present, in the returned author objects.
- [ ] `search_works` accepts an author-ID filter that OR-combines multiple IDs and
      works together with `source_issn`/`source_id`/`from_year`/`to_year`.
- [ ] A request for "works in source `S133599136` authored by `[A1, A2, … A130]`"
      returns exactly those works (with author IDs), batching the >50-ID set as
      needed. No name-string matching required by the caller.
- [ ] Existing tool signatures and outputs are otherwise unchanged; all new
      parameters are optional and backward-compatible.

## Secondary bugs observed in v0.4.1 (nice to fix in the same pass)

These each return **HTTP 400** for what should be valid input:

1. `search_works` 400s when `query` is `"*"` or an empty string. Empty/whitespace
   query should be treated as "no text filter" (same as omitting it). Today the
   caller must omit the parameter entirely, which is surprising.
2. `search_authors` 400s when `institution` and `exact_phrase` are passed together.
3. `search_authors_by_expertise` 400s when `institution` is combined with a topic or
   `min_h_index`. (Forces callers to fall back to `autocomplete_search` for
   institution-scoped expert lookups.)

In each case the desired behavior is to compose the filters rather than reject the
request.
