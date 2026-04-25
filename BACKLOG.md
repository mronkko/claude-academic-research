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

- **S6** — stronger deprecation callout for `legacy/` in
  `zotero-operations/SKILL.md`.
  **Closed** — premise no longer holds. A fresh grep of
  `skills/zotero-operations/SKILL.md` returns zero `legacy` or
  `rollback` references; the rollback-mention concern evaporated,
  likely during the portability pass. Left here for audit trail.

---

## Tier 2 — medium impact, medium effort (needs approval)

### Skills

- **S3** — add an explicit "Companion skills" section to
  `critic-loop`.
  **Why deferred:** the skill body already references
  `empirical-integrity` in prose (five mentions in
  `skills/critic-loop/SKILL.md` — e.g. line 141 on the Step 1 test
  gate), but there is no dedicated structural section anchoring the
  companion relationships. Tier 1 pass scoped this out to avoid
  drifting off S1.
  **What it would take:** ~10 lines in `skills/critic-loop/SKILL.md`
  — a "Companion skills" section enumerating the dependency on
  `empirical-integrity` (test gate), `grounded-citations` (write-time
  rule-book), and `manuscript-revision` (doctrine).
  Files: [skills/critic-loop/SKILL.md](skills/critic-loop/SKILL.md).

- **S4** — add reverse cross-link from `manuscript-revision` to
  `academic-style`.
  **Why deferred:** `academic-style/SKILL.md:3,22` already delegates
  to `manuscript-revision`; only the reverse direction is missing.
  Users who skip `academic-style` incur extra critic iterations.
  **What it would take:** one short paragraph in
  `skills/manuscript-revision/SKILL.md` naming `academic-style` as
  the "before the loop" companion.
  Files: [skills/manuscript-revision/SKILL.md](skills/manuscript-revision/SKILL.md).

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

- **P1** — extract shared `LogManager` for the three `enrich_*`
  orchestrators.
  **Why deferred:** `core.config_loader` already handles the
  config-loading side (all three scripts import `get` / `require`
  from it). What remains reimplemented in each of
  `enrich_abstracts.py`, `enrich_pdfs.py`, `enrich_dois.py` is
  CSV-log initialization (`_open_log`), "already-done" tracking
  (`_already_done` / `_load_done_dois` — note the status-filter
  string differs: `"updated"` vs `"attached"`), and the per-file
  `LOG_FIELDS` list. Adding a field to the log still means editing
  three files in sync.
  **What it would take:** new
  `scripts/pipelines/shared_orchestrators.py` with a `LogManager`
  class (append-only CSV with last-row-wins reduction, parametric
  status filter). Refactor the three scripts to use it. Existing
  tests should drive this — no behaviour change.
  Files: [scripts/pipelines/enrich_abstracts.py](scripts/pipelines/enrich_abstracts.py), [scripts/pipelines/enrich_pdfs.py](scripts/pipelines/enrich_pdfs.py), [scripts/pipelines/enrich_dois.py](scripts/pipelines/enrich_dois.py).

- **P5** — shared credential-check helper for searchers.
  **Why deferred:** each of `scopus.py`, `wos.py`, `openalex.py`,
  `semantic_scholar.py` re-implements "API key missing → raise"
  logic with inconsistent error regimes. Concrete cases to cover:
  `wos.py` uses `os.environ["WOS_API_KEY_EXTENDED"]` (raises bare
  `KeyError` with a useless message); `semantic_scholar.py` uses
  `os.environ.get(..., "")` (silently empties — the API accepts
  anon calls with lower quota); `openalex.py` doesn't need a key.
  The helper must accommodate "required", "optional", and
  "unauthenticated-allowed" modes.
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

- **P12** — Setup wizard paste-in command breaks when two plugin
  versions are cached side-by-side.
  **Why deferred:** ergonomic failure on a happy-path command — it
  bites whenever Claude Code keeps an older plugin version cached
  alongside the new one (common after `/plugin marketplace
  upgrade`). The skill emits this paste-in line:

      python3 ~/.claude/plugins/cache/mronkko/academic-research/*/scripts/setup/wizard.py

  The literal `*` is a shell glob; with two versions present it
  expands to two paths and `python3` aborts with "can't open file:
  ambiguous arguments". Surfaced in the real-session log
  (gitignored `logs/b1ecdc14-c827-414a-a772-7050633ffc7b.jsonl`
  line 4 — IDE-selected by the user when reporting this).
  Three call sites use the broken pattern:
  [skills/setup/SKILL.md:13](skills/setup/SKILL.md#L13) (Wizard
  path callout), [skills/setup/SKILL.md:42](skills/setup/SKILL.md#L42)
  (the literal pasted command), and
  [scripts/core/config_loader.py:61](scripts/core/config_loader.py#L61)
  (runtime error message inside `require()`).
  **What it would take:**
  1. Skill prose: replace the glob with
     `${CLAUDE_PLUGIN_ROOT}/scripts/setup/wizard.py`. Claude Code
     resolves `${CLAUDE_PLUGIN_ROOT}` to the active version's
     absolute path before the model emits text, so the user pastes
     a concrete path. This is already the canonical pattern in
     CLAUDE.md and is allow-listed under
     `Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/**)`.
  2. `config_loader.py:require()`: `${CLAUDE_PLUGIN_ROOT}` is
     unresolved at user-terminal time, so the same trick won't
     work. Compute the wizard path from `__file__` instead:
     `(Path(__file__).resolve().parent.parent / "setup" /
     "wizard.py")`. Points to the same version currently running,
     by construction.
  3. Regression guard: a unit test that greps `skills/setup/SKILL.md`
     for stray `*` globs in fenced code blocks. One assertion —
     prevents future drift back to the broken pattern.
  Files: [skills/setup/SKILL.md](skills/setup/SKILL.md), [scripts/core/config_loader.py](scripts/core/config_loader.py), [tests/unit/](tests/unit/).

- **P11** — Elsevier ScienceDirect PDF fetcher silently caches
  1-page previews when entitlement is partial.
  **Why deferred:** silent correctness failure, not a crash —
  surfaced only because the user noticed 39 papers with <10K
  extracted chars after full-text coding had already started.
  [fetchers/sciencedirect.py:106](scripts/pipelines/fetchers/sciencedirect.py#L106)
  validates only `status_code == 200` and `%PDF` magic bytes;
  Elsevier signals partial entitlement via the response header
  `x-els-status: WARNING - Response limited to first page because
  requestor not entitled to resource`, which the fetcher receives
  but never inspects. The 1-page preview is a valid PDF and passes
  both checks, so it gets cached and propagates to coding. JYU's
  TDM license grants this **per-article**, not per-journal — same
  journal/year mixes entitled and non-entitled papers, so a
  prefix/journal denylist is not a fix.
  **Evidence in real-session log** (gitignored
  `logs/b1ecdc14-c827-414a-a772-7050633ffc7b.jsonl`): the warning
  header appears 8 times — diagnostic message at line 1742,
  empirical comparison of PDF vs XML endpoints at lines 1760-1770
  (preview returns SIZE=234516 / Pages=1 with WARNING; XML returns
  full body with `x-els-status: OK`), root-cause writeup at
  lines 2339-2340, and the session-memory summary at line 2931.
  The user's improvised remediation (downstream
  `scripts/fetch_elsevier_xml_pdfs.py`, not in this repo) confirms
  the fix shape — but per the standing "no improvised pipeline
  code" rule it must be lifted into the plugin.
  **What it would take:**
  1. In `ScienceDirectSource.fetch_pdf`, after the `resp` check,
     reject the PDF when `resp.headers.get("x-els-status",
     "").startswith("WARNING")`. Fall through, do not cache.
  2. Add an XML fallback at the same URL with
     `Accept: text/xml`. On `x-els-status: OK`, parse the
     `<body>` element, render a text-only archival PDF via
     `reportlab`, cache that. Document the provenance in the
     cache filename or sidecar so the audit script can tell a
     real PDF from a TDM-recovered one.
  3. Distinguish "preview blocked, XML also empty" (truly
     unrecoverable → FE6) from "PDF preview but XML succeeded"
     (recovered) so
     [audit_zotero_library.py](scripts/pipelines/audit_zotero_library.py)
     can flag the former before coding starts rather than after.
  4. Live test under `tests/live/` using one known-blocked Elsevier
     DOI (per the "every source has a live test" rule). The XML
     fallback path also needs its own live coverage entry.
  Files: [scripts/pipelines/fetchers/sciencedirect.py](scripts/pipelines/fetchers/sciencedirect.py), [scripts/pipelines/audit_zotero_library.py](scripts/pipelines/audit_zotero_library.py), [tests/live/](tests/live/).

- **P9** — migrate `test_live_coverage.py` from `legacy/` to
  `fetchers/*.py`.
  **Why deferred:** the live-coverage guard currently walks
  `legacy/fetch_abstracts.py` and `legacy/attach_pdfs.py` for the
  canonical list of sources ([tests/unit/test_live_coverage.py:103-110](tests/unit/test_live_coverage.py#L103-L110)).
  The docstring itself flags this: "When the refactored `fetchers/*.py`
  classes become the coverage source of truth, this function … should
  walk them instead — then the legacy/ directory can be deleted."
  This is load-bearing for any future `legacy/` cleanup: as long as
  the guard reads from `legacy/`, we can't remove those scripts.
  **What it would take:** rewrite the two guard functions to walk
  `scripts/pipelines/fetchers/*.py` and enumerate
  `AbstractFetcher` / `PdfFetcher` subclasses. Preserve the alias
  mapping, or map subclass `name` attributes to test names. Keep the
  test coverage strictness unchanged.
  Files: [tests/unit/test_live_coverage.py](tests/unit/test_live_coverage.py).

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

- **P3** — split `zotero_io.py` (978 LOC) into `zotero_io_api.py`
  (auth + pyzotero wrapping) and `zotero_io_slr.py`
  (`parse_slr_coding_note`, SLR-specific helpers). The module has
  become a kitchen-sink.
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
- **P8** — CI guard that fails if `--legacy-browser` flag is removed
  but `legacy/` directory still exists, or vice versa. Currently a
  four-item checklist in `legacy/README.md` that nothing enforces.
- **R4** — IRON RULE tables in long SKILL.mds
  (`systematic-review/SKILL.md` is >700 lines). Anti-pattern / Why
  it fails / Correct behaviour rows as an anti-context-rot device.
- **R7 (narrowed)** — port `find_duplicates` detection into
  `audit_zotero_library.py` so the audit report surfaces duplicate
  candidates offline. The merge half is already ported —
  [zotero_io.py:830](scripts/pipelines/zotero_io.py#L830)
  (`merge_duplicate_item`, adapted from zotero-mcp) — and the
  find-duplicates doctrine is already wired into
  [zotero-operations/SKILL.md:240](skills/zotero-operations/SKILL.md).
  What remains is detection in the audit script itself (MCP
  find/merge still can't be invoked from a headless script, but the
  detection algorithm can be reimplemented locally).
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
