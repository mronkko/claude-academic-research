---
name: grounded-citations
description: Use when inserting a new citation into academic prose, attributing a finding to a source, or summarising what a paper says. Trigger phrases: "cite this", "add a citation for X", "what does Smith (2019) say", "summarise this paper", "attribute this finding". Enforces that every citation is a BBT key from Zotero and that the paper's content has been externally consulted in this session (fresh MCP fetch or a Zotero child note) — never recalled from context. If the consulted source does not support the claim, drop the claim. Do NOT use for Zotero library housekeeping — use `zotero-operations`. Do NOT use for auditing an existing draft's citations — use `fact-check`.
---

# Grounded citations

## Core rule

Every citation Claude inserts into academic prose must satisfy **all four**
of these requirements. They are conjunctive — failing any one means the
citation may not be made.

1. **In Zotero.** The paper is in the project's Zotero library. If not,
   add it via `mcp__zotero__zotero_add_by_doi` (or
   `mcp__zotero__zotero_add_by_url` when no DOI exists) before the
   citation is written.
2. **BBT key.** The `[@citekey]` in prose is the Better BibTeX key fetched
   from Zotero via `mcp__zotero__zotero_get_item_metadata` with
   `format="bibtex"`. Never hand-craft keys (`Smith2019`-style); never
   fabricate; never write to Zotero's Extra field to pin a key.
3. **Externalised consultation.** At citation time, the paper's content
   is available either as a **fresh MCP response** (abstract, notes, or
   full-text retrieved in the current turn or an adjacent recent turn) or
   as a **Zotero child note** read via `mcp__zotero__zotero_get_notes`.
   Context-window recall alone is **not** sufficient: remembering an
   abstract read 500K tokens ago is not grounding — either re-fetch it
   or read the note.
4. **Claim support.** The consulted content visibly supports the
   attributed claim. If nothing you have consulted supports the claim,
   **drop the claim**. Do not paper over; do not flag for later; do not
   keep a speculative citation. Remove it from prose, or replace the
   attribution with a source that does support the claim.

## What counts as externalised consultation

Ranked by strength:

- **Zotero full-text** via `mcp__zotero__zotero_get_item_fulltext` — the
  paper's own words, strongest grounding.
- **Zotero child notes** via `mcp__zotero__zotero_get_notes` — durable,
  survives context compaction. Preferred when re-citing a paper multiple
  times across a long session.
- **Fresh abstract** via `mcp__openalex__get_work`,
  `mcp__semantic-scholar__get-paper-abstract`, or
  `mcp__zotero__zotero_get_item_metadata` — minimum acceptable.

**Recommended pattern** for papers cited repeatedly: the first time
Claude reads the paper, write a Zotero child note summarising the
relevant passage via `mcp__zotero__zotero_create_note`. That note
becomes the durable consultation artifact for every subsequent citation
— no re-fetch needed, no context-recall gamble.

## Available search APIs

| Tool | MCP server | Python library | Best for |
|------|-----------|----------------|----------|
| OpenAlex | `mcp__openalex__*` | — | Open metadata, citation graphs, journal/author profiles |
| Semantic Scholar | `mcp__semantic-scholar__*` | — | Abstracts, citation networks, ArXiv |
| Scopus | `mcp__scopus__*` | `pybliometrics` (Python 3.14, config at `~/.config/pybliometrics.cfg`) | Comprehensive citation database, AJG/ABS journal coverage |
| Web of Science | — (not yet implemented) | — | Citation database, journal impact factors; institutional access available |
| Zotero | `mcp__zotero__*` | — | Reference management, full-text retrieval |

Procedures for *adding papers to Zotero*, *fixing BBT keys*, and
*generating `references.bib`* live in the `zotero-operations` skill and
in `scripts/pipelines/generate_bib.py`. Bulk citation workflows for
systematic reviews live in `systematic-review`. Auditing citations in an
existing draft is `fact-check`'s job.

## Red flags

- You are about to cite a paper that is not yet in Zotero — add it first.
- You are hand-crafting a citation key (`Smith2019`) instead of fetching
  the BBT key from Zotero.
- You are citing from context-window recall when the abstract was read
  many turns ago — re-fetch the abstract or read the Zotero note.
- The consulted content does not actually support the claim and you are
  keeping the claim anyway — **drop the claim**, don't paper over.
- You are citing a paper based only on a title match in a search result,
  without having read its abstract.
- OpenAlex and Semantic Scholar return conflicting metadata — resolve
  before citing.
- A DOI search returns no result for a paper you "know" exists — do not
  cite it.
