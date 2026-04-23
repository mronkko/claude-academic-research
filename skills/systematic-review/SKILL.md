---
name: systematic-review
description: Use when running a full systematic literature review (SLR) — PRISMA-style search, screening, coding, and export. End-to-end pipeline from scripted Scopus/WoS search → Zotero import → abstract fetch → PDF attach → Claude-driven abstract and full-text screening → QA evaluator agents → human adjudication in Zotero → export to manuscript. Trigger phrases: "systematic review", "SLR", "PRISMA", "screen papers", "code the included papers", "run the search", "full-text screening". For isolated Zotero enrichment work (adding abstracts, attaching PDFs to an existing library) that is NOT part of a full SLR pipeline, use the `zotero-operations` skill instead. Targets social-sciences research; medical-SLR instruments (RoB 2, ROBINS-I, evidence hierarchies I–VII, PRISMA-P) are out of scope.
---

# systematic-review

## Pre-flight (ALWAYS run first)

Before any step below, verify the plugin has been configured:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup/check_configured.py"
```

If the result is `NOT CONFIGURED`, stop immediately and tell the user:

> The academic-research plugin has not been set up on this machine
> yet. Run `/setup` first to configure API keys (Zotero, Elsevier,
> WoS, Anthropic, Semantic Scholar), MCP servers, and permission
> rules. Do not attempt an SLR before that.

Do not call MCP tools, run pipeline scripts, or proceed with any stage
of the procedure. `/setup` is the required first step.

If the result is `configured`, proceed.

---

## Bootstrap (first run in this project)

An SR project needs (a) the canonical directory scaffold, (b) four
regression-test files, and (c) pipeline-stage config templates. Run
the three setup helpers below in order. They are all idempotent —
re-running skips anything already in place. Do not use shell
`mkdir -p` (prompts the user, bash-only) or chained `cp` calls
(prompts the user for every chain) for the same work.

Create the directory scaffold:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup/ensure_dir.py" \
    scripts screening pdfs analysis analysis/results manuscript
```

Check what's already present:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup/check_project_scaffold.py" \
    scripts/test_common.py scripts/test_citations.py \
    scripts/test_empirical_integrity.py scripts/test_systematic_review.py \
    search_config.py screening_config.py \
    analysis/manuscript_stats.py manuscript/manuscript_tables.py \
    manuscript/manuscript.qmd
```

If any are missing, install them (one call, skip-if-exists for the
rest):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup/install_templates.py" \
    test_common.py:scripts/test_common.py \
    test_citations.py:scripts/test_citations.py \
    test_empirical_integrity.py:scripts/test_empirical_integrity.py \
    test_systematic_review.py:scripts/test_systematic_review.py \
    search_config.py:search_config.py \
    screening_config.py:screening_config.py \
    manuscript_stats.py:analysis/manuscript_stats.py \
    manuscript_tables.py:manuscript/manuscript_tables.py \
    manuscript.qmd:manuscript/manuscript.qmd
```

Tell the user which files were installed and flag that the top of
each `test_*.py` has project-specific paths, `test_empirical_integrity.py`
has a `FORBIDDEN_LITERALS` tuple, and `search_config.py` /
`screening_config.py` / `manuscript_stats.py` all need customisation
before use.

If the project has no `CLAUDE.md` yet, suggest using
`${CLAUDE_PLUGIN_ROOT}/templates/sr_claude_md.md` as a starting
point — but don't write it without the user's say-so. CLAUDE.md is
user-owned. To install once the user confirms:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup/install_templates.py" \
    sr_claude_md.md:CLAUDE.md
```

---

## Scope lock-in (required before any search)

Before calling ANY search tool — MCP (`mcp__scopus__search_scopus`,
`mcp__openalex__search_*`, `mcp__semantic-scholar__*-search*`,
`mcp__paper-search*__search_*`) or script
(`scripts/pipelines/search*.py`) — **including piloting and volume
probes** — the scope brief must exist at
`.claude/systematic-review/scope.md` AND the user must have
explicitly confirmed it in the current session ("proceed", "looks
good", "confirmed", or equivalent). Silence is not confirmation, and
"experiment with X" is not confirmation of the surrounding scope.

The gate exists because "just a pilot search" shapes the methods:
keyword combinations get baked into the user's mental model, volume
numbers anchor downstream inclusion calls, and reframing after a
pilot is more expensive than reframing on paper. Pin down scope on
paper, get explicit sign-off, then search.

**Brief contents (every section required before asking for
confirmation):**

1. **AI scope** — narrow (generative AI / LLMs only) / medium (ML +
   genAI, pre-LLM work included) / broad (all algorithmic
   decision-making). Give the reason for the choice.
2. **Entrepreneurship scope** — new ventures / SMEs / both. If both,
   name the synthesis strategy (separate strands? single framework?).
3. **Research question(s)** — one or more focal questions the review
   will answer. If the synthesis will map multiple streams (e.g.
   AI-as-tool vs AI-as-domain vs AI-as-method), name the streams.
4. **Time window** — start year (inclusive), end year (inclusive),
   and the reason for the start year (a pivot paper, a technology
   event, a round number with a defence).
5. **Journal set** — tier list (AJG/ABS 2024 / FT50 / ABDC), which
   field codes within it, and whether ISSN-filtering will be used
   (requires WoS Expanded).
6. **Database access** — which databases the user's institution
   provides, which of those the formal search will use, and which
   databases are excluded (with reason).
7. **Exclusion criteria** — language restriction? editorials / book
   reviews / proceedings? conference papers? predatory-listed
   journals?

Draft the brief in conversation, ask the user to confirm, then write
`.claude/systematic-review/scope.md`. Create the directory first:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup/ensure_dir.py" .claude/systematic-review
```

If the user changes scope mid-run, update `scope.md` and any
affected `search_config.py` together before further searches.

**Self-check before every search call:** has `scope.md` been written?
Has the user said "proceed" (or equivalent) since the brief was
finalised? If either answer is no, STOP and finish the interview.

---

## Core architecture

Every systematic review runs through the same stages:

```
search → import to Zotero → fetch abstracts → attach PDFs →
abstract screening → full-text screening/coding → QA with evaluator agents →
human adjudication → export results → test suite → manuscript
```

Principles:

- **Scripted searches only.** Main searches run as Python scripts querying
  APIs directly (Scopus, WoS Expanded, OpenAlex). MCP tools may be used for
  piloting (keyword tests, volume estimates), never for the formal search.
- **Zotero is the ground truth.** Every screening decision, coding field,
  and adjudication outcome lives on the Zotero item — as a tag (for
  decisions and stage membership) or as a child note (for structured
  coding fields). See *Zotero tag conventions* and *Child notes* below for
  the vocabulary. Scripts never delete items from Zotero. See the
  `zotero-operations` skill for lower-level Zotero patterns.
- **CSV logs are run-history, not source of truth.** Screening and coding
  scripts append a row per decision to `screening/*.csv` for provenance
  and debugging (who decided what, when, with which model and prompt
  version). But "what is the current decision on item X?" is answered
  by Zotero, not the CSV. Adjudicator flips happen in Zotero directly;
  re-runs read Zotero tags to decide what to skip.
- **Fix the data, don't work around it.** When a script hits records
  missing a DOI / ISSN / abstract, pause and surface the items. Missing
  DOIs are usually a data-capture bug (search-API field not mapped, manual
  entry, non-journal item). Do not add silent title-match fallbacks until
  the user confirms the data is genuinely unfixable.
- **Resumable stages.** Every stage is Ctrl+C-safe and resume-idempotent.
  On start, scripts read the project's Zotero collection, build a "done"
  set from the stage tags (`abstract:include` / `abstract:exclude` /
  `abstract:borderline` for abstract screening; `fulltext:include` /
  `fulltext:exclude` for full-text coding), and skip items already
  tagged. The CSV log is written in parallel for provenance but is not
  consulted for resume decisions.
- **Progress the user can follow.** Pipeline scripts use `flush=True` on
  every print; emit `[N/total]` counters; invoke via `| tee` to a log
  file. Never pipe to `/dev/null`.
- **Filterable.** Every stage accepts some filter-keys mechanism —
  `--filter-keys-file <path>` for enrichment / audit / export scripts,
  `--only-keys <k1,k2,…>` for screening scripts. Either way, the next
  stage drives from the previous stage's Zotero tag state (queried via
  MCP or pyzotero); the file / CLI filter is a way to narrow further.

## Environment variables

| Variable | Used by | Purpose |
|----------|---------|---------|
| `ZOTERO_API_KEY` | All scripts | Zotero API authentication (required) |
| `ZOTERO_GROUP` | All scripts | Zotero group library ID (per-project, set in the project's own CLAUDE.md or shell) |
| `ANTHROPIC_API_KEY` | Screening scripts | Claude API (required for LLM screening) |
| `ELSEVIER_API_KEY` | `enrich_pdfs.py`, `enrich_abstracts.py` | Elsevier/ScienceDirect full-text retrieval |
| `SCOPUS_API_KEY` | Search scripts | Scopus API (often same as `ELSEVIER_API_KEY`; some institutions issue separately) |
| `WILEY_TDM_TOKEN` | `enrich_pdfs.py --sources wiley` | Wiley TDM UUID token |
| `OPENALEX_API_KEY` | `enrich_pdfs.py`, `enrich_abstracts.py` | OpenAlex Content API ($0.01/download, paid) |
| `SEMANTIC_SCHOLAR_API_KEY` | `enrich_abstracts.py` | Semantic Scholar (higher rate limit with key) |
| `CROSSREF_MAILTO` | All scripts | Crossref polite pool (any email) |
| `WOS_API_KEY_EXTENDED` | Search scripts | WoS Expanded (full Boolean, `IS=` works) — **prefer this** |
| `WOS_API_KEY` | Search scripts | WoS Starter (field-limited, no `IS=`) — piloting only |

The `/setup` skill writes these to `~/.config/academic-research/config.toml`
(mode 0600) on first run. Environment variables take precedence over the
file.

## Zotero tag and note conventions

Zotero is the ground truth for screening decisions, coding fields, and
adjudication outcomes (see the *Core architecture* principles above).
This section is the canonical catalogue of every tag and child note
the pipeline reads or writes. Scripts and skills reference these
conventions; the table below is the single source of truth.

### Stage tags (set by screening scripts)

Tell you where each item is in the pipeline. Mutually exclusive within
each stage — an item has at most one `abstract:*` tag and at most one
`fulltext:*` tag at any given time. Scripts apply these at decision
time via the Zotero API and remove prior stage tags on flip.

| Tag | Applied by | Meaning |
|---|---|---|
| `abstract:include` | `abstract_screen.py` | Passes title-abstract screening — proceeds to full-text |
| `abstract:exclude` | `abstract_screen.py` | Excluded at title-abstract stage |
| `abstract:borderline` | `abstract_screen.py` | Kept for full-text review (missing abstract, or LLM uncertain) |
| `fulltext:include` | `fulltext_code.py` | Passes full-text screening; has `SLR Coding` child note |
| `fulltext:exclude` | `fulltext_code.py` | Excluded at full-text stage |

### Pre-screening and quality-flag tags

Set outside the main screening loop — by preflight checks (predatory)
or post-screening quality audits (retraction). Both are warnings the
adjudicator sees in Zotero, not automatic exclusions.

| Tag | Applied by | Meaning |
|---|---|---|
| `predatory:flag` | Preflight journal check against Beall's list (`import_to_zotero.py`) | **Warning, not exclusion.** Author decides during full-text review whether to keep each flagged paper. |
| `retracted:flag` | Post-coding retraction check via `mcp__zotero__scite_check_retractions` (see *Retraction check* in *Key methodological rules*) | **Warning, not exclusion.** Cited paper has been retracted per Scite's retraction-watch data. Adjudicator decides whether to keep (with a discussion note), replace the citation, or drop the paper. |

### QA and adjudication tags

Applied during the post-screening QA evaluator pass and the human
adjudication loop (see *Post-screening QA* below).

| Tag | Applied by | Meaning | Removed when |
|---|---|---|---|
| `qa-flag` | Main agent after any evaluator flags an item | Sentinel for filtering in Zotero | After human adjudication (replaced by `qa-adjudicated-*`) |
| `qa-hard` | Main agent from a HARD evaluator flag | Clear violation of a named inclusion / exclusion criterion | After adjudication |
| `qa-soft-include` | Main agent from an inclusion-validator SOFT flag | Borderline inclusion | After adjudication |
| `qa-soft-exclude` | Main agent from an exclusion-validator SOFT flag | Borderline exclusion | After adjudication |
| `qa-wrong-code` | Main agent from an exclusion-validator `WRONG_CODE` flag | Exclusion stands but the code is wrong | After the exclusion code is corrected on the item |
| `qa-adjudicated-include` | Human after reviewing flag | Final decision: INCLUDE | Never (permanent adjudication record) |
| `qa-adjudicated-exclude` | Human after reviewing flag | Final decision: EXCLUDE | Never |

### Flip semantics under adjudication

If the human adjudicator flips an automated decision, the Zotero tag
set is updated atomically:

- Remove the screener's `fulltext:*` tag → add the opposite one.
- Remove the `qa-*` severity tag → add the matching `qa-adjudicated-*`.
- Optionally append a row to `screening/fulltext_screening.csv` for
  provenance (who flipped, when, why). The CSV is run-history; the
  tag is the current state.

### Child notes

| Note title | Attached to | Written by | Purpose |
|---|---|---|---|
| `SLR Coding` | Every item with `fulltext:include` | `fulltext_code.py` after each coding decision | Structured coding fields (constructs, method, findings — see `screening_config.py:FULLTEXT_CODING_FIELDS`). The adjudicator reads this note directly in Zotero; the CSV row is parallel provenance. |

A `SLR Coding` note is **created on first code**, **overwritten on
re-code** (via `--full-recode`), and **never deleted automatically**.
If the adjudicator edits a field inline in Zotero, the edit is
authoritative — subsequent `fulltext_code.py` runs skip that item
unless `--full-recode` is passed.

### How scripts use these conventions

- **Resume is tag-driven.** Each script queries Zotero for items
  already carrying the stage tag it writes, and skips them. The CSV
  log is not consulted for resume decisions. `--only-keys` / `--rerun`
  / `--full-recode` flags are the escape hatches for re-processing
  specific items.
- **Filtering downstream stages.** `fulltext_code.py` processes items
  tagged `abstract:include` OR `abstract:borderline`.
  `export_coded_includes.py` reads items tagged `fulltext:include`
  (adjudication flips propagate automatically because tags are
  authoritative).
- **Never hand-craft tags in a manuscript chunk or stats script.**
  Tags come from Zotero; if a stat needs a count of `fulltext:include`
  items, `manuscript_stats.py` queries Zotero, not the CSV.

## Pipeline scripts

All scripts live under `${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/`. Invoke
with `uv run`; first-run `uv` installs declared deps into an ephemeral
venv automatically. Invocations below show the most common form; run
each script with `--help` to see the full flag surface (every script
has additional options for re-processing, parallelism, caching, and
single-item debugging).

| Stage | Script | Invocation |
|---|---|---|
| Multi-database formal search | `search.py` | `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/search.py --config ./search_config.py [--databases scopus,wos,openalex,semantic_scholar]` |
| Single-database piloting (Scopus) | `search_scopus.py` | `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/search_scopus.py --config ./search_config.py` |
| Single-database piloting (Web of Science) | `search_wos.py` | `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/search_wos.py --config ./search_config.py` |
| Single-database piloting (OpenAlex, free) | `search_openalex.py` | `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/search_openalex.py --config ./search_config.py` |
| Single-database piloting (Semantic Scholar) | `search_semantic_scholar.py` | `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/search_semantic_scholar.py --config ./search_config.py` |
| Import deduplicated search CSV into Zotero | `import_to_zotero.py` | `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/import_to_zotero.py --group <id> --input <search.csv> [--collection <key>]` |
| Abstract screening (Claude Haiku on title+abstract) | `abstract_screen.py` | `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/abstract_screen.py --group <id> --collection <key> --config ./screening_config.py` |
| Full-text screening + structured coding (Claude Sonnet) | `fulltext_code.py` | `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/fulltext_code.py --group <id> --collection <key> --config ./screening_config.py --pdf-dir ./pdfs` |
| Fetch missing abstracts (multi-source cascade) | `enrich_abstracts.py` | `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/enrich_abstracts.py --filter-keys-file <keys>` |
| Attach missing PDFs (multi-source cascade) | `enrich_pdfs.py` | `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/enrich_pdfs.py --filter-keys-file <keys>` |
| Attach Wiley PDFs only (TDM token) | `enrich_pdfs.py --sources wiley` | `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/enrich_pdfs.py --sources wiley --filter-keys-file <keys>` |
| Attach Cloudflare-gated PDFs (Sage, APA, T&F, Emerald, …) | `enrich_pdfs.py --sources browser` | `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/enrich_pdfs.py --sources browser --filter-keys-file <keys>` |
| Audit library (missing abstracts / PDFs / stubs) | `audit_zotero_library.py` | `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/audit_zotero_library.py --group <id>` |
| Export includes-only coded view | `export_coded_includes.py` | `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/export_coded_includes.py --log-csv <screening.csv> --out <coded.csv>` |
| Generate `references.bib` from manuscript keys | `generate_bib.py` | `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/generate_bib.py <project_dir>` |

Additional templates shipped with the plugin:

- **`${CLAUDE_PLUGIN_ROOT}/templates/search_config.py`** — journal
  list, query definitions, year window. Read by `search.py` and
  `search_openalex.py`.
- **`${CLAUDE_PLUGIN_ROOT}/templates/screening_config.py`** — system
  prompts for abstract screening and full-text coding, plus the
  `FULLTEXT_CODING_FIELDS` list that drives the coding schema.
- **Test templates** (copy all three plus `test_common.py` into your
  project's `scripts/` directory). One file per skill so failures map
  back cleanly to the rule-book the regression violated:
    - `${CLAUDE_PLUGIN_ROOT}/templates/test_systematic_review.py` —
      this skill's 11 pipeline invariants (PRISMA arithmetic,
      `search_run.json` integrity, decision-state whitelists,
      `temperature=0`, `screening_config` round-trip, ghost handling).
    - `${CLAUDE_PLUGIN_ROOT}/templates/test_citations.py` — `@citekey`
      resolution, bare `Author (YYYY)` detection, BBT-key uniqueness.
      Owned by the `grounded-citations` / `fact-check` skills.
    - `${CLAUDE_PLUGIN_ROOT}/templates/test_empirical_integrity.py` —
      forbidden-literal grep, label uniqueness, inline `s['…']` key
      resolution against the live `build_stats()` dict, figure-file
      existence, `manuscript_stats.json` ↔ `build_stats()` content
      check. Owned by the `empirical-integrity` skill.
    - `${CLAUDE_PLUGIN_ROOT}/templates/test_common.py` — shared
      `TestRunner` infra the three test files import.
- **`${CLAUDE_PLUGIN_ROOT}/templates/manuscript_stats.py`** —
  flat-dict builder that reads every pipeline output and returns keys
  like `screen.n_included`, `search.unique_dois`, etc. for inline
  lookup in the manuscript. Copy into the project's
  `analysis/manuscript_stats.py`; extend as the manuscript needs new
  facts. Output: `analysis/results/manuscript_stats.json` (written by
  the script's CLI mode; never hand-edited).
- **`${CLAUDE_PLUGIN_ROOT}/templates/manuscript_tables.py`** —
  pandas-based table functions (methods, regions, exclusion reasons,
  construct families) for Quarto code chunks. Keeps prose readable.
  Copy into the project's `manuscript/manuscript_tables.py` so the
  `.qmd` can `from manuscript_tables import ...`.
- **`${CLAUDE_PLUGIN_ROOT}/templates/manuscript.qmd`** — Quarto
  scaffold with setup chunk importing `build_stats()`, placeholder
  sections, and example inline expressions showing every methodology
  number wired to `s['key']` rather than hand-typed.

A project CLAUDE.md template for new SLR projects lives at
`${CLAUDE_PLUGIN_ROOT}/templates/sr_claude_md.md`. A
manuscript-only variant (no SLR-pipeline scaffolding, for research-report
editing projects) lives at
`${CLAUDE_PLUGIN_ROOT}/templates/manuscript_claude_md.md`.

## Key methodological rules

### Search

**Pilot before the formal run.** Before committing to the formal search
parameters, probe each candidate database with a handful of keyword
combinations to surface volume estimates and construct-coverage gaps.
Per the *Scripted searches only* principle above, MCP tools are
acceptable for piloting (they are fast and session-scoped), and are
the only way to probe Scopus / OpenAlex / Semantic Scholar without
first spinning up the full scripted-search machinery. The formal run
then uses the scripted searchers under `scripts/pipelines/`.

**Source preference ordering.** Which databases to include depends on
what the user's institution provides. Degrade gracefully rather than
blocking on a missing subscription:

| Preference | Source | Access | Notes |
|---|---|---|---|
| 1 (preferred) | **Web of Science Expanded** | Script only, via `WOS_API_KEY_EXTENDED`. No MCP. | Strongest field coverage for social-sciences SR. Use `WOS_API_KEY_EXTENDED`, not `WOS_API_KEY` — Starter's `IS=` ISSN filter returns 0 results and blocks journal-list filtering. |
| 2 | **Scopus** | Script + MCP (`mcp__scopus__*`). Requires `ELSEVIER_API_KEY` or `SCOPUS_API_KEY`. | Strong alternative when WoS is unavailable. Covers the same journal set as WoS with different dedup patterns. |
| 3 | **OpenAlex** | Script + MCP (`mcp__openalex__*`). Free, no subscription. | Open-access baseline; always usable. Weaker field-precision for niche social-sciences topics, but improves year over year. |
| 4 | **Semantic Scholar** | Script + MCP (`mcp__semantic-scholar__*`). Free tier available. | Good for recent work and preprints; complementary to the above. |

A formal SR typically combines **two or three** sources from this list
— the exact mix depends on access. A user without WoS or Scopus can
still run a defensible SR using OpenAlex + Semantic Scholar, provided
the coverage gaps are disclosed in the methods section (pulled from
`search_metadata.json` via the stats dictionary; never typed in prose).

**Technical tips for search design:**

- **Wildcard multi-word phrases for WoS.** Scopus stems phrases; WoS does
  not. `TS="growth aspiration"` misses plural "aspirations". Always
  write `TS=("growth aspir*" OR ...)`.
- **Merge abstracts during dedup.** Same DOI from Scopus and WoS → keep
  the record with the non-empty abstract. Blindly-first-wins drops data.
- **Second-pass dedup by title+first-author.** DOI-only dedup misses the
  common case where Scopus has a DOI and WoS does not (or vice versa).
  Normalise title, first-author lastname, merge.

### Abstract retrieval cascade

Cascade in order: Crossref → Semantic Scholar (DOI) → Semantic Scholar
(title) → Scopus → ScienceDirect → OpenAlex GROBID.

- **Do NOT use OpenAlex `abstract_inverted_index`.** Often reconstructed
  from GROBID full-text parsing — returns body-text fragments, not
  abstracts. See <https://bmkramer.github.io/SesameOpenScience_site/thought/202411_open_abstracts/>.
- The GROBID TEI XML `<abstract>` element is the acceptable last-resort
  OpenAlex source; still verify length > 60 chars and sense-check.

### PDF retrieval

A four-phase cascade that `enrich_pdfs.py` runs automatically. Each
phase handles a class of item the previous phase can't; nothing is
ever silently dropped.

**Phase 1 — API cascade (`enrich_pdfs.py` default mode).** Works for
most open-access and publisher-TDM-enabled items:

```
publisher TDM API (Elsevier, Wiley)  →  Crossref TDM  →  PMC
  →  OpenAlex Content  →  Unpaywall  →  OpenAlex OA metadata
```

Elsevier and Wiley TDM require `ELSEVIER_API_KEY` and
`WILEY_TDM_TOKEN`. OpenAlex Content is paid ($0.01 per download, gated
on `OPENALEX_API_KEY`).

**Phase 2 — browser cascade for Cloudflare-gated publishers**
(`enrich_pdfs.py --sources browser`). HTTP clients cannot solve the
Cloudflare JS challenge, so for Sage, OUP, Taylor & Francis, Emerald,
and similar CF-gated publishers, a Playwright-driven Chromium opens
visibly. The user passes the Cloudflare challenge once per publisher;
the authenticated session then captures subsequent downloads
automatically. `--legacy-browser` is the rollback path to the pre-v0.3
handler in `scripts/pipelines/legacy/fetch_pdfs_browser.py` — use only
if the refactored browser cascade regresses.

**Phase 3 — Zotero Connector + institutional SFX/OpenURL**
(`enrich_pdfs.py` with Connector handlers). For items the browser
cascade can't reach directly — typically paywalled content accessed via
library proxy — the script launches Zotero Desktop's Connector
extension and routes requests through the institution's SFX/OpenURL
resolver (`scripts/pipelines/fetchers/library_resolver.py`). Requires:
Zotero Desktop running locally, Zotero Connector installed in the
Chromium profile, and the institution's OpenURL base URL configured.

**Phase 4 — graceful failure.** Items that all three phases fail on
are logged with a status code (`connector_zotero_unavailable`,
`connector_save_failed`, `connector_sw_timeout`,
`connector_extension_missing`, and others defined in `enrich_pdfs.py`)
so the user can surface the residual list for manual retrieval.
**Never silently drop items** — a paper with no attached PDF after all
phases is a data-quality signal, not a failure to hide.

**Cross-cutting tips** (apply at every phase):

- **Always validate `%PDF` magic bytes** *and* parse-test the PDF
  before caching. Some downloaders save HTML-with-200 or corrupted PDFs.
- **Disable Chromium's built-in PDF viewer** via a `user_data_dir`
  with `plugins.always_open_pdf_externally=true` in Preferences —
  otherwise PDFs open inline and neither `expect_download` nor
  `expect_response` captures the bytes.
- **Pilot the browser phases on a small batch** before a full run —
  Cloudflare challenges and Connector state are session-scoped; if
  something's misconfigured you want to know after 10 items, not 500.

### Screening

- **Temperature=0 always.** The test suite must grep `"temperature": 0`
  in screening scripts.
- **Haiku for abstract screening** (fast, cheap, sufficient for
  include/borderline/exclude).
- **Sonnet for full-text screening and coding** (needs reasoning
  capacity for structured extraction).
- **Items without abstracts → borderline.** Retain for full-text review;
  never auto-exclude.
- **Append-only logs.** Last-row-wins per Zotero key allows overrides
  without losing history. Abstract becoming available for a previously-
  borderline item does not require editing earlier rows — append a new
  decision.
- **Parallelise with `ThreadPoolExecutor` + `threading.Lock` on the
  CSV log.** Default 8 workers for Haiku, 5 for Sonnet.
- **Resilient JSON parsing.** Even with "JSON only" system prompts,
  Sonnet sometimes emits chain-of-thought before the object. Use
  `llm_helpers.extract_json_from_response()` which walks for the first
  balanced `{...}`. Errored rows write `decision=error` with truncated
  response in `reason`; `--rerun` retries only those.

### Predatory journal flag

Before screening, query a predatory-journal list (Beall's archive at
<https://beallslist.net/> or equivalent) for each journal ISSN. Papers
from listed journals get a `predatory:flag` tag in Zotero. This is a
**warning, not an exclusion** — the author decides during full-text
review whether to keep each flagged paper. Transparent flagging
(not silent removal) is the rule.

### Retraction check

PRISMA quality assessment should catch **retracted papers** in the
included set — citing a retracted paper is a fact-check failure mode.
Run this check **after full-text coding is complete and before
exporting `coded_papers.csv`**, so retractions don't slip into the
manuscript's bibliography.

The Zotero MCP server wraps Scite's free retraction-watch endpoint
(no Scite account required). Invoke via:

```
mcp__zotero__scite_check_retractions(
    group_id=<group>,
    collection_key=<collection>,
)
```

Narrow the scope to items already tagged `fulltext:include` so the
check runs against papers that matter for the synthesis, not the
full library. Retracted items get a `retracted:flag` tag; surface
them to the author before re-running `export_coded_includes.py`.
**Flag, don't silently drop** — the adjudicator decides whether to
keep (with a prominent discussion note), replace the citation, or
exclude.

### Post-screening QA

After every automated full-text screening / coding run (and after
every re-run following prompt changes), launch **three parallel
evaluator agents**, then run a **human adjudication** loop on
whatever they flag. Abstract screening is typically not re-QAed — its
errors surface at Stage 2 anyway — but the pattern works identically
if you want to.

#### The three evaluators

Launch in a **single message, multiple `Agent` tool calls** so they
run in parallel (≈max-of-three latency instead of sum-of-three).
Every evaluator flags items; **no evaluator ever re-decides**.

- **Inclusion validator.** Input: every row decided `include`, with
  the automated reason and the key coding fields. Prompt asks it to
  flag **false positives** — papers that slipped through despite
  failing one of the inclusion criteria. Each flag marks severity
  **HARD** (clearly fails a named criterion) or **SOFT** (borderline,
  defensible). Returns a bulleted list, one per flagged item, with
  `item_key`, severity, and a one-sentence reason.
- **Exclusion validator.** Input: a **stratified sample** across
  exclusion codes — 6–8 items per code. Rationale: each exclusion
  code is a potential source of systematic false negatives, so
  sample across codes rather than uniformly across all exclusions.
  The prompt asks the agent to flag items the screener excluded that
  *should* have been included (false negatives) — HARD if clearly so,
  SOFT if borderline. Also flags a separate category `WRONG_CODE`:
  the exclusion stands but the code is wrong (e.g. exclusion E3 when
  the real reason is E1).
- **Coding-quality validator.** Input: a **random sample** of ≈20 %
  of included papers with **every coding field shown in full** (not
  truncated). The prompt checks each field for: bare labels (should
  be prose), missing citations where theories are named, fabrication
  risk (a claim that sounds too specific to have come from the
  paper), inconsistency across fields, and thin/vague entries. Ends
  with a single-word ship-it verdict and per-paper notes.

The 20 % threshold for coding-quality spot-checks is the plugin's
default. Smaller corpora (< 40 includes) warrant 100 % review;
larger corpora (> 200 includes) can drop to 10 % with a quality
audit built in.

#### Applying QA tags

Evaluators run as `Agent` calls in the main session — they cannot
write to Zotero themselves. The main agent takes each flag the
evaluators return and applies the appropriate `qa-*` tag via
`mcp__zotero__zotero_update_item` (with an `add_tags` parameter) or
`mcp__zotero__zotero_batch_update_tags` for the bulk case.

See *Zotero tag and note conventions* above for the full tag
vocabulary — `qa-flag`, `qa-hard`, `qa-soft-include`,
`qa-soft-exclude`, `qa-wrong-code`, and the two post-adjudication
`qa-adjudicated-*` tags.

#### Human adjudication loop

The human opens Zotero, filters the collection by `qa-flag`, and for
each flagged item:

1. Reads the attached PDF and the `SLR Coding` child note.
2. Decides: **keep** the automated decision, or **flip** it.
3. Updates the Zotero tag set atomically:
   - Removes the severity tag (`qa-hard` / `qa-soft-*` /
     `qa-wrong-code`) and `qa-flag`; adds `qa-adjudicated-include` or
     `qa-adjudicated-exclude`.
   - **If flipping the decision**, also removes the screener's
     `fulltext:*` tag and adds the opposite one. Tags are the
     authoritative state — a flip that doesn't update the `fulltext:*`
     tag leaves Zotero inconsistent with the adjudication.
   - **If correcting an exclusion code without flipping**, updates
     the coding field in the `SLR Coding` child note and removes
     `qa-wrong-code`.
4. Optionally appends a provenance row to
   `screening/fulltext_screening.csv` (who flipped, when, why). The
   CSV is run-history; the Zotero tag is the current state. Downstream
   scripts (`export_coded_includes.py`, `manuscript_stats.py`,
   `test_systematic_review.py`) read from Zotero, not from the CSV.
5. Writes one line to `screening/qa_review.md` recording the decision
   (format below).

#### `screening/qa_review.md` structure

A single markdown file in the project's `screening/` directory with
two sections.

**Scope clarifications.** Protocol-level decisions the adjudicator
made while working through flags. These apply **going forward** and
propagate back into the screening prompt version for any future
re-run. Format:

> 1. **\<one-line rule\>** — \<paragraph rationale\>. *(YYYY-MM-DD)*

Example: *"Cross-country GEM studies at country-year level are in
scope. Rationale: the GEM cluster is a coherent strand; fragmenting
it weakens synthesis."*

**Adjudication log.** One line per flagged item, in processing order.
Format:

> `{item_key}` **{short citation}** — **{kept DECISION / flipped to
> DECISION [EXCLUSION_CODE]}** — \<one-to-two-sentence rationale\>.
> *(YYYY-MM-DD)*

Group related flips onto one line when the rationale is identical
(e.g. "10 GEM studies — all kept INCLUDE — see scope clarification
1"). Individual contentious flips get their own line.

This file **is** the methods-section evidence for the manuscript's
QA paragraph. Without it, the adjudication is not reproducible.

#### Red flag

You are about to **silently drop a `qa-flag`ed item** — remove the
flag without recording a disposition in the adjudication log. Never.
Every flagged item gets one line in `qa_review.md`, even if the
decision is "kept without change". Silent drops break the
reproducibility invariant that makes the QA step worth the effort.

## Data integrity

These rules supplement the `empirical-integrity` skill with SR-specific
patterns:

- **Auto-extract script constants** into `search_metadata.json`. Never
  import scripts (side effects); parse with
  `re.search(r'CONSTANT\s*=\s*"([^"]+)"', source)`. Keywords, year
  bounds, model names, prompt versions all live in the metadata file.
  `analysis/manuscript_stats.py` then ingests `search_metadata.json` and exposes
  each field under `s['search.*']` or `s['provenance.*']` in the
  manuscript's stats dictionary — the manuscript never reads
  `search_metadata.json` directly.
- **Forbidden methodology literals.** The project's test suite must
  grep the manuscript for hand-typed search dates, model names
  (`claude-haiku`, `claude-sonnet`), keyword strings, year bounds.
  These must use inline expressions from the stats dictionary
  (`s['search.databases']`, `s['provenance.fulltext.model']`, …).
- **PRISMA arithmetic test.** `include + borderline + exclude = total
  screened`; `coded include + exclude = total coded`. Catches missing
  items or pipeline drops.
- **Search integrity gatekeeper.** `search_run.json` records the
  canonical count of unique DOIs from the scripted search. Post-import
  invariant: Zotero DOIs == search DOIs. Abort if extras exist (items
  added outside the pipeline).

## Test suite

See `empirical-integrity` for the overall approach and file layout.
SR-specific invariants live in
`${CLAUDE_PLUGIN_ROOT}/templates/test_systematic_review.py` (copy into
the project's `scripts/`). The file ships 14 active tests:

| Test | What it catches |
|---|---|
| Pipeline artefacts exist and non-empty | Pipeline didn't run |
| `search_run.json` marker matches dedup CSV | Stale or missing integrity gatekeeper |
| `search_metadata.json` has required fields | Export bug |
| No duplicate DOIs in dedup CSV | Dedup gap |
| Abstract log uses allowed decision states | Pipeline emitted an unexpected abstract-stage decision |
| Fulltext log final decisions | Non-final (`error`) decision left at the end of the fulltext log |
| PRISMA arithmetic | Screening funnel inconsistency |
| Coded count == fulltext includes | Export/coding drift |
| `temperature=0` pinned in Claude calls | Reproducibility regression |
| `screening_config` constants match logs | Config changed without a re-run |
| No `decision=error` left in fulltext log | Unresolved screening errors |
| No ghost keys (fulltext log ⊆ Zotero) | Items removed or renamed outside the pipeline |
| **Fulltext tags consistent with CSV log** | Zotero tag state diverges from CSV decisions — tag write-back failed, or an out-of-band CSV edit wasn't mirrored in Zotero |
| **Every `fulltext:include` item has an SLR Coding note** | Include-tag set without a coded note — export script has nothing to read for that paper |

BBT-key uniqueness and `coded_papers.csv` → `references.bib` resolution
live in `test_citations.py` (citation concerns). Manuscript-prose
invariants — forbidden literals, label uniqueness, inline `s['…']`
resolution, figure-file existence — live in
`test_empirical_integrity.py`. Zotero-collection dedup checks are run
via `mcp__zotero__zotero_find_duplicates`, not as a static test.

**Grow the suite with the pipeline.** When you find a new SR-pipeline
regression a static check could catch — a new metadata field that
should round-trip, a new PRISMA edge case, a new Zotero-drift pattern —
add the test to `scripts/test_systematic_review.py` before closing
out the task. The failure becomes the sentinel so the same class of
mistake can't silently return across runs.

## Scope note

This skill targets **social-sciences systematic reviews** (management,
entrepreneurship, IS, organizational behavior). Medical / clinical SLR
instruments — evidence hierarchies (I–VII), RoB 2, ROBINS-I, PRISMA-P
preregistration — are **out of scope** for v0.1. A medical-SLR variant
would need those plugged in; forcing them into social-science reviews
is domain-inappropriate.

## Red flags

- You are about to hardcode an API key in a reusable script (use env vars).
- Temperature is not pinned to 0 in a screening or coding API call.
- An OpenAlex abstract is being used directly without cross-checking
  against Crossref or the GROBID `<abstract>` element.
- A manual count appears in manuscript prose instead of an inline
  expression.
- A downloaded file is assumed to be a PDF without checking `%PDF`
  magic bytes.
- Zotero contains items not in the current search scope (extras from
  prior runs or manual additions).
- A PDF download returned HTTP 200 but the response is HTML (Cloudflare
  challenge page).
- You are adding a non-DOI fallback (title fuzzy match, author-based
  dedup) without first surfacing the DOI-less records to the user and
  asking whether the source data should be fixed instead.
- A predatory-journal flagged paper is being silently excluded instead
  of surfaced to the author for decision.
- You are about to read `~/.config/academic-research/config.toml` via
  `cat`, `head`, `tail`, `grep`, `less`, `more`, `awk`, `sed`, a
  Python script, or any other command. **NEVER read that file.** It
  holds API keys. Pipeline scripts read it via Python's `open()`
  outside your tool layer; you have no legitimate reason to inspect
  it. If debugging feels like it needs a look inside the file, ask
  the user to re-run `/setup` — that's the reset path.
- You are about to write a Bash heredoc or an inline Python script to
  run a pipeline-style task (enumerate a library, compute stats,
  mutate Zotero, fetch abstracts, etc.). **Never improvise.** If a
  shipped script under `scripts/pipelines/` covers the task, invoke
  it. If none does, tell the user which task is missing and propose
  adding a shipped script — do not write a one-off. Improvised
  scripts leak keys through your context and sidestep pre-approved
  permissions.
