# Backlog — claude-academic-research

Deferred development ideas. Items live here instead of in
`/Users/mronkko/.claude/plans/` so they survive across sessions and
travel with the repo. Originating source for most items: a critical
review produced during the 0.5.0 cycle (kept in the author's plans
directory — not checked in because it references machine-local paths).

**Conventions:**

- Items are grouped by the critical review's tier framework
  (Tier 1 = high impact, low effort; Tier 2 = medium / medium; Tier 3
  = big refactor, only when touching the area anyway; Tier 4 = nice
  to have).
- Each entry includes **Why deferred** + **What it would take**
  + **Files to look at**. Enough context to pick up cold in a later
  session.
- Tier 1 items are **done** — left here for the audit trail. See
  each entry's "Status" line.
- Before promoting a Tier 2/3 item, confirm it is still relevant
  (the codebase moves).

---

## Tier 1 — shipped in 0.5.0 (audit trail)

- **S1** — `fact-check` ↔ `critic-loop` mutual-exclusion rule.
  Status: done in commit `4ea1af3`. Both skills cross-reference each
  other with an explicit delegation clause; prevents duplicate
  citation verification when a user runs both on the same draft.
- **S2** — `zotero-operations` ↔ `systematic-review` enrichment
  boundary. Status: done in commit `16e81a8`. New "who owns
  enrichment" section in `zotero-operations/SKILL.md`.
- **R5** — Scite retraction-check doctrine. Status: done in commit
  `af3022d`. Skill-level wiring (MCP can't be called from headless
  scripts). Both `zotero-operations` and `systematic-review` gained
  a post-audit / post-coding retraction-check step.
- **R6 (partial)** — batch tag updates in `--csv-backfill`. Status:
  done in commit `9156489`. New `zotero_io.batch_update_tags()` using
  pyzotero's `update_items` multi-item PATCH. Wired into both
  `--csv-backfill` paths. **Steady-state parallel screening path
  deliberately left on per-item `update_tags()` with tenacity retry
  — see Tier 2 below for the remaining scope.**

## Re-evaluation candidates

- **P4** — drop `.claude/` from the default `--output` in
  `audit_zotero_library.py`.
  **Skipped** during Tier 1 because it directly conflicts with the
  intent of commit `0221509` (portability refactor 2026-04-23), which
  explicitly chose `.claude/<scope>/` paths for (a) cross-platform
  support and (b) auto-gitignore by the setup wizard. Re-evaluate
  only if end-user testing surfaces a concrete CI / standalone-runner
  pain caused by the `.claude/` default — otherwise keep as-is.
  Files: [scripts/pipelines/audit_zotero_library.py:118](scripts/pipelines/audit_zotero_library.py#L118).

---

## Tier 2 — medium impact, medium effort (needs approval)

### Skills

- **S3** — add "companion skills" cross-links for
  `critic-loop ↔ empirical-integrity`.
  **Why deferred:** the loop's Step 1 test gate invokes
  `test_empirical_integrity.py`, but the skill body doesn't say so.
  Tier 1 pass scoped this out to avoid drifting off S1.
  **What it would take:** ~10 lines in `skills/critic-loop/SKILL.md`
  — a "Companion skills" section enumerating the dependency on
  `empirical-integrity` (test gate), `grounded-citations` (write-time
  rule-book), and `manuscript-revision` (doctrine).
  Files: [skills/critic-loop/SKILL.md](skills/critic-loop/SKILL.md).

- **S4** — cross-link `academic-style` and `manuscript-revision`.
  **Why deferred:** they're meant to be used in sequence (clean
  draft → critic-loop revision) but neither mentions the other.
  Users who skip `academic-style` incur extra critic iterations.
  **What it would take:** one short paragraph in each SKILL.md
  naming the companion.
  Files: [skills/academic-style/SKILL.md](skills/academic-style/SKILL.md), [skills/manuscript-revision/SKILL.md](skills/manuscript-revision/SKILL.md).

- **R9** — document `zotero-mcp-server[scite,semantic]` optional
  extras in the setup wizard.
  **Why deferred:** users install the base package and never discover
  semantic search or retraction alerts. R5 (done) depends on Scite
  being available — the wizard could install the extra automatically
  or at least surface the option.
  **What it would take:** one pass through `scripts/setup/wizard.py`
  at the MCP-registration step; add a prompt or auto-enable the
  extras. Tests: extend `tests/unit/test_setup_wizard.py`.
  Files: [scripts/setup/wizard.py](scripts/setup/wizard.py).

### Scripts

- **P1** — extract shared `LogManager` / `ConfigLoader` for the
  three `enrich_*` orchestrators.
  **Why deferred:** config-loading, CSV-log initialization, and
  "already-done" tracking are reimplemented in each of
  `enrich_abstracts.py`, `enrich_pdfs.py`, `enrich_dois.py`. Adding
  a field to the log means editing three files in sync.
  **What it would take:** new
  `scripts/pipelines/shared_orchestrators.py` with `LogManager`
  class (append-only CSV with last-row-wins reduction) and
  `ConfigLoader` helper. Refactor the three scripts to use it.
  Existing tests should drive this — no behaviour change.
  Files: [scripts/pipelines/enrich_abstracts.py](scripts/pipelines/enrich_abstracts.py), [scripts/pipelines/enrich_pdfs.py](scripts/pipelines/enrich_pdfs.py), [scripts/pipelines/enrich_dois.py](scripts/pipelines/enrich_dois.py).

- **P5** — shared credential-check helper for searchers.
  **Why deferred:** each of `scopus.py`, `wos.py`, `openalex.py`,
  `semantic_scholar.py` re-implements "API key missing → raise"
  logic with inconsistent error messages.
  **What it would take:** add `require_config_key()` helper or
  `@requires_credential` decorator to `searchers/base.py`; refactor
  each searcher to use it.
  Files: [scripts/pipelines/searchers/base.py](scripts/pipelines/searchers/base.py), [scripts/pipelines/searchers/](scripts/pipelines/searchers/).

- **P7** — shared log-CSV schemas.
  **Why deferred:** log schemas are defined inline in each orchestrator
  but downstream templates (e.g. `test_systematic_review.py`) expect
  specific columns. Adding a column risks silent template drift.
  **What it would take:** new
  `scripts/pipelines/log_schemas.py` with
  `ABSTRACT_LOG_FIELDS`, `FULLTEXT_LOG_FIELDS`, etc. Imported by
  orchestrators and templates. Consider generating the test template's
  `LOG_FIELDS` assertion from the same source.
  Files: [scripts/pipelines/](scripts/pipelines/), [templates/](templates/).

- **R6 (steady-state)** — batch tag writes during parallel screening.
  **Why deferred:** the Tier 1 pass landed batch writes on the
  `--csv-backfill` path only. Steady-state parallel workers still
  call per-item `update_tags()` after each decision. Benefit of
  batching: fewer API calls, lower 412-retry pressure. Cost:
  threading complexity (shared buffer, flusher thread, clean
  shutdown on Ctrl+C, partial-batch error handling). Tenacity
  retries already handle 412s correctly; benchmarks would need to
  show a real bottleneck before paying the complexity.
  **What it would take:** add a buffered-flusher class that takes
  decisions from the ThreadPoolExecutor completion loop and calls
  `batch_update_tags()` every N items or on exit. Atexit handler
  for partial flush on SIGINT. Thread-safety tests.
  Files: [scripts/pipelines/abstract_screen.py](scripts/pipelines/abstract_screen.py), [scripts/pipelines/fulltext_code.py](scripts/pipelines/fulltext_code.py), [scripts/pipelines/zotero_io.py](scripts/pipelines/zotero_io.py).

### Reference-project adoptions

- **R1 + R2 + R3** — Concession Threshold Protocol, frame-lock
  detection, and explicit read-only constraint on critic subagents
  (from `Imbad0202/academic-research-skills`).
  **Why deferred:** R1 directly targets the sycophancy failure mode
  our four-critic loop is vulnerable to. R2 is a one-line rule. R3
  formalizes behaviour we already rely on.
  **What it would take:** ~50 lines of prose in
  `skills/critic-loop/SKILL.md` — one new section per item.
  Files: [skills/critic-loop/SKILL.md](skills/critic-loop/SKILL.md).

---

## Tier 3 — high effort, higher risk; only when touching the area

- **P2** — decompose `enrich_pdfs.py` (1369 LOC) into
  `BrowserOrchestrator` + per-publisher handlers. The `_drive_handler`
  signature takes 8 parameters + callback — that's a class wearing a
  function costume. Hard to test in isolation.
  Files: [scripts/pipelines/enrich_pdfs.py](scripts/pipelines/enrich_pdfs.py).

- **P3** — split `zotero_io.py` (now ~1100 LOC after the 0.5.0 tag
  + note work) into `zotero_io_api.py` (auth + pyzotero wrapping)
  and `zotero_io_slr.py` (`parse_slr_coding_note`, SLR-specific
  helpers). The module has become a kitchen-sink.
  Files: [scripts/pipelines/zotero_io.py](scripts/pipelines/zotero_io.py).

- **R10 + R11** — adopt the OA fallback chain from
  `openags/paper-search-mcp` (source-native → OpenAIRE / Europe PMC
  / PMC → Unpaywall → optional Sci-Hub) and add CORE / Europe PMC
  / PMC as new `AbstractFetcher` / `PdfFetcher` providers. Each new
  provider must ship with a matching file under `tests/live/`
  (enforced by `tests/live/test_live_coverage.py`).
  Files: [scripts/pipelines/fetchers/](scripts/pipelines/fetchers/).

---

## Tier 4 — do if convenient

- **S5** — expand `skills/setup/SKILL.md` (~50 lines currently) with
  guidance on rotating a single API key, re-running the wizard, and
  auditing what's already configured. The wizard is idempotent but
  the skill doesn't advertise it.
- **S6** — stronger deprecation callout for `legacy/` in
  `zotero-operations/SKILL.md`. The rollback mention shouldn't read
  as a first-class option.
- **P6** — standardize on `http_client.get_json()` across all
  fetchers. Currently some fetchers call `session.get()` directly
  and parse ad-hoc. Hard to add a global rate-limit or retry policy.
  Files: [scripts/pipelines/fetchers/](scripts/pipelines/fetchers/), [scripts/pipelines/http_client.py](scripts/pipelines/http_client.py).
- **P8** — CI guard that fails if `--legacy-browser` flag is removed
  but `legacy/` directory still exists, or vice versa. Currently a
  four-item checklist in `legacy/README.md` that nothing enforces.
- **R4** — IRON RULE tables in long SKILL.mds
  (`systematic-review/SKILL.md` is >700 lines). Anti-pattern / Why
  it fails / Correct behaviour rows as an anti-context-rot device.
- **R7** — `mcp__zotero__zotero_find_duplicates` +
  `mcp__zotero__zotero_merge_duplicates` in
  `audit_zotero_library.py` and post-import. MCP offers a dry-run
  preview we currently improvise. Same MCP-from-headless-script
  constraint as R5 — would be skill doctrine, not script code.
- **R8** — `mcp__zotero__zotero_get_pdf_outline` in `fulltext_code.py`.
  Jump to coding-relevant sections without reading the whole PDF.
  Requires restructuring the LLM-input pipeline (currently sends the
  whole PDF up to a soft cap). Tier 3 work in practice.

---

## House-keeping

- **`REVIEW_NOTES.md`** at repo root is stale v0.1.0 material —
  marked "gitignored, delete after review" in its own header but
  still tracked. Either remove it (obsolete items resolved in later
  releases) or replace with a "historical" header.
