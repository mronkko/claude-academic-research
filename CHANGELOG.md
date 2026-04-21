# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.1] — 2026-04-21

### systematic-review skill: QA-evaluator pattern fully documented

The skill already mentioned the three-agent QA step in passing. This
release writes out the full protocol that the reference SLR project
uses, so the plugin can drive the QA loop end-to-end without a
separate external reference.

Added to `skills/systematic-review/SKILL.md`:

- **Three evaluator sketches** (inclusion validator, exclusion
  validator, coding-quality validator) with each evaluator's
  sampling strategy, prompt focus, and severity scheme. Includes the
  default 20 % coding-quality spot-check threshold with tuning
  guidance for smaller / larger corpora.
- **Tag-vocabulary table** listing all seven `qa-*` tags, when each
  is applied, and when each is removed. Closes the ambiguity around
  whether `fulltext:include` / `fulltext:exclude` move alongside
  `qa-adjudicated-*` (they do not — screener verdict vs. reviewer
  process trail are separate records).
- **Human adjudication loop** as a six-step procedure with the
  last-row-wins CSV append pattern for flips, plus the separate
  `qa-wrong-code` path for code corrections that don't flip the
  decision.
- **`screening/qa_review.md` structure** with both sections
  (Scope clarifications + Adjudication log) and the exact line
  format for each. Cross-references the existing example line.
- **Red flag** against silently dropping a `qa-flag`ed item without
  recording a disposition.

No code changes; prose-only. Default tests unchanged (165 pass
today — the other instance's refactor has already added new tests).

## [0.2.0] — 2026-04-21

### Manuscript scaffold — Milestone G, and plugin-v0.2 milestone

Ships the last missing piece of the end-to-end SLR pipeline: the
manuscript scaffold. With this release, a project can go from search
results to a rendered manuscript using only plugin-shipped artifacts
plus the per-project config files.

New templates:

- **`templates/stats.py`** — flat-dict builder that reads every
  pipeline output (`search_metadata.json`, `search_run.json`, the
  two screening CSVs, `coded_papers.csv`) and returns keys like
  `search.unique_dois`, `screen.abstract.n_include`,
  `screen.n_included`, `provenance.fulltext.model`,
  `provenance.fulltext.prompt_version`. Flat dotted keys fail loudly
  on typos in the manuscript, which is the whole point. Also
  demonstrates an optional regex-based family classifier for free-text
  coding fields.
- **`templates/_tables.py`** — pandas-based table helpers that turn
  `coded_papers.csv` into publication-ready tables (methods,
  geographic regions, exclusion reasons, included-papers list).
  Keeps Quarto chunks one-liners.
- **`templates/manuscript.qmd`** — Quarto scaffold with a setup
  chunk importing `build_stats()`, placeholder sections (introduction,
  methods, findings, discussion, limitations, references), and
  example inline expressions showing every methodology number wired
  to `s['key']` rather than hand-typed. The scaffold passes its own
  empirical-integrity check out of the box.

systematic-review skill's "Additional templates" section now lists all
six templates the plugin ships (search_config, screening_config,
test_suite, stats, _tables, manuscript) and what each is for.

### Plugin end-to-end status

The pipeline is now complete for social-sciences SLRs from search
through render. Stages and their shipped scripts:

- Search → `search.py` / `search_openalex.py`
- Import → `import_to_zotero.py`
- Enrich → `fetch_abstracts.py` + `attach_pdfs.py` /
  `fetch_pdfs_wiley_tdm.py` / `fetch_pdfs_browser.py`
- Audit → `audit_zotero_library.py`
- Screen → `abstract_screen.py` + `fulltext_code.py`
- Export → `export_coded_includes.py`
- Bibliography → `generate_bib.py`
- Test → `templates/test_suite.py`
- Render → `templates/manuscript.qmd` + `templates/stats.py` +
  `templates/_tables.py`

### Still deferred (v0.2.x candidates)

- Zotero tag + child-note write-back from `fulltext_code.py` (coded
  decisions currently live only in the CSV log).
- Standalone `search_scopus.py` / `search_wos.py` piloting wrappers.
- INFORMS and OUP custom flows in `fetch_pdfs_browser.py` (caught by
  the `live_browser` test suite as FAIL today).

72 default tests pass; ruff clean.

## [0.1.9] — 2026-04-21

### Screening scripts — Milestone F

Ports `abstract_screen.py` and `fulltext_code.py` from the SLR
reference project with full generalisation: prompts, coding schema,
and model choice all come from a per-project `screening_config.py`,
so the plugin's copies of the scripts are deliberately generic.

New files:

- **`scripts/pipelines/abstract_screen.py`** (~220 lines) — stage-1
  screening. Claude Haiku on title+abstract at temperature=0. Reads
  `ABSTRACT_SCREENING_SYSTEM_PROMPT`, `ABSTRACT_SCREENING_MODEL`, and
  `ABSTRACT_SCREENING_PROMPT_VERSION` from the config. Parallelised
  with `ThreadPoolExecutor` + `threading.Lock` on the CSV log. Append-
  only log with `item_key` as key; re-running skips already-screened
  items. Flags: `--dry-run`, `--sample N`, `--workers N`.
- **`scripts/pipelines/fulltext_code.py`** (~320 lines) — stage-2
  screening + structured coding. Claude Sonnet on full PDF text.
  Reads `FULLTEXT_CODING_SYSTEM_PROMPT` and `FULLTEXT_CODING_FIELDS`
  from the config. **The coding schema is dynamic**: the script
  renders the field list into the system prompt's JSON-schema
  section, and builds the output CSV's columns from the same list —
  add a field to `FULLTEXT_CODING_FIELDS` and both the prompt and
  CSV schema update automatically. Uses `core.llm.extract_pdf_text`
  (pdfplumber + pypdf fallback) and `core.llm.extract_json_from_response`
  for lenient JSON parsing of Sonnet's output. Flags: `--dry-run`,
  `--limit N`, `--only-keys K1,...`, `--workers N`, `--rerun`
  (reprocess error rows), `--full-recode` (backup + rebuild).
- **`templates/screening_config.py`** — minimal-but-runnable template
  for both screening stages. Placeholder research question, inclusion
  criteria, exclusion codes, and three starter coding fields
  (`key_findings`, `sample`, `method`) for the user to extend. Each
  prompt carries a `PROMPT_VERSION` string that lands in every CSV
  row for traceability.

`systematic-review` skill's stage-to-script table now includes both;
the "deferred" section shrinks to just the Quarto manuscript
scaffold.

**Deferred to v0.2.x**: automatic write-back of `fulltext:include` /
`fulltext:exclude` tags and coded-field child notes to Zotero
(currently documented as a post-run reminder in the script output).

72 tests pass (no new unit tests for the screening scripts — they're
thin wrappers over API clients; live smoke testing is the right
approach). Ruff clean.

## [0.1.8] — 2026-04-21

### Search scripts — first half of Milestone E

Ported the two main search scripts from the SLR motivation reference,
plus a template for the per-project search configuration. Pipeline
can now run a real formal search against Scopus / WoS / OpenAlex from
a project's own `search_config.py`.

New files:

- **`scripts/pipelines/search.py`** (~335 lines) — Scopus + Web of
  Science Expanded orchestrator. Reads a per-project
  `search_config.py` by path (via `--config`). Runs each `QUERY_DEFS`
  entry against Scopus (via pybliometrics) and optionally WoS
  (`--wos`). Deduplicates across databases by DOI with a
  title+first-author fallback for no-DOI records; merges abstracts
  when they exist. Writes `search_results_raw.csv`,
  `search_results.csv`, `search_metadata.json`, and a DOI-set hash
  in `search_run.json` (the integrity gatekeeper every downstream
  test reads).
- **`scripts/pipelines/search_openalex.py`** (~250 lines) — free
  alternative using OpenAlex REST API. Runs two block queries
  (`BLOCK_A_TERMS`, `BLOCK_B_TERMS` from `search_config.py`)
  separately and merges, because OpenAlex's relevance-ranked
  `search=` parameter loses recall on combined queries. No API key
  required; uses `CROSSREF_MAILTO` for polite-pool identification.
- **`templates/search_config.py`** — minimal-but-runnable example
  with 5 entrepreneurship journals, two `QUERY_DEFS` entries
  (narrow + broad), and two OpenAlex block-term lists. Comments
  explain per-query Scopus vs. WoS stemming differences and the
  recall reasoning behind the block-query approach.

Updated `systematic-review` skill: the script-invocation table now
lists `search.py` and `search_openalex.py`, and the "deferred"
section drops the search-scripts bullet.

**Still deferred for later milestones:** standalone `search_scopus.py`
/ `search_wos.py` (users can run `search.py` with just one database
today), `abstract_screen.py`, `fulltext_code.py`, Quarto manuscript
scaffold.

No changes to existing plugin code. Default tests unchanged (72 pass).

## [0.1.7] — 2026-04-21

### Live-test fixes after first real run

First real-keys run of the new live suite flushed out three failures.
Root causes were a mix of test-code bugs and wrong test DOIs:

- **`test_scopus_abstract` was genuinely broken.** I used
  `view="META_ABS"` which populates `.description` and leaves
  `.abstract` as `None` — a pybliometrics quirk. The plugin's
  production code at `fetch_abstracts.py` correctly uses
  `view="FULL"`. Test now matches production. (Also dropped a
  one-off debug helper at `scripts/debug/inspect_scopus_abstract.py`
  that surfaces this kind of pybliometrics field-naming oddity for
  future debugging.)
- **`test_crossref_tdm_link_present` had the wrong DOI.** PLOS ONE
  DOIs have no text-mining link on Crossref because they are fully
  open-access and expose full text elsewhere. Switched to an Elsevier
  DOI (verified: 2 text-mining links). On any future DOI that still
  lacks a TDM link, the test skips with an explanation pointing at
  `KNOWN_DOIS['crossref_tdm']`.
- **`test_wiley_tdm_downloads_pdf` had an out-of-scope DOI.** The
  ETP 2010 DOI was not in the institution's Wiley TDM scope (ETP
  moved to Sage in 2022; older issues may or may not be TDM-accessible
  at Wiley). Switched to an SMJ 2024 DOI. On "Unknown Doi" / "not
  entitled" / "forbidden" responses, the test skips with an
  institutional-scope explanation rather than failing.
- **Browser-test `wiley` DOI updated** — same ETP-at-Sage issue; now
  points at the same SMJ DOI.

Test-design principle codified: **PASS when the endpoint works, SKIP
when the test DOI falls outside the provider's coverage, FAIL only
when the endpoint itself is broken.** On your machine today:
18 passed, 5 skipped (all known-legitimate), 0 failed.

### Wizard MCP-server registration

The `/setup` wizard now checks five Model Context Protocol servers
and offers to register any that are missing:

- **Zotero** (required tier — every citation skill depends on it).
- **Scopus / Semantic Scholar / OpenAlex** (search-database tier —
  at least one required for literature search).
- **paper-search** (optional — ArXiv / PubMed PDF retrieval).

Each server has a `McpServerSpec` with a homepage, an install
command, and a free-text install note. The wizard parses
`claude mcp list` output to classify each server as connected /
needs-auth / failed / missing, prints a summary with counts per
tier, and exits with code 4 if the required Zotero server is not
connected. The `setup` skill's error-handling guidance is updated
to cover "command not found" for each underlying MCP binary and
the new exit-code-4 case.

Wizard grew by ~380 lines; tests grew accordingly (72 default tests,
was 59).

### No changes to plugin production-pipeline code.

## [0.1.6] — 2026-04-20

### Test-suite template for SLR projects

Ports the 528-line project-specific test suite from the reference SLR
into a generalised template at
`templates/test_suite.py`. Ship 13 universal tests that check
invariants every SR pipeline must satisfy, plus commented scaffolding
for four project-specific test families the user fills in.

**Universal tests (run out of the box):**

- Pipeline artefacts exist and are non-empty.
- `search_run.json` DOI count matches the deduplicated CSV.
- `search_metadata.json` has required fields (dates, databases, year
  bounds, queries).
- No duplicate DOIs in the deduplicated search output.
- Abstract / full-text decision states match the allowed whitelists.
- PRISMA arithmetic: fulltext-screened items all come from the
  abstract-include+borderline set.
- Coded-papers row count equals fulltext-include count.
- Temperature=0 pinned in every Claude API call (regex across
  `abstract_screen.py` and `fulltext_code.py`).
- Top-level `MODEL` and `PROMPT_VERSION` constants match what the
  logs recorded.
- BBT keys in `coded_papers.csv` are non-empty and unique.
- No `decision=error` rows remaining after `--rerun`.
- No "ghost" keys (items in logs but absent from Zotero) — skipped
  cleanly if `pyzotero` or local Zotero unavailable.

**Project-specific scaffolding (commented out, uncomment to enable):**

- Coding-field completeness — list your schema's required field names.
- Forbidden methodology literals in manuscript prose (model names,
  version strings, hand-typed counts).
- Manuscript `@citekey` resolution against `references.bib`.
- `stats.json` freshness vs. `coded_papers.csv` modification time.

Shared `TestRunner` infrastructure (verbose + concise output, exit
code 0/1, unhandled-exception capture) makes customisation low-effort.
Copy the template, uncomment the tests that apply, run
`uv run scripts/test_suite.py`.

`systematic-review` skill updated to point at the new template.

## [0.1.5] — 2026-04-20

### Two new pipeline scripts — first steps toward end-to-end SLR

Ported from the `SLR motivation` reference project, generalised for
plugin use. Both had been referenced in the `systematic-review` skill
but were missing from the plugin.

- **`scripts/pipelines/import_to_zotero.py`** — read a deduplicated
  search CSV, create or update Zotero items with three-layer dedup
  (DOI match → title+first-author match → within-batch dedup). Accepts
  `--group`, `--collection`, `--input`, `--dry-run`; no project-specific
  defaults. Reads API key via `core.config_loader` so the key stays
  out of Claude's context. Prints an explicit `NEXT STEP — run a
  duplicate check` reminder.
- **`scripts/pipelines/export_coded_includes.py`** — filter a
  full-text-screening CSV to the `decision=include` subset with
  last-row-wins semantics on `item_key` (so adjudication flips via
  appended rows take effect). Configurable output columns and
  decision filter (`--decision exclude` useful for PRISMA reporting).
  Pure stdlib, no external deps.
- **Unit tests** — 5 new tests for the export script's filtering,
  last-row-wins, dry-run, column restriction, and alternative-decision
  behaviours.
- **`systematic-review` skill** — stage-to-script table updated to
  include the two new scripts and call out explicitly which scripts
  are still deferred (search, abstract-screen, fulltext-code,
  test-suite template).

54 → 59 default tests. Ruff clean.

### Still deferred (roadmap)

- Search scripts (Scopus / WoS / OpenAlex variants + `search_config.py`
  template).
- Abstract screening (`abstract_screen.py`, Haiku) and full-text
  coding (`fulltext_code.py`, Sonnet) — the biggest lift because
  both require schema-driven prompt templates.
- `test_suite.py` template.
- QA evaluator pattern documentation in `systematic-review` skill.
- Quarto manuscript scaffold + `stats.py` builder pattern.

## [0.1.4] — 2026-04-20

### Live test suite

New opt-in test suite that probes every external service the plugin
talks to: PDF endpoints, abstract endpoints, authentication
workflows. Runs only when explicitly invoked, never automatically,
never in CI.

- **`pytest -m live`** — 23 direct-HTTP tests: 8 PDF (Crossref TDM
  metadata, PMC, Elsevier/ScienceDirect, OpenAlex Content, Springer
  direct, Unpaywall, OpenAlex OA URLs, Wiley TDM), 5 abstract
  (Crossref, Semantic Scholar, Scopus, ScienceDirect, OpenAlex
  GROBID), 10 auth workflows (one per KeySpec, reusing the wizard's
  `_verify_*` helpers so the test exercises the same path the wizard
  uses at setup).
- **`pytest -m live_browser`** — 9 tests parametrized directly from
  `publishers.registry.DEFAULT_PUBLISHERS`. Opens a shared persistent
  Chromium; user solves CF challenge + institutional SSO once per
  publisher domain; assertions use `%PDF-` magic bytes (catches
  HTML-wrapper responses that masquerade as 200 OK).
- **Coverage guard** at `tests/unit/test_live_coverage.py` (runs on
  every default `pytest` invocation). Asserts every registry entry,
  every `KeySpec`, every `fetch_*_pdf`, and every `fetch_from_*` has
  a matching live test. Failing produces an actionable message
  naming the gap. Enforces the "every new service ships with a
  test" project policy.
- **Dependencies are opt-in.** Tests `pytest.importorskip` the
  Python packages they need (`wiley-tdm`, `playwright`,
  `pybliometrics`), so default contributors don't pay the install
  cost. README at `tests/live/README.md` documents the one-line
  install.
- **Known-stable DOIs** in `tests/live/conftest.py` — best-guess
  starting points. Users may need to edit for journals not covered
  by their institutional subscription.

### Numbers

- Default `pytest`: 54 unit tests (was 50). +4 guard tests.
- `pytest -m live`: 23 tests. Each skips cleanly if its key is
  missing.
- `pytest -m live_browser`: 9 tests. `-x` bails at first failure.
- Total with both markers: 86 tests.

## [0.1.3] — 2026-04-20

### UX polish after first real-pipeline run

- **Audit script writes `.keys` files directly.** After
  `audit_zotero_library.py` runs, `/tmp/zotero_audit.missing_abstract.keys`,
  `.missing_pdf.keys`, and `.empty_stubs.keys` land next to the JSON —
  feedable straight to the next pipeline stage's `--filter-keys-file`
  flag. Eliminates the `jq` extraction step (which triggered a
  permission prompt and invited the `empty_stub` vs `empty_stubs`
  singular/plural typo). The script's "Next steps" output now shows
  the exact command to run for each non-empty category.
- **Browser fetcher announces itself.** `fetch_pdfs_browser.py` prints
  a 20-line banner before launching Chromium: what is about to happen,
  which publishers are queued with counts, what the user may be asked
  to do (solve CF challenge, sign in via SSO). No more surprise
  browser windows.
- **Skill-level narration rule.** `zotero-operations` now instructs
  Claude to announce potentially startling stages to the user *before*
  running them (browser fetches, long attach_pdfs runs, first-run uv
  installs).
- **Canonical workflow prose updated** — the skill's "add missing
  abstracts and PDFs" walkthrough drops the `jq` step and references
  the `.keys` files directly.

## [0.1.2] — 2026-04-20

### Security hardening

- **Canonical scripts replace improvised pipeline code.** When Claude
  was asked to "add missing abstracts and PDFs" it composed a Python
  heredoc that read `config.toml` to extract the API key and run a
  library audit. That approach leaks keys through Claude's tool
  context. The fix ships a real audit script
  (`scripts/pipelines/audit_zotero_library.py`) and hardens the
  `zotero-operations` skill to forbid improvisation.
- **Shared config reader** (`scripts/core/config_loader.py`) — all
  pipeline scripts now have a single canonical path to read
  `~/.config/academic-research/config.toml`. Env vars take precedence;
  `require()` raises a clear error if a required value is missing.
- **Broader `permissions.deny` patterns** — wizard now writes deny
  entries for `cat`/`head`/`tail`/`grep`/`less`/`more`/`awk`/`sed`/
  `od`/`xxd`/`strings`/`bat` against both the absolute and tilde-prefix
  form of the config file. Not exhaustive (Python heredocs still
  slip through), so skill-level red flags are the primary defence.
- **Explicit "never read config" red flag** added to every procedural
  skill (`systematic-review`, `zotero-operations`, `fact-check`,
  `critic-loop`).
- **Explicit "never improvise a pipeline script" red flag** added to
  `zotero-operations` and `systematic-review`.

### New functionality

- `scripts/pipelines/audit_zotero_library.py` — classify a library's
  items into have-PDF / missing-PDF / empty-stub / missing-abstract
  categories. Prints summary, writes JSON. Intended to drive
  `fetch_abstracts.py` and `attach_pdfs.py` via their
  `--filter-keys-file` argument.

### uv + PEP 723 inline dependencies

- All pipeline scripts now declare their runtime deps in a PEP 723
  header. `uv run <script>` auto-installs into an ephemeral venv on
  first run — no more `pip install` before use, no system-Python
  pollution.

### Skill updates

- `zotero-operations` — added a canonical "intent → script" table, a
  step-by-step workflow for "add abstracts and PDFs", and forbids
  directory probing or improvised scripts.
- Systematic-review, fact-check, critic-loop, zotero-operations —
  now each have the two new hard-rule red flags above.

## [0.1.1] — 2026-04-20

### Security fix (breaking UX change)

- **`/setup` now launches a terminal wizard** (`scripts/setup/wizard.py`)
  instead of collecting API keys in chat. The previous design asked
  the user to paste API keys into the Claude chat, which would have
  transmitted them to Anthropic's API as part of the user message.
  The wizard reads keys with `getpass` in the user's terminal — keys
  never enter Claude's context.
- The setup skill now detects TTY and either launches the wizard
  in-process (CLI Claude Code) or instructs the user to open a
  terminal (Desktop / Positron / VSCode / headless).
- Wizard is idempotent; re-run to update or add keys.
- Wizard patches `~/.claude/settings.json` with the plugin's
  permission rules (allow `Bash(... ${CLAUDE_PLUGIN_ROOT}/scripts/**)`,
  deny `Read` on the config file). Backs settings.json up before
  mutating.

## [Unreleased]

### critic-loop extensions (deferred from 2026-04-19 prior-art review)

- **Devil's Advocate** as a 5th parallel critic (forces construction of the
  strongest case *against* the manuscript's position). Revisit after seeing
  4-critic loop performance.
- **Traceability matrix** for iteration 2+ — feed each critic a diff since
  its prior iteration plus its own prior unresolved issues, to verify
  substantive fixes rather than cosmetic rewrites.

### Potential improvements (deferred prior-art)

- **Marker** (GPL-3.0) — LLM-assisted PDF extraction for CID-font garbling.
  Integrate via subprocess CLI only (not import) to preserve MIT licensing.
  Candidate fallback in `scripts/core/pdf_extract.py` when both pdfplumber and
  pypdf fail the quality score.
- **paperscraper** (MIT) — Wiley + Elsevier TDM + bioRxiv + PMC BioC-XML.
  Partial overlap with `scripts/pipelines/attach_pdfs.py`; integration would
  require rewriting the orchestration layer. Defer until we have evidence the
  simplification is worth the churn.
- **grobid-client-python**, **semanticscholar** PyPI, **Europe PMC** — minor
  code-quality wins.
- **`/add-publisher`** scaffold skill — generate `publishers/<name>.py` stub
  from DOI prefix + login-required + CF-required inputs.

## [0.1.0] — TBD

Initial public release. See README for the full feature set.
