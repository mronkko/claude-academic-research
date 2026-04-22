# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] — unreleased

### Two-pass PDF retrieval: API cascade first, browser + Connector on residuals

The browser-mode pipeline is now explicitly a second pass. Pass 1
(the API cascade — Elsevier / Springer / Wiley TDM / Crossref TDM /
PMC / OpenAlex / Unpaywall) stays unchanged: fast, non-interactive,
no per-item DOI resolution. Pass 2 (new in v0.4.0) only processes
items Pass 1 couldn't attach, so DOI resolution costs scale with the
*residual* count rather than total library size.

Pass 2 routing:

1. Resolve the DOI via Crossref (habanero) — cached on disk in
   `<cache-dir>/doi_resolver_cache.json`. Catches prefix-drift
   cases like ETAP's `10.1111/etap.*` DOIs that now live on
   `journals.sagepub.com`.
2. If the resolved host matches an API source Pass 1 would have
   skipped by DOI prefix (Wiley TDM / Elsevier / Springer), retry
   that source once with `bypass_prefix_filter=True`. Catches
   journals that migrated onto one of the big TDM-capable
   publishers without changing their DOI prefix.
3. Otherwise pick the correct browser handler by matching the
   resolved URL host against each handler's `direct_access_domains`.
4. Fall through to the Zotero Connector (via SFX target) when no
   browser handler matches or the matched handler hits Case 2 /
   fails / is in `[library] no_access`.

New `--all` flag on `enrich_pdfs.py` runs both passes in one
invocation: Pass 1 → re-query `zot.pdf_map()` → Pass 2 on what's
left. Equivalent to running the two commands back-to-back, but in
one process. Mutually exclusive with `--sources`.

### Zotero Connector fallback for library-routed PDFs

When the library's SFX resolver offers a full-text route via a
third-party platform we don't have a bespoke handler for (EBSCOhost,
JSTOR, ProQuest, Project MUSE, …), the browser pipeline now delegates
to the Zotero Connector Chrome extension. One generic handler
(`scripts/pipelines/fetchers/browser/connector.py`) covers whatever
Zotero's community-maintained translators cover — no more
one-handler-per-platform maintenance tax.

**Three-pass routing model.**

For every DOI the browser pipeline now runs two SFX queries (default
date-filtered + `sfx.ignore_date_threshold=1`) and classifies each
item into one of three cases:

- **Case 3** — library covers this DOI on the direct publisher's
  domain: run the direct handler (Wiley, AoM, Sage, …).
- **Case 2** — library has the publisher but this DOI's year is out
  of coverage: skip the direct handler (it would paywall); route to
  the Connector via a Query-B target if one exists.
- **Case 1** — library has no relationship with this publisher at
  all: try the direct handler anyway (user might be an individual
  member, e.g. AoM); on failure, fall through to the Connector.

Items with no direct handler (MIS Quarterly, INFORMS without AoM-like
user subscriptions, …) go straight to the Connector upfront bucket.

**Learn-from-runtime failure prompt** replaces wizard enumeration of
publisher access. On the first per-item failure in a run the user
sees a three-way choice:

- `k` — keep trying the direct handler (failures still retry via
  the Connector at end of run);
- `s` — skip remaining direct attempts this run (default on Enter);
- `A` — always skip: appends the publisher to `[library] no_access`
  in `~/.config/academic-research/config.toml` so future runs jump
  straight to the Connector.

Non-TTY runs (CI / piped stdin) take `skip` automatically or obey
the new `--on-first-failure` flag.

**Dedup via vendored zotero-mcp algorithm.** The Connector saves as
a new Zotero item (it doesn't know about the existing DOI item).
`ZoteroClient.merge_duplicate_item` — ported ~60 LOC from
`zotero-mcp` (MIT-licensed, attribution in module docstring) — moves
children into the keeper, unions tags and collections, skips
duplicate attachments by `(contentType, filename, md5, url)`, and
trashes the duplicate parent via `PATCH {"deleted": 1}` (recoverable
from Zotero's Trash, not a permanent delete).

**Setup wizard additions.**

- Detects the Zotero Connector extension at the macOS / Linux /
  Windows Chrome default-profile paths and offers to use it. Install
  hint printed when the extension is absent.
- Shows the current `[library] no_access` list and lets the user
  remove entries (the "undo" path for the runtime "Always skip"
  answer).

### New

- `scripts/pipelines/fetchers/doi_resolver.py` — `resolve_doi(doi, *, crossref, cache)` via habanero.Crossref plus an on-disk `DoiResolverCache`. Called only in Pass 2; never in the API cascade.
- `scripts/pipelines/fetchers/browser/connector.py` — `ZoteroConnectorHandler`
  (Playwright + extension service-worker-eval path, per the POC in
  `temp/open_zotero_browser.py`).
- `scripts/core/config_writer.py` — safe `append_to_list` /
  `remove_from_list` helpers. Used by the failure-prompt "Always
  skip" path and by the wizard's `no_access` editor; preserves mode
  `0600` and the wizard's manual TOML format.
- `ZoteroClient.merge_duplicate_item(target_key, duplicate_key)`.
- `fetchers.library_resolver.sfx_lookup_dual()` and
  `first_fulltext_target_preferred()` with a `SFX_PLATFORM_PRIORITY`
  ranking (EBSCOhost > publisher-direct > JSTOR > ProQuest).
- `PublisherHandler.attaches_directly: bool` — when True, the
  driver calls `download_and_attach(page, ctx, service_worker,
  item, zot, …)` instead of the standard `download()` +
  `zot.attach_pdf()` pipeline.
- `enrich_pdfs.py --sources connector` — Connector-only mode for
  targeted validation runs.
- `enrich_pdfs.py --all` — runs Pass 1 (API cascade) then Pass 2
  (browser + Connector) on residuals in one invocation.
- `enrich_pdfs.py --on-first-failure=keep|skip|always_skip` —
  non-interactive answer for the failure prompt.
- `PdfFetcher.direct_access_domains` class attribute + `bypass_prefix_filter`
  kwarg on `fetch_pdf`. Wiley TDM / Elsevier / Springer declare their
  hosts; Pass 2 uses the flag to invoke them on DOIs whose prefix
  Pass 1 skipped.
- `resolve_by_host(host, handlers)` helper in `fetchers.browser`
  mirroring `resolve_by_doi` but matching on `direct_access_domains`.
- `[zotero_connector] extension_dir` config key; override via
  `ZOTERO_CONNECTOR_DIR` env var.
- `[library] no_access` TOML list config key; wizard-editable at
  setup time, runtime-appendable via the failure prompt.

### Changed

- `fetchers.library_resolver.SfxCache` value shape is now
  `{"urls": [target URLs]}` — raw target list is stored once per
  `(doi, ignore_date_threshold)` and filtered per-caller. Legacy
  `{has_access, targets}` entries from v0.3.x are treated as a
  cache miss and re-queried on the next run (one-time cost).
- `setup/wizard.py:_write_config` now emits TOML list values
  (`no_access = ["aom", "apa"]`) in addition to quoted strings.
  Existing runs are unaffected — the format for scalar keys is
  unchanged.
- `launch_context()` gains an `extensions=[...]` keyword argument
  that maps to Chromium's `--load-extension` + isolates the
  Connector profile from the direct-handler profile.

## [0.3.1] — 2026-04-22

### Move legacy orchestrators under `legacy/` subdirectory

The four pre-v0.3.0 orchestrators that v0.3.0 deliberately retained
as a rollback path are now under `scripts/pipelines/legacy/`:

- `legacy/attach_pdfs.py`
- `legacy/fetch_abstracts.py`
- `legacy/fetch_pdfs_browser.py`
- `legacy/fetch_pdfs_wiley_tdm.py`

Plus `legacy/README.md` documenting the deletion checklist for the
next release.

### Fixed

- The moved scripts add `scripts/pipelines/` to `sys.path` at module
  load so `import zotero_io` still resolves (zotero_io lives one
  level up now). `fetch_pdfs_browser.py` also walks two levels up
  for `SCRIPTS_ROOT` (for the `publishers.registry` import).
- `enrich_pdfs.py --legacy-browser` subprocess path updated to
  `legacy/fetch_pdfs_browser.py`.
- `tests/unit/test_live_coverage.py` reads the source files from
  their new `legacy/` path; the guard still enforces live-test
  coverage for every `fetch_from_*` / `fetch_*_pdf` function in the
  legacy cascade.
- `tests/live/test_browser_publishers.py` loads the legacy fetcher
  from its new path and adds the legacy dir to `sys.path` before
  importing (so the sibling `import attach_pdfs` resolves).
- `audit_zotero_library.py` "next steps" output now suggests the
  refactored `enrich_abstracts.py` / `enrich_pdfs.py` rather than
  the legacy scripts.

## [0.3.0] — 2026-04-22

### Pipeline refactor: pyzotero-backed Zotero I/O, per-provider fetcher classes, library-aware browser flow

Multi-week refactor of the `scripts/pipelines/` tree. The
pre-refactor structure mixed Zotero upload logic, custom HTTP
retry, and per-publisher download flows inside four monolithic
scripts (`attach_pdfs.py`, `fetch_abstracts.py`, `fetch_pdfs_wiley_tdm.py`,
`fetch_pdfs_browser.py`). This release replaces that with:

**New modules.**

- `scripts/pipelines/zotero_io.py` — `ZoteroClient` wrapping
  `pyzotero`. Every script that touches Zotero now routes through it.
  Deletes ~110 lines of custom 3-step S3 upload + manual
  `If-Unmodified-Since-Version` PATCH code; `pyzotero.attachment_simple()`
  and `pyzotero.update_item()` already did this. `@retry` on
  `update_abstract()` re-fetches the item's latest version on HTTP 412
  and re-applies — covers the previously-unhandled version-conflict case.
- `scripts/pipelines/http_client.py` — shared `requests.Session` with
  `urllib3.Retry` (429 / 5xx, exponential backoff) and `tenacity`
  wrappers on `get_json` / `get_bytes`. Replaces hand-rolled `urllib`
  wrappers and ad-hoc `time.sleep(30) + recursion` retries.
- `scripts/pipelines/fetchers/` — one class per provider implementing
  the `AbstractFetcher` / `PdfFetcher` ABC pair. A provider that
  serves both capabilities (Crossref, OpenAlex, ScienceDirect)
  inherits both. Nine abstract-fetchers and eleven PDF-fetchers total,
  each in its own file with live tests.
- `scripts/pipelines/fetchers/wos.py` — new abstract fetcher using the
  WoS Expanded API with a title-search fallback for DOI aliases
  (e.g. AoM Annals `10.5465/…` that WoS indexes under its original
  Routledge/T&F `10.1080/…` prefix). Recovers 2 of 7 abstracts that the
  prior cascade couldn't find on a test library.

**New `fetchers/browser/` sub-package.**

- Nine `PublisherHandler` subclasses (aaa, aom, apa, emerald, informs,
  oup, sage, tandf, wiley) with two intermediate bases
  (`RequestHandler` for sessions that `ctx.request.get()` can use;
  `PageNavigationHandler` for publishers whose Cloudflare rejects
  non-browser requests). Three custom flows ported from the
  SLR-motivation project's working code: INFORMS's epdf→pdfdirect
  rewrite, OUP's JS-extracted PDF href, APA PsycNET's multi-step
  click-through.
- `setup_url_template` per handler — landing-page URL opened during
  the browser-setup phase, distinct from the download URL. Fixes an
  observed bug where opening a `?download=true` PDF URL triggered
  Chromium to auto-download to the profile's download dir and stranded
  the user at `about:blank` before they could solve Cloudflare.
- SFX / OpenURL pre-flight (`library_resolver.py`). When
  `[library] openurl_base` is set in `config.toml`, each DOI is
  checked against the library's link resolver before the browser
  handler runs. Targets are filtered by the handler's
  `direct_access_domains` — a JSTOR / EBSCOhost / ProQuest route
  reported by SFX doesn't count as accessible if our handler only
  knows the direct-publisher URL. Eliminates ~30s-per-item timeouts
  on inaccessible DOIs and surfaces the skip in the CSV log.
- On-disk SFX cache keyed by `(doi, handler-domain-set)` so
  re-running is instant and two handlers querying the same DOI with
  different domain filters don't collide.

**New orchestrators.**

- `scripts/pipelines/enrich_abstracts.py` — replaces
  `fetch_abstracts.py`. Drives the abstract-fetcher cascade
  (Crossref → Semantic Scholar → Scopus → WoS → ScienceDirect →
  OpenAlex GROBID) through a `ThreadPoolExecutor`.
- `scripts/pipelines/enrich_pdfs.py` — replaces `attach_pdfs.py`
  plus the two `fetch_pdfs_*.py` fallbacks. Automated cascade by
  default; `--sources wiley` routes to the Wiley TDM handler;
  `--sources browser` drives the per-publisher browser handlers
  in-process (no more subprocess shell-out). `--legacy-browser`
  keeps the old subprocess path available for rollback.

**Setup-wizard improvements.**

- Per-tier MCP-server check with install / homepage hints for each
  expected server (zotero, openalex, semantic-scholar, scopus,
  paper-search). Wizard offers to run `claude mcp add` after
  confirming the binary's available on PATH.
- Local-Zotero probe against `http://localhost:23119/api/` — prints
  actionable instructions if Zotero desktop isn't running or the
  Better BibTeX local HTTP server isn't enabled.

**UX in browser flow.**

- Setup banner now says "Google Chrome for Testing" (the actual window
  title Playwright produces on macOS); removed the undefined
  "Playwright" jargon in favour of "a separate automated browser used
  only by this script".
- Per-publisher `setup_hint` explaining what institutional access /
  sign-in is needed (AoM's two-gate login, Wiley's Shibboleth flow,
  etc.).
- Yes/no prompt at the end of the setup banner: `y` to proceed, `n`
  to skip the publisher entirely (all items logged as
  `skipped_no_access`, no 30s download timeouts).
- pyzotero's `WheneverDeprecationWarning` silenced at the `zotero_io`
  import level — library-internal, benign, was burying real output.

**Migrated scripts.** Every Zotero-touching script (`abstract_screen`,
`audit_zotero_library`, `fulltext_code`, `import_to_zotero`, plus
the legacy `attach_pdfs`/`fetch_abstracts`/`fetch_pdfs_*`) now uses
`ZoteroClient`. The legacy top-level scripts remain on disk during
this release cycle as a rollback path; next release deletes them
once the new orchestrators have proven themselves on production
libraries.

**Fixed.**

- `ZoteroClient.attach_pdf`: pyzotero's `attachment_simple()` returns
  `{"success": [...], "failure": [...], "unchanged": [...]}` where all
  three are lists of item dicts, not dicts keyed by integer index.
  The first version of the wrapper matched the wrong shape (tests
  mocked the wrong shape too), and crashed on real uploads with
  `'list' object has no attribute 'values'`. Fixed both the wrapper
  and the tests.
- DOI-alias handling in WoS title fallback: a 100-char-truncated
  quoted WoS query silently returned 0 hits (quoted phrase searches
  require exact match). Switched to unquoted `TI=(…)` keyword-AND
  search with a Python-side title normaliser for precision.
- `enrich_pdfs.py --sources browser` opened the PDF URL directly,
  triggering Chromium to auto-download and strand the user at
  about:blank. Every handler with `?download=true` in its URL
  template now has a distinct `setup_url_template` pointing at the
  article landing page.

**New tests.** 219 unit tests pass (from 72 pre-refactor). Coverage
added for the Zotero wrapper, HTTP client, fetcher ABCs, each
provider fetcher, the browser handlers (registry + URL
regressions), and the SFX resolver (parser, domain filter, cache).

## [0.2.4] — 2026-04-19

### test_suite.py template: realign with refactored screening pipeline

The screening scripts no longer define `MODEL` / `PROMPT_VERSION` as
module-level constants — they read the values from the project's
`screening_config.py` via `getattr(mod, "ABSTRACT_SCREENING_MODEL", …)`.
The template's old `test_screening_script_constants_in_log` grepped the
scripts for `^MODEL = "..."`, found nothing, and silently no-op'd.
Drift was invisible.

Fixed in `templates/test_suite.py`:

- New `SCREENING_CONFIG` path constant pointing at the project's
  `screening_config.py` (the canonical source of model + prompt-version
  declarations).
- Renamed `test_screening_script_constants_in_log` →
  `test_screening_config_constants_in_log`. Now greps
  `screening_config.py` for the four `FULLTEXT_CODING_*` /
  `ABSTRACT_SCREENING_*` constants and verifies each log's model /
  prompt_version set is a subset (subset, not equality — so an
  in-progress re-run mid-transition isn't a false alarm).
- `test_temperature_zero_pinned` kept as-is but reworded: silently
  passes when neither script is locally copied, since the plugin's own
  test suite enforces the invariant for plugin-invoked scripts.

Did not add a `VersionConflictError` leakage test — the error is
tenacity-retried internally and does not surface in screening CSV rows
in practice, so it would be speculative.

## [0.2.3] — 2026-04-19

### Manuscript scaffold maturity + `_tables.py` → `tables.py` rename

The manuscript templates are the public API a user's Quarto
manuscript imports from. Leading the file with an underscore
implied "private / implementation detail" — the opposite of intent.
Dropped the underscore and grew the scaffold to cover two common
gaps: PRISMA flow and construct-family grouping.

Templates:

- **`templates/_tables.py` → `templates/tables.py`** (git rename,
  history preserved). The Quarto manuscript imports `from tables
  import …`, so the file name should match the import. All cross-refs
  updated (skill + manuscript).
- **`templates/tables.py`** — added `tbl_construct_families(stats)`
  helper that reads `coding.family.<slug>` keys from `build_stats()`
  output and returns a sorted DataFrame. Empty DataFrame when no
  families configured, so the manuscript can fall back to a
  placeholder comment cleanly.
- **`templates/manuscript.qmd`** — new PRISMA Mermaid code chunk in
  the Methods section, driven entirely by inline `s[...]` lookups
  (no hand-typed counts). New `tbl-families` chunk in Findings that
  renders `tbl_construct_families(s)` when configured, otherwise
  emits a placeholder HTML comment. Updated all imports to `from
  tables import …`.
- **`templates/stats.py`** — expanded the `CONSTRUCT_FAMILIES` comment
  block into a proper worked example explaining how the field name,
  rule tuples, and downstream `tbl_construct_families()` fit together.
  The list still ships empty (feature is opt-in).

Skill update: `systematic-review/SKILL.md` points at `tables.py`
instead of `_tables.py`, and notes the copy-into-project step so
the `.qmd`'s `from tables import …` resolves.

## [0.2.2] — 2026-04-21

### `searchers/` package — one ABC, four implementations

Extracted the per-database search logic from `search.py` /
`search_openalex.py` into a clean abstract-base-class package so
that (a) adding a new database is writing one small file, and (b)
the orchestrator's database loop becomes data-driven.

Mirrors the `fetchers/` package pattern (fetchers = retrieve content
for a known DOI; searchers = discover DOIs matching a query) without
overlapping it.

New package `scripts/pipelines/searchers/`:

- **`base.py`** — `SearchSource` ABC with `name`, `supports_journal_scope`,
  `supports_block_queries` attributes; `run(config, ctx)` method;
  `credentials_error(ctx)` hook. `SearchContext` dataclass carries
  year window, ISSN list, mailto. Common `SEARCH_ROW_FIELDS` schema
  that harmonises Scopus / WoS / OpenAlex / Semantic Scholar outputs
  (union of per-source identifiers + OA metadata where available).
- **`scopus.py`** — `ScopusSearch` using `pybliometrics`.
  `supports_journal_scope=True`, not block queries. Credentials:
  either `~/.config/pybliometrics.cfg` or `SCOPUS_API_KEY`.
- **`wos.py`** — `WosSearch` against the Expanded API with 100-row
  paging. Requires `WOS_API_KEY_EXTENDED` (Starter tier does not
  support `IS=` so is not a substitute). `supports_journal_scope=True`.
- **`openalex.py`** — `OpenAlexSearch` with the block-query pattern
  (run Block A and Block B separately, merge) that preserves recall
  against OpenAlex's relevance-ranked `search=`. Free tier; no key.
  `supports_block_queries=True`.
- **`semantic_scholar.py`** — `SemanticScholarSearch` via the
  graph-API bulk endpoint. New database in the plugin. Same block
  pattern as OpenAlex. `supports_journal_scope=False` — S2 doesn't
  reliably filter server-side, so the source post-filters
  client-side against `ctx.issns`. API key optional but strongly
  recommended (unauthenticated tier is 1 rps shared globally).

Refactored:

- **`search.py`** now reads the registry, picks sources that pass
  `credentials_error()` by default (or respects `--databases scopus,wos`),
  and dispatches each source's `run()`. Existing dedup + metadata +
  integrity-gatekeeper logic unchanged. ~100 lines shorter net.
- **`search_openalex.py`** reduces to a thin shim that re-dispatches
  to `search.py --databases openalex`.

New thin single-DB wrappers for piloting:
`search_scopus.py`, `search_wos.py`, `search_semantic_scholar.py`
(each ≈30 lines, each delegates to `search.py --databases <name>`).

Tests: `tests/unit/test_searchers_base.py` (17 tests) — ABC cannot
be instantiated; every registered source declares `name`,
`supports_*` flags; registry returns fresh instances; empty-row
schema invariants; per-source credential checks.

Skill update: `systematic-review` stage table lists every search
entry point.

165 → 182 default tests. Ruff clean on my additions.

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
