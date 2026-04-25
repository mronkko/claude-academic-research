# Glossary

Compact reference for the acronyms and tooling terms the
academic-research plugin's skills use. Each entry is one sentence —
enough to anchor the term on first encounter; deeper context lives
in the skill that introduces it. Skills are loaded independently by
the Claude Code harness, so each skill *also* defines critical terms
on first use within itself; this file is the canonical longer entry.

## Tooling and infrastructure

- **MCP** — *Model Context Protocol*. The standard the Claude Code
  harness uses to talk to "MCP servers" — small helper programs
  Claude calls in the background.
- **MCP server** — a small helper process Claude can call. Examples
  registered by this plugin's setup wizard: Zotero, Scopus,
  OpenAlex, Semantic Scholar, paper-search.
- **Skill** — a prose rule-book the harness loads when a user's
  request matches the skill's trigger phrases. Skills tell Claude
  *how* to approach a task; they do not contain executable code.
- **Plugin** — this repository, packaged for `/plugin marketplace
  add mronkko/claude-academic-research`. Ships skills, pipeline
  scripts, and templates that downstream Claude Code instances use.
- **`${CLAUDE_PLUGIN_ROOT}`** — environment variable Claude Code
  resolves to the active plugin version's absolute directory before
  the model emits text. Always use this in pasted shell commands —
  never the `~/.claude/plugins/cache/.../*/` glob (it breaks when
  two plugin versions are cached side-by-side).
- **glob** — a shell-pattern wildcard like `*.py` or
  `~/.claude/.../*/scripts/...`. Globs expand to multiple paths;
  passing one to `python3` errors with "ambiguous arguments".

## Reference data + databases

- **BBT** — *Better BibTeX*, a Zotero plugin. Generates citation
  keys (e.g. `smith2020Foo`) automatically as items are added, and
  exposes a local JSON-RPC endpoint at
  `http://127.0.0.1:23119/better-bibtex/json-rpc` plus a bibtex
  library export at `/better-bibtex/library/{id}/library.bibtex`.
- **BBT key / citation key** — the short identifier (e.g.
  `brownUsingDailyStock1985a`) BBT generates for a Zotero item. Used
  in manuscripts as `@brownUsingDailyStock1985a`.
- **DOI** — *Digital Object Identifier*, e.g. `10.1016/j.respol.
  2020.104010`. The canonical identifier for a journal article;
  most pipeline scripts key off DOI for dedup and lookups.
- **ISSN** — *International Standard Serial Number*, the journal
  identifier (e.g. `0883-9026`). The plugin's `_canonicalize_issn`
  helper normalises Scopus's bare 8-digit form to L-form (with
  hyphen) so dedup works across databases.

## Scholarly / publisher terms

- **CSL** — *Citation Style Language*. XML files that describe how
  a citation should be formatted (APA, Chicago, MLA, …). Pandoc /
  Quarto consume them via `--csl=<file>` or YAML `csl:`.
- **SFX / OpenURL** — institutional link resolvers. SFX is Ex
  Libris's product; OpenURL is the underlying standard. The
  library_resolver in this plugin probes the SFX endpoint to know
  which publishers your institution has full-text access to before
  trying a direct fetch.
- **TDM** — *Text and Data Mining*. Elsevier's TDM API
  (`api.elsevier.com/content/article/doi/...`) is meant for
  programmatic full-text access; the plugin uses it instead of
  scraping ScienceDirect.

## Journal-ranking sources

- **ABS** — *Chartered Association of Business Schools*. Their
  Academic Journal Guide (AJG) ranks management-discipline journals
  on a 1-2-3-4-4* scale. The plugin's `build_journal_list_from_abs.py`
  reads the AJG xlsx.
- **JCR** — *Journal Citation Reports*. Clarivate's impact-factor
  ranking. No `_from_jcr.py` shipped yet; the architecture is
  source-agnostic so a sibling script could be added.
- **FNEGE** — French national journal-ranking. Same pattern.
- **ABDC** — *Australian Business Deans Council*. Same pattern.
- **CiteScore** — Scopus's metric. Per-database ranking, not a
  separate authoritative list.

## Pipeline conventions

- **PRISMA** — *Preferred Reporting Items for Systematic Reviews
  and Meta-Analyses*. The reporting standard the systematic-review
  skill follows. Defines the search → screen → code → extract flow
  the pipeline orchestrates.
- **Stage tag** — Zotero tag with a `stage:value` shape that records
  pipeline position: `abstract:include`, `fulltext:exclude`,
  `qa-adjudicated-include`, etc. Tags are the authoritative state;
  CSV logs are run-history.
- **FE-code** — *Full-text exclusion code*. Reasons for excluding
  at full-text screening: FE2 (book chapter), FE3 (other
  non-journal), FE6 (no fulltext available), and project-specific
  codes defined in `screening_config.py`. Surfaces in the audit
  report grouped by `pdf_fetch_log` cause.

## Cross-platform note

When pipeline scripts and skills mention paths like `~/.config/...`,
that's prose shorthand for `Path.home() / ".config" / ...` in code.
Don't write `open("~/x")` in Python — `Path.home()` resolves
correctly on every OS; `~` is shell-only.
