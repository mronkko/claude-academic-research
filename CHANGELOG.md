# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
