# Glossary

Compact reference for the acronyms and tooling terms the
academic-research project's skills use. Each entry is one sentence —
enough to anchor the term on first encounter; deeper context lives
in the skill that introduces it. Skills are loaded independently by
the agentic harness (such as Claude Code or Antigravity), so each skill
*also* defines critical terms on first use within itself; this file is the
canonical longer entry.

## Tooling and infrastructure

- **MCP** — *Model Context Protocol*. The standard the agentic
  harness (such as Claude Code or Antigravity) uses to talk to "MCP servers" —
  small helper programs the agent calls in the background.
- **MCP server** — a small helper process the agent can call. Examples
  registered by the setup wizard: Zotero, Scopus,
  OpenAlex, Semantic Scholar, paper-search.
- **Skill** — a prose rule-book the harness loads when a user's
  request matches the skill's trigger phrases. Skills tell the agent
  *how* to approach a task; they do not contain executable code.
- **REQUIRED SUB-SKILL** — a marker inside one skill's body or a
  per-subagent prompt that names another skill to load via the
  `Skill` tool **before proceeding**. The receiver loads the named
  sub-skill itself — the caller does *not* inline the sub-skill's
  content into the prompt. Used to keep shared doctrine in one
  place (e.g. `verifying-citations` is loaded as a REQUIRED
  SUB-SKILL by both `fact-check` and `critic-loop`'s evidence
  critic). See CLAUDE.md for the full contract.
- **Plugin** — this repository, packaged for `/plugin marketplace` or
  direct integration. Ships skills, pipeline scripts, and templates
  that agentic environments use.
- **`${CLAUDE_PLUGIN_ROOT}`** — environment variable resolving to the active
  plugin version's absolute directory. Always use the POSIX fallback
  `${CLAUDE_PLUGIN_ROOT:-.}` in pasted shell commands to ensure compatibility
  across both Claude Code and other agentic environments (such as Antigravity).
- **glob** — a shell-pattern wildcard like `*.py` or
  `./scripts/...`. Globs expand to multiple paths;
  passing one to `python3` errors with "ambiguous arguments".

## Reference data + databases

- **BBT** — *Better BibTeX*, a Zotero plugin. Generates citation
  keys (e.g. `smith2020Foo`) automatically as items are added, and
  exposes a local JSON-RPC endpoint at
  `http://127.0.0.1:23119/better-bibtex/json-rpc` plus a bibtex
  library export at `/better-bibtex/library/{id}/library.bibtex`.
- **BBT key / citation key** — the short identifier BBT generates
  for a Zotero item, used in manuscripts as `@brownUsingDailyStock1985`.
  When two items would collide on author / year / first significant
  title word, BBT appends lower-case suffixes — `…1985`, `…1985a`,
  `…1985b`, etc. The suffix is part of the key; never strip it.
- **DOI** — *Digital Object Identifier*, e.g. `10.1016/j.respol.
  2020.104010`. The canonical identifier for a journal article;
  most pipeline scripts key off DOI for dedup and lookups.
- **ISSN** — *International Standard Serial Number*, the journal
  identifier (e.g. `0883-9026`). Most databases return the
  hyphenated 8-digit L-form; Scopus returns the bare 8-digit form
  (`08839026`). The pipeline normalises both to L-form for
  cross-database dedup; the normalisation helper lives in the
  scripts, not in this glossary.
- **Scopus** — Elsevier's curated citation database. Accessed
  in this plugin via two surfaces: the registered `mcp__scopus__*`
  MCP server (for ad-hoc lookups during drafting) and the
  `pybliometrics` Python library (config at
  `~/.config/pybliometrics.cfg`) used by `scripts/pipelines/searchers/`
  for SR-pipeline searches. Requires an Elsevier API key, registered
  by the setup wizard.

## Scholarly / publisher terms

- **CSL** — *Citation Style Language*. XML files that describe how
  a citation should be formatted (APA, Chicago, MLA, …). Pandoc /
  Quarto consume them via `--csl=<file>` or YAML `csl:`.
- **Link resolver / OpenURL** — the service that answers "which
  licensed routes does my institution have to this article?". OpenURL
  is the underlying standard; the plugin speaks two dialects of it as
  equal peers, autodetected from `[library] openurl_base` and
  overridable with `[library] resolver`:
  - **SFX** — Ex Libris's older product. Returns `<target>` elements
    with real publisher or EZproxy URLs, so routes are identifiable by
    hostname.
  - **Alma `uresolver`** — what **Primo VE** institutions have, and the
    majority today. Primo's public OpenURL path only redirects to the
    HTML discovery UI, so the plugin talks to Alma's
    `/view/uresolver/<inst_code>/openurl` endpoint instead. Its routes
    all point at the Alma redirector rather than the publisher, so they
    are identified by the platform *names* Alma sends
    (`package_public_name` / `interface_name`) — which is why the
    plugin ranks platforms by host **or** name.

  The resolver runs only in the browser and Zotero Connector passes, to
  skip items your library cannot reach before opening Chromium for them.
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
