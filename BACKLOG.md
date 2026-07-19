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
- **R6** — batch tag updates. Status: `--csv-backfill` done in commit
  `9156489`; steady-state `abstract_screen.py` done in this pass.
  `batch_update_tags()` (pyzotero multi-item PATCH) is now fed by a
  main-loop buffer that flushes every `--tag-batch-size` decisions
  (default 50; `1` restores per-item writes), with a final flush in a
  `finally` so Ctrl+C still tags partial progress. CSV is written first,
  so a tag write that never lands is recoverable (a re-run re-screens
  untagged items). **`fulltext_code.py` deliberately stays on per-item
  `update_tags()`**: every `fulltext:include` already requires its own
  child-note PATCH, so batching the tag write there removes no round-trip
  — the note write is the floor. This matches the "show a real bottleneck
  before paying the complexity" caution the steady-state entry carried.

## Tier 2 — shipped in this pass (audit trail)

- **R9** — Zotero MCP `[scite,semantic]` extras. The wizard now installs
  `zotero-mcp-server[scite,semantic]` (and the `skills/setup` doc matches),
  so Scite retraction checks (R5's dependency) and semantic search are
  present by default instead of silently absent.
- **P1** — shared enrich-orchestrator run-log helpers extracted to
  `scripts/pipelines/shared_orchestrators.py` (`open_log`,
  `load_done_keys`, `LogManager`). The three `enrich_*` scripts now
  delegate instead of each re-implementing `_open_log` / `_already_done`
  / `_load_done_dois`.
- **P5** — `searchers/base.py` gained `resolve_credential()` with
  required / optional modes. `wos.py` no longer raises a bare `KeyError`
  from `os.environ[...]`; `semantic_scholar.py` and `scopus.py` route
  through the same helper.
- **P7** — enrich-log column lists moved into `log_schemas.py`
  (`ABSTRACT_FETCH_FIELDS`, `PDF_FETCH_FIELDS`, `DOI_ENRICH_FIELDS`),
  joining the screening/coding schemas already there. Adding a column is
  now a one-file edit.
- **R1 + R2 + R3** — `critic-loop/SKILL.md` gained the Concession
  Threshold Protocol (R1), frame-lock detection (R2), and an explicit
  read-only constraint on critic subagents (R3).

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

- **S6** — stronger deprecation callout for `legacy/` in
  `zotero-operations/SKILL.md`.
  **Closed** — premise no longer holds. A fresh grep of
  `skills/zotero-operations/SKILL.md` returns zero `legacy` or
  `rollback` references; the rollback-mention concern evaporated,
  likely during the portability pass. Left here for audit trail.

---

## Tier 2 — medium impact, medium effort (needs approval)

### Scripts

- **P9** — migrate `test_live_coverage.py` from `legacy/` to
  `fetchers/*.py`.
  **Status: done in the 0.6.0 legacy-deletion pass.** The coverage
  guards now walk the `AbstractFetcher` / `PdfFetcher` subclass tree
  (`_leaf_sources()` in `tests/unit/test_live_coverage.py`) and the
  browser-publisher guard enumerates `fetchers.browser.all_handlers()`.
  A capability diff confirmed nothing in `legacy/` was worth keeping,
  so the whole directory was deleted in the same pass (along with
  `scripts/publishers/registry.py`, whose only remaining consumers
  were the legacy script and the tests now pointed at the handler
  registry). Left here for the audit trail.

---

## Tier 3 — high effort, higher risk; only when touching the area

- **P2** — decompose `enrich_pdfs.py` (1369 LOC) into
  `BrowserOrchestrator` + per-publisher handlers. The `_drive_handler`
  signature takes 8 parameters + callback — that's a class wearing a
  function costume. Hard to test in isolation.
  Files: [scripts/pipelines/enrich_pdfs.py](scripts/pipelines/enrich_pdfs.py).

- **P3** — split `zotero_io.py` (1156 LOC) into `zotero_io_api.py`
  (auth + pyzotero wrapping) and `zotero_io_slr.py`
  (`parse_slr_coding_note`, SLR-specific helpers). The module has
  become a kitchen-sink. Not addressed by the zotero-cli evaluation
  in House-keeping below — that pass added a documentation tier
  rather than restructuring the module; the split is still open.
  Files: [scripts/pipelines/zotero_io.py](scripts/pipelines/zotero_io.py).

- **R10 + R11 (partial)** — finish the OA fallback chain from
  `openags/paper-search-mcp`. PMC and Unpaywall are already live
  ([fetchers/pmc.py](scripts/pipelines/fetchers/pmc.py),
  [fetchers/unpaywall.py](scripts/pipelines/fetchers/unpaywall.py))
  and [fetchers/\_\_init\_\_.py:62](scripts/pipelines/fetchers/__init__.py#L62)
  sketches the cascade order. What remains is **CORE** and
  **Europe PMC** as new `AbstractFetcher` / `PdfFetcher` providers,
  plus an audit of the cascade ordering in `fetchers/__init__.py`.
  Each new provider must ship with a matching file under
  `tests/live/` (enforced by `tests/live/test_live_coverage.py`).
  Files: [scripts/pipelines/fetchers/](scripts/pipelines/fetchers/).

---

## Tier 4 — do if convenient

- **S5** — expand `skills/setup/SKILL.md` (101 lines currently) with
  guidance on rotating a single API key, re-running the wizard, and
  auditing what's already configured. The wizard is idempotent but
  the skill doesn't advertise it.
- **P6 (near-closed)** — standardize on `http_client.get_json()`
  across all fetchers. Only two direct `session.get()` calls remain
  outside `http_client`, both on non-content paths:
  [fetchers/library_resolver.py:297](scripts/pipelines/fetchers/library_resolver.py#L297)
  (SFX resolver probe) and
  [fetchers/browser/connector.py:655](scripts/pipelines/fetchers/browser/connector.py#L655)
  (connector ping). Not worth a dedicated pass; tidy opportunistically
  if touching those files.
- **P8** — CI guard for `--legacy-browser` ↔ `legacy/` coherence.
  **Closed as moot in the 0.6.0 legacy-deletion pass** — the flag and
  the directory were removed together (see P9), so there is nothing
  left to keep coherent. Left here for the audit trail.
- **R4** — IRON RULE tables in long SKILL.mds
  (`systematic-review/SKILL.md` is >700 lines). Anti-pattern / Why
  it fails / Correct behaviour rows as an anti-context-rot device.
- **R7 (narrowed)** — port `find_duplicates` detection into
  `audit_zotero_library.py` so the audit report surfaces duplicate
  candidates offline. The merge half is already ported —
  [zotero_io.py:1008](scripts/pipelines/zotero_io.py#L1008)
  (`merge_duplicate_item`, adapted from zotero-mcp) — and stays
  pipeline-only: `scripts/pipelines/fetchers/browser/connector.py`
  calls it directly and depends on its structured return stats, so
  it is not a candidate for replacement by the `zotero-cli` tier
  added below (no equivalent structured output from a shelled-out
  CLI call). The find-duplicates doctrine is already wired into
  [zotero-operations/SKILL.md](skills/zotero-operations/SKILL.md)
  under "Import dedup". What remains is detection in the audit
  script itself (MCP find/merge still can't be invoked from a
  headless script, but the detection algorithm can be reimplemented
  locally).
- **R8** — `mcp__zotero__zotero_get_pdf_outline` in `fulltext_code.py`.
  Jump to coding-relevant sections without reading the whole PDF.
  Requires restructuring the LLM-input pipeline (currently sends the
  whole PDF up to a soft cap). Tier 3 work in practice.
- **S7** — add missing `Trigger phrases:` blocks to three skills.
  CLAUDE.md says every procedural skill follows the shape
  "Use when … + Trigger phrases: … + Do NOT use for X". The
  description lines in
  [skills/academic-style/SKILL.md](skills/academic-style/SKILL.md),
  [skills/empirical-integrity/SKILL.md](skills/empirical-integrity/SKILL.md),
  and [skills/setup/SKILL.md](skills/setup/SKILL.md) lack the
  `Trigger phrases:` block. Breaking the shape risks wrong-skill
  triggering. One-line description edit per skill; no body changes
  needed.
- **P10** — drop the legacy-layout branch from the
  `test_systematic_review.py` template. The template defines
  `ABSTRACT_SCRIPT` / `FULLTEXT_SCRIPT` paths and Test 8 silently
  passes if neither local copy exists ([templates/test_systematic_review.py:58-65](templates/test_systematic_review.py#L58-L65),
  [:160-170](templates/test_systematic_review.py#L160-L170)). Now
  that SR projects invoke plugin scripts by path, the silent-pass
  branch adds cognitive load without catching anything. Delete the
  branch or assert-fail if a local copy is found (indicating an
  outdated project layout).
- **M1** — add `keywords` to `.claude-plugin/plugin.json` for
  marketplace search. The manifest currently has only `name`,
  `version`, `description`, `author`, `license`, `homepage`. An
  array like `["systematic-review", "zotero", "citations",
  "manuscript", "critic-loop", "academic"]` would improve
  discoverability in `/plugin marketplace`.

---

## House-keeping

- **`REVIEW_NOTES.md`** — deleted in this backlog-review pass.
  The file was v0.1.0 scratch material referencing skills that no
  longer exist by those names (`mcp-research`, `academic-writing`);
  it was already gitignored (line 41 of `.gitignore`) and never
  tracked, so the delete is local-only — no follow-up needed.

- **`zotero-cli` evaluation (2026-07-19)** — the upstream
  `zotero-mcp-server` package (the wizard already installs it, see
  `EXPECTED_MCP` in `scripts/setup/wizard.py`) ships a standalone
  `zotero-cli` as of v0.6.2. Evaluated as a replacement for
  `zotero_io.py`'s pyzotero wrapping; **rejected for batch pipelines,
  adopted for one-off agent operations**. Measured `zotero-cli
  config` startup at ~1.5–2 s per invocation (fresh Python process
  each call); it also has no batch-by-item-keys mode, no reliable
  `--json` output, and no HTTP-412 version-conflict retry — a
  library-wide screening/coding run touches hundreds to thousands of
  items and needs all three. `zotero_io.py` and `bbt_client.py`
  remain the pipeline-facing layer. `zotero-cli` was added as tier 2
  in the Zotero access hierarchy (`zotero-operations/SKILL.md`'s
  IRON RULE, mirrored in `systematic-review/SKILL.md`) for
  agent-initiated one-off writes — edits, tag/note mutations,
  dedup-find, single-item add — replacing what would otherwise be
  improvised inline Python or a defect-signal direct-HTTP call. The
  setup wizard gained a PATH presence check (`_check_zotero_cli`)
  that also detects the stale PyPI package `zotero-mcp` (0.1.6,
  pre-dates the CLI) shadowing the real one, plus a read-only
  permission-allow category for `zotero-cli search/get/config/
  duplicates find`. `merge_duplicate_item` was considered for
  deletion in favor of `zotero-cli duplicates merge` but kept — see
  R7 (narrowed) above; it is a load-bearing pipeline call, not an
  agent-facing convenience.
