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
  `load_done_keys`). The three `enrich_*` scripts now
  delegate instead of each re-implementing `_open_log` / `_already_done`
  / `_load_done_dois`.
  **Amended (chore/zotero-mcp-overlap):** the extraction also shipped a
  `LogManager` class bundling the handle, a write lock, and the resume
  lookup. No orchestrator ever adopted it — `enrich_pdfs` opens its log
  across several phases and owns its own lock, and the other two want
  only the two functions — so it was carrying three tests and no
  callers. Deleted. Don't re-add one without a caller; the module
  docstring says so too.
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

- **S10** — C3, replacing `_row_to_zotero_item`'s hand-rolled mapping
  with `zotero_mcp.citation_import.csl_json_to_zotero` (part of the
  batch-DOI-import work; C1 `_normalize_doi` and C2
  `schema.valid_fields` pre-POST validation shipped, C3 did not).
  **Skipped** — the plan's own escape hatch ("if the test churn
  outweighs the gain, take only C1+C2") applied. Reasons: (1)
  `csl_json_to_zotero` takes CSL-JSON, not our CSV's flat row shape,
  so adopting it means *adding* a CSV→CSL adapter, not net-removing a
  mapper. (2) Its main sophistication — CSL type→itemType dispatch,
  multipart dates, per-type container-field selection — is moot here:
  every search-CSV row is already a known `journalArticle` with a
  plain year. (3) `template_fn` needs a live Zotero item-template GET,
  which would put a network dependency into a path `main()` currently
  runs even under `--dry-run`. (4) It touches a shipped path covered
  by 15 tests in `test_import_to_zotero_canonicalize.py` for
  uncertain gain. The one piece that might still be worth lifting in
  isolation someday: `_looks_corporate` (`citation_import.py:118`),
  a small self-contained corporate-author-name heuristic
  `_parse_authors` (`import_to_zotero.py`) doesn't have — but search
  results are almost always individual authors, so this is low
  priority. Re-evaluate only if corporate-author mis-parsing shows up
  as a real dedup/data-quality issue in practice.
  Files: [scripts/pipelines/import_to_zotero.py](scripts/pipelines/import_to_zotero.py)
  (`_row_to_zotero_item`, `_parse_authors`).

- **S9** — file-format/architecture question for a DOI-resolve →
  Zotero-import handoff (`resolve_dois.py` → `import_to_zotero.py`).
  **Closed** — moot. `resolve_dois.py` was dropped during the
  batch-DOI-import work (see S10 above): investigating the zotero-mcp
  checkout found the 244-DOI crash was a zotero-mcp defect fixable at
  source (upstream `feat/batch-doi-add`), not something needing a new
  Zotero-import entry point. No CSV-vs-JSON handoff to design if
  there's no new script writing one side of it.

- **S11** — stray editable `zotero-mcp-server` install on this
  machine's system Python, pointing at a *different* local checkout
  (`~/Documents/GitHub/zotero-mcp-antigravity`, version 0.6.2 —
  pre-dates the 0.9 tool-surface rename). It shadows the correct
  `>=0.9,<0.10` package this plugin now requires, so
  `tests/unit/test_zotero_mcp_sync.py` (and anything else importing
  `zotero_mcp`) fails under plain `pytest` on PATH, even though it
  passes cleanly under this project's own `.venv` (`uv sync`-managed,
  isolated).
  **Resolved (2026-08-12).** Investigation found the target directory
  had already been renamed away (it was a linked git worktree of the
  `~/Documents/GitHub/zotero-mcp` repo, physically renamed to
  `zotero-mcp-indexing` outside of `git worktree move`), so the
  editable install was fully dangling — `import zotero_mcp` under
  system Python raised `ModuleNotFoundError` outright, and its
  `zotero-mcp`/`zotero-cli` console scripts in system Python's `bin/`
  were already shadowed on `PATH` by a separate, correctly-targeted
  `uv tool install --editable ~/Documents/GitHub/zotero-mcp` (the
  live checkout `zotero-mcp` this plugin actually depends on — see
  CLAUDE.md's Reference projects section). No MCP config (Claude
  Desktop, `~/.claude.json`) referenced the dangling path or the
  system-Python interpreter. Confirmed by user, then removed via
  `pip uninstall zotero-mcp-server` under
  `/Library/Frameworks/Python.framework/Versions/3.12`; this repo's
  own suite still passes under `uv run pytest`. Still true generally,
  and worth keeping as house style: invoke this repo's tests via
  `.venv/bin/pytest` / `uv run pytest`, not a bare `pytest` on `PATH`.

---

## Cleanup pass — `chore/zotero-mcp-overlap` (2026-08-12)

A review for dead code, overlap with the `mronkko/zotero-mcp` fork, and
refactor candidates. Most of the suspected overlap turned out to be
already correctly resolved (`merge_duplicate_item` per R7, `bbt_client`'s
stdlib-only constraint, `generate_bib` vs `zotero_export_bibliography`,
S10's `csl_json_to_zotero` reasoning) — but it surfaced two live defects.

- **Two BBT defects, both silent, both in `bbt_client` / `zotero_io`.**
  1. `get_bbt_keys` called `item.citationkey` with `{"keys": [...]}`.
     BBT validates named params against the handler signature
     (`async citationkey(item_keys)`) and answers `-32602 unsupported
     argument`. The error body has no `result`, so the method returned
     `{}` and `populate_missing_bbt_keys` reported *every* item as
     unkeyed. zotero-mcp hit the same bug (its #293).
  2. `get_bibtex_export` built
     `/better-bibtex/library/<id>/library.bibtex`. The real path is
     `/better-bibtex/export/library?/<id>/library.bibtex` — both the
     `export` segment and the literal `?` matter. HTTP 404 otherwise.

  Neither was reachable by the unit tests, which mock the transport; one
  unit test actively *pinned* the wrong URL. Both are fixed, and
  `tests/live/test_zotero_io_bbt.py` now exercises the real endpoints —
  that live test is what found defect 2. **Lesson worth keeping: for
  local-service integrations, a mocked test can only pin whatever shape
  we already believed.**

- **Two sources of truth for test dependencies, drifted.**
  `pyproject.toml`'s dev group omitted `tenacity` while `ci.yml` carried
  a hand-written pip line that included it, so `uv sync` produced an
  environment where 14 test modules failed to collect while CI stayed
  green. CI now runs `pip install --group dev` (PEP 735) and
  `tests/unit/test_ci_dependencies.py` fails the build if a hand-written
  list comes back. `anthropic` and `google-genai` were in that pip line
  and are not needed by the suite at all — dropped. `pyzotero` / `httpx`
  / `requests` were arriving only transitively via `zotero-mcp-server`
  and are now explicit. `reportlab` was added: without it one test in
  `test_sciencedirect_preview_fallback.py` silently skipped on every run.

- **`>=0.9` was written out six times.** Now
  `scripts/setup/zotero_mcp_floor.py`, imported by
  `check_zotero_mcp_version.py` and `wizard.py`, with
  `tests/unit/test_zotero_mcp_floor.py` asserting it matches the
  `pyproject.toml` pin and that no install string has drifted back to a
  literal. `pyproject.toml` stays the declarative pin because
  `test_zotero_mcp_sync.py`'s E3 regex-matches the literal there.

- **Three DOI normalizers collapsed** into `scripts/pipelines/doi_utils.py`
  (`normalize_doi` strict, `strip_doi_prefixes` lenient, plus `doi_key` /
  `doi_cache_key`). `doi_resolver.py` had a comment conceding its copy was
  "kept in sync rather than imported". **The strict/lenient split is
  load-bearing — do not flatten it:** `import_to_zotero` needs identity
  semantics (a malformed DOI must match nothing), `doi_resolver` needs a
  cache key (collapsing malformed DOIs to `""` makes them share one entry
  and serve each other's cached lookups), and `enrich_dois --fix-malformed`
  needs the cleaned string plus a "changed" flag. This also removed
  `from zotero_mcp.tools._helpers import _normalize_doi` — a private symbol
  from a private module of an external package.

- **`screening_common.py` extracted** from `abstract_screen.py` /
  `fulltext_code.py` (config loading, stage-tag matching, CSV decision
  reading, `--csv-backfill`), with `search.py` as a third consumer of the
  config loader. Each orchestrator keeps a thin wrapper binding its own
  constants, so both stages' tests passed unchanged. **One difference is
  deliberately not abstracted:** abstract screening filters CSV decisions
  while reading (a trailing `error` row does not displace an earlier valid
  decision), full-text coding filters after (it does). See the module
  docstring.

- **Upstream PR: 54yyyu/zotero-mcp#445.** `zotero_mcp/__init__.py` did
  `from .server import mcp` eagerly, so `from zotero_mcp.schema import
  valid_fields` cost ~1.73 s and ~1480 modules — for a stdlib-only lookup
  that takes 8 µs. `import_to_zotero.py` paid that on every run and had to
  declare the whole server dependency tree in its PEP 723 block. The PR
  makes `mcp` lazy (PEP 562 `__getattr__`; ~9 ms / 75 modules after) and
  promotes `_normalize_doi` to a public stdlib-only
  `zotero_mcp.identifiers.normalize_doi`.
  **Follow-up, deliberately not done here:** once that merges and a
  release ships, raise this repo's floor in
  `scripts/setup/zotero_mcp_floor.py` + `pyproject.toml`, and consider
  dropping the vendored `doi_utils.normalize_doi` in favour of the
  upstream one. Until a release lands, PyPI 0.9.1 still has the eager
  import, so nothing here changes.

- **Model selection is now a flag, not a config edit** (`--model` on both
  screening scripts, aliases in `scripts/core/models.py`). Prompted by the
  observation that "screen these with Haiku" previously forced an agent to
  rewrite the user's `screening_config.py`. Also closes
  [issue #1](https://github.com/mronkko/claude-academic-research/issues/1):
  `ANTHROPIC_BASE_URL` points the pipelines at any Anthropic-compatible
  endpoint (LM Studio, Open WebUI), making `ANTHROPIC_API_KEY` optional
  and suppressing the "unknown model prefix" warning that would otherwise
  fire on every call for a locally-served model name.

  **Open decision:** `FULLTEXT_CODING_MODEL` still defaults to
  `claude-sonnet-4-6` while the `sonnet` alias points at `claude-sonnet-5`.
  Moving the *default* changes cost and output for every existing project,
  so it was left for its own decision rather than riding along in a
  cleanup branch.

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

- **P11** — Alma/Primo library-resolver follow-ups (issue #6).
  The original minimal fix (flavor detection, `svc_dat=CTO`, and Alma
  `<context_service>`/`<resolution_url>` parsing in
  `fetchers/library_resolver.py`) shipped DOI-only. Two of its three
  deferred items have since shipped too:

  - **Done — ISSN+date+volume fallback.** `has_fulltext_access` /
    `sfx_lookup_dual` / `first_fulltext_target_preferred` /
    `_build_query_url` / `_query_target_urls` all gained optional
    `issn`/`pub_date`/`volume` kwargs (default `None`, fully backward
    compatible). `_query_target_urls` retries once via the new
    `_build_issn_query_url` (`rft.issn`+`rft.date`+`rft.volume`, no
    `rft_id`) when a DOI-keyed Alma query returns zero targets — the
    exact false negative the issue's reporting institution hits.
    `enrich_pdfs.py` extracts `ISSN`/`date`/`volume` from
    `zot_item["data"]` into `entry` at creation time (~L841), which
    already flows unchanged into both call sites (`sfx_lookup_dual`
    ~L959, `first_fulltext_target_preferred` ~L1056) — no extra
    plumbing needed once the fields were added there, since `entry`
    is the same dict object referenced through Pass 1 → Pass 2/3.
    Verified live against Aalto with a deliberately-engineered
    reproduction of the reporting institution's scenario (a DOI Alma
    will never link, paired with a real, licensed ISSN) in
    `tests/live/test_library_resolver_alma.py`'s
    `test_alma_uresolver_issn_fallback_*` tests — both pass. Note for
    whoever next touches this: live testing found `rft.date`/
    `rft.volume` don't act as filters at Aalto's Alma deployment at
    all (identical results across correct, wrong, and missing
    date/volume) — unlike SFX's `sfx.ignore_date_threshold` dual
    query, which relies on real date filtering. Don't assume Aalto's
    behavior generalizes to other Alma institutions.
  - **Done — wizard support + discovery docs.** `scripts/setup/
    wizard.py:KEYS` gained a `LIBRARY_OPENURL_BASE` → `[library]
    openurl_base` `KeySpec` (optional, not hidden — it's a URL, not a
    secret; `verify=_verify_none`, matching `WILEY_TDM_TOKEN`/
    `OPENALEX_API_KEY`'s pattern for fields with no cheap live check).
    Its `where=` field carries the SFX/Alma discovery recipes already
    written into `library_resolver.py`'s module docstring. Fixed a
    latent gap found while wiring this in: `load_from_config()` built
    the env-var-aware test fixtures expected `LIBRARY_OPENURL_BASE` to
    take precedence over `config.toml` (see
    `test_load_from_config_env_var_overrides_toml`) but never actually
    passed `env=` to `config_loader.get()` — silently dead code before
    this fix. `tests/live/test_auth_workflows.py` gained
    `test_auth_library_openurl_base` (a reachability probe, not an
    auth check — `openurl_base` has no credential to verify) to
    satisfy `test_live_coverage.py`'s per-`KeySpec` guard.
  - **Still open — platform-priority / `required_domains` matching
    for Alma targets.** `_effective_host()` / `_target_matches_domains()`
    / `_platform_rank()` operate on the target URL's hostname; Alma's
    `resolution_url` always hosts on the Alma redirector domain
    (e.g. `aalto.alma.exlibrisgroup.com`), never the real publisher,
    so `SFX_PLATFORM_PRIORITY` ranking and `required_domains`
    filtering silently degrade to "unranked"/"no match" for Alma
    targets instead of raising. Deliberately left deferred (confirmed
    with the user 2026-08-12) — doesn't regress the current caller
    (`enrich_pdfs.py`'s `first_fulltext_target_preferred` call site
    doesn't pass `required_domains`), and a real fix is a bigger,
    riskier change than the two items above: it needs
    `_fulltext_target_urls` to return structured `(url, platform_name)`
    data instead of a flat `list[str]`, which ripples into the on-disk
    cache format (currently `{"urls": [...]}`) and needs a migration
    path for existing cached entries, mirroring the precedent already
    set for legacy `{has_access, targets}` entries. Text-match target
    against Alma's `package_name`/`interface_name` keys (e.g.
    `"EBSCOhost"`) instead of a URL host — a different mechanism from
    SFX's.
  Files: [scripts/pipelines/fetchers/library_resolver.py](scripts/pipelines/fetchers/library_resolver.py),
  [scripts/pipelines/enrich_pdfs.py](scripts/pipelines/enrich_pdfs.py)
  (~L841, ~L959, ~L1056), `scripts/setup/wizard.py`,
  `tests/live/test_auth_workflows.py`.

### Skills

- **S8** — modular / stage-scoped invocation for `systematic-review`.
  Right now the skill's Bootstrap section (SKILL.md:35-92) is an
  all-or-nothing gate: using the skill for even one mechanical
  capability — e.g. just `enrich_pdfs.py`'s PDF-source cascade
  against an existing Zotero collection — first requires installing
  the full canonical scaffold (`search_config.py`,
  `screening_config.py`, 4 test templates, `manuscript.qmd`), and for
  the search stage, an explicit `.claude/systematic-review/scope.md`
  sign-off (SKILL.md:189-280).

  Hit directly in the AI-literature-review-study project: it already
  runs its own PRISMA-style pipeline (own Zotero collection, own tag
  taxonomy, own phase-based script layout) and wanted only
  `enrich_pdfs.py`'s Phase-1 PDF cascade for one batch of 244 items.
  Reusing the skill for that meant either (a) bootstrapping
  scaffold/config files the project will never populate or use, or
  (b) bypassing `Skill`/SKILL.md entirely and reverse-engineering
  `enrich_pdfs.py`'s actual requirements straight from source — which
  turned out to need none of the scaffold, just
  `ZOTERO_API_KEY`/`ZOTERO_GROUP`-style env vars via
  `core.config_loader`. Path (b) works but throws away SKILL.md's
  documentation/discoverability, and it only worked here because
  reading ~1300 lines of `enrich_pdfs.py` source was on the table;
  most users wouldn't find this out.

  **What it would take:**
  - Add a "standalone stage usage" note (Bootstrap section, or
    per-row in the "Common workflows" table around SKILL.md:659-680)
    stating which stages need zero scaffold beyond Zotero credentials
    (`enrich_pdfs.py`, `enrich_abstracts.py`, `audit_zotero_library.py`
    look like this already) vs. which genuinely need
    `search_config.py`/`screening_config.py`/`scope.md` (`search.py`,
    `abstract_screen.py`, `fulltext_code.py`).
  - Sharpen the `description:` trigger framing — the existing "Do NOT
    use for isolated Zotero enrichment without a screening pipeline —
    use `zotero-operations`" clause doesn't cover a project running
    its *own* partial pipeline that still wants one specific stage
    (PDF enrichment, or full-text coding) without the rest.
  - Maybe a `--minimal`/stage-scoped path in `check_configured.py` /
    `check_project_scaffold.py` that checks only the credentials a
    named stage needs, instead of the full four-test-file +
    config-template list.

  Files: [skills/systematic-review/SKILL.md](skills/systematic-review/SKILL.md)
  (Bootstrap ~L35-92, scope gate ~L189-280, workflows table
  ~L659-680), `scripts/setup/check_configured.py`,
  `scripts/setup/check_project_scaffold.py`.

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
