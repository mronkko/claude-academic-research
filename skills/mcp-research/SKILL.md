---
name: mcp-research
description: Use when citing papers, editing .bib files, generating bibliographies, resolving DOIs, adding items to Zotero, or calling MCP research tools (OpenAlex, Semantic Scholar, Scopus, Zotero, paper-search). Enforces the rule that every citation must come from an MCP query in the current session — never from training memory.
---

# MCP research tools

## Core rule

Never cite, summarise, or attribute findings to a paper that you have not
retrieved via an MCP tool in this session. Do not rely on training-data
memory for citations.

## Available search APIs

| Tool | MCP server | Python library | Best for |
|------|-----------|----------------|----------|
| OpenAlex | `mcp__openalex__*` | — | Open metadata, citation graphs, journal/author profiles |
| Semantic Scholar | `mcp__semantic-scholar__*` | — | Abstracts, citation networks, ArXiv |
| Scopus | `mcp__scopus__*` | `pybliometrics` (Python 3.14, config at `~/.config/pybliometrics.cfg`) | Comprehensive citation database, AJG/ABS journal coverage |
| Web of Science | — (not yet implemented) | — | Citation database, journal impact factors; institutional access available |
| Zotero | `mcp__zotero__*` | — | Reference management, full-text retrieval |

Scopus full-text (ScienceDirect) access works via the Elsevier API
(`api.elsevier.com/content/article/doi/{doi}`) with just the API key —
no InstToken required. PDF and XML full text are available for all
Elsevier journals (DOI prefix `10.1016/` etc.). Use
`pybliometrics.sciencedirect.ArticleRetrieval` for structured access or
request `Accept: application/pdf` directly.

## Literature search workflow

1. Start with `mcp__openalex__find_seminal_papers` to anchor the review
   with highly-cited foundational works.
2. Use `mcp__openalex__search_works` for keyword search. Use Boolean
   operators and year filters to narrow results.
3. Expand using:
   - `mcp__openalex__get_work_citations` — forward tracing (who cited this paper)
   - `mcp__openalex__get_work_references` — backward tracing (what this paper cites)
4. Use `mcp__openalex__get_related_works` when keyword search misses
   synonyms or related constructs.
5. Use `mcp__semantic-scholar__get-paper-abstract` to cross-check abstracts
   of important papers before attributing findings to them.
6. Use `mcp__scopus__search_scopus` for supplementary searches, especially
   for AJG/ABS journal coverage.

## Adding to Zotero

- Add every paper to Zotero immediately when found — do not batch or defer.
- Use `mcp__zotero__zotero_add_by_doi` for papers with DOIs (preferred).
- Use `mcp__zotero__zotero_add_by_url` only when no DOI exists.
- After adding, retrieve the BBT (Better BibTeX) citation key using
  `mcp__zotero__zotero_get_item_metadata` with `format="bibtex"`. The key
  is the first argument of the BibTeX entry.
- Use that exact key in manuscript citations. Never hand-craft keys like
  `Smith2019`.

## Citation keys

- BBT keys are auto-generated from author/year/title (e.g.,
  `brownUsingDailyStock1985a`).
- If a key appears in the manuscript but the bibliography generation script
  reports it as "not found": search Zotero by author/title, get the correct
  BBT key, update the manuscript. Do not add a duplicate item.
- Generate the project's bibliography with
  `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/generate_bib.py <project_dir>`.
- Never write to the Extra field to override or pin citation keys.

## Verifying claims against sources

- Before attributing a specific finding to a paper, confirm it by reading
  the abstract via `mcp__openalex__get_work` or
  `mcp__semantic-scholar__get-paper-abstract`.
- If the abstract does not support the claim, do not make the citation.
  Remove the claim or find a source that does support it.
- For papers where full text is in Zotero, use
  `mcp__zotero__zotero_get_item_fulltext` to verify specific statistics or
  quotations.
- Create notes in Zotero for key papers using
  `mcp__zotero__zotero_create_note` to record verified claims.

## Bulk operations (systematic reviews)

The `systematic-review` skill drives the full SLR pipeline. The reusable
scripts it invokes:

- **PDF attachment**:
  `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/attach_pdfs.py` — 7-source
  cascade (Elsevier, Springer, Crossref TDM, PMC, OpenAlex Content,
  Unpaywall, OpenAlex OA). Parallel downloads, serial Zotero uploads.
- **Abstract retrieval**:
  `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/fetch_abstracts.py` —
  6-source cascade (Crossref, S2, Scopus, ScienceDirect, OpenAlex GROBID).
- **Bibliography**:
  `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/generate_bib.py <project_dir>`
  — Scan manuscript for citation keys, generate `references.bib` via BBT.

All scripts read API keys from environment variables (`ZOTERO_API_KEY`,
`ZOTERO_GROUP`, etc.). See the `systematic-review` skill for the full env
var list and usage patterns. A project CLAUDE.md template for systematic
reviews lives at `${CLAUDE_PLUGIN_ROOT}/templates/sr_claude_md.md`.

## Red flags

- You are about to cite a paper without having queried OpenAlex or
  Semantic Scholar for it.
- A DOI search returns no result for a paper you "know" exists — do not cite it.
- OpenAlex and Semantic Scholar return conflicting metadata — resolve
  before citing.
- A paper's abstract contradicts what the manuscript claims the paper
  found — fix the claim.
