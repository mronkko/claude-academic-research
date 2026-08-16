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

## Done — PDF retrieval reliability + reporting (branch `fix/pdf-retrieval-reporting`, 2026-08-13)

Driven by a real downstream session transcript in which a 244-item
review needed four rounds of user pushback ("Why do we have so many
fulltexts not available?" → "79 is too many. What is missing." → "No,
try harder. Report what is missing.") to get from 125 to 223 usable
full texts. Almost everything recovered had been retrievable all along;
the pipeline simply never said so. Landed together because they are one
failure mode — silent loss with no end-of-run account:

- **Upload failures were terminal and undiagnosable.** `attach_pdf` had
  no retry, and the exception text was printed and dropped. 48 Sage
  PDFs, fully downloaded behind a solved Cloudflare challenge, were
  lost this way. Now: tenacity retry on 429/5xx/transport (never on a
  reported `failure` payload), a `detail` column on every non-success
  row, and rows in `pdf_fetch_log.csv` under a new `UPLOAD_FAILED`
  cause whose FE suggestion is explicitly "not an exclusion".
- **Nothing recovered cached-but-unattached PDFs.** Recovery took a
  hand-written one-off script. Now a cache-recovery pass runs before
  any fetching, and the DOI case-skew that could hide a cache hit
  between the API and browser paths is fixed.
- **`--sources browser` printed no summary at all**, and only 3 of 14
  statuses were ever counted. New `pdf_run_report.py` reports every
  status with per-item citations and a concrete next lever, from all
  four exits, plus `--report` to re-read an existing log.
- **The SFX pre-flight failed closed**, so a transport blip was
  indistinguishable from a real entitlement gap — 16 items skipped
  against journals the user demonstrably had access to. Now fails open
  on unset/unreachable/unparseable via `lookup_fulltext_target`, stops
  persisting negative verdicts to `sfx_cache.json`, and falls back to
  the DOI resolver URL (the Connector skips outright on a null target,
  so failing open without a URL would only relabel the skip).
  `--ignore-library-coverage` overrides it entirely.
- **Truncated downloads passed validation.** Every HTTP fetcher checked
  only `status_code == 200` and `content[:4] == b"%PDF"`, which half a
  PDF satisfies. OpenAlex served five permanently-truncated files
  (byte-identical across retries; one declared its xref at offset
  1,744,085 in a 1,608,714-byte file) that were attached as clean
  successes and initially misdiagnosed as scans needing OCR. New
  `fetchers/_pdf_validate.py` checks Content-Length, the `%%EOF`
  trailer and the xref offset; all seven fetchers use it on both
  responses and cache reads, and a corrupt file is never attached —
  attaching it would make the item look permanently done.
- **Publishers needing an interactive solve were never enumerated**, so
  the user solved Sage and AoM and was never told APA was queued (10
  items, zero attempts). Handlers now declare
  `needs_interactive_solve`, the queue names them up front, and
  `--plan` prints the whole plan without opening a browser.
- Also: `--no-prompt` now actually implies `--on-first-failure=skip`;
  the fail-fast message no longer suggests `--browser`, a flag that
  never existed; `--filter-keys-file` warns about keys that matched
  nothing; and `open_log` migrates headerless / short-header logs
  rather than silently misaligning them (a real user log was 1813 rows
  with no header).

**On OCR — a diagnosis to stay away from.** "Scanned PDF needing OCR"
was proposed twice during the incident and was wrong both times. Of the
5 textless files, **0** were scans: 3 came back intact via Wiley TDM
(19/22/24 pages) and 2 via the Sage browser handler (34/44 pages), all
with real extractable text. The lesson is that zero extractable text is
a *symptom*, and on the only evidence we have its usual cause is a bad
copy from a bad source. `attached_no_text` therefore points at
re-fetching from a different source, and names OCR only as what to
consider after a second source returns the same file. No OCR work is
planned, and "it's a scan" should not be anyone's first hypothesis.

**Still open, deliberately:** Alma structured-target ranking (P11's
"still open" half — unchanged).

---

## In progress — live end-to-end SLR test (branch `feat/live-mini-slr`)

**L1** — a live, opt-in mini systematic review that runs the whole
pipeline against real APIs and a real Zotero library, then asserts the
pipeline's own invariants. Designed 2026-08-12; implemented 2026-08-12;
**first live run 2026-08-13 (run-id `20260813T082726Z`): 12/14 verify
checks passed, 2 failed — root cause understood, fix not yet applied.**
See "L1 follow-up" immediately below for exactly what to do next; this
paragraph is the historical design record.

**L1 follow-up — trim-stage journal diversity. DONE (2026-08-13, branch
`fix/pdf-retrieval-reporting`).** `stage_trim` no longer shells out to
`filter_search_results.py --top-n`; it round-robins across the
configured `JOURNALS` (ISSN match, falling back to journal name),
deterministically ordered by year desc then DOI. Covered by
`tests/unit/test_mini_slr_trim.py` (13 tests), including the exact live
failure shape (40 SBE rows ahead of 5 JBV + 5 SEJ). Unmatched rows only
top up a short sample, so a stray row can never displace the coverage
the sampling exists to guarantee. A journal that returns nothing prints
a WARN naming the publisher route that consequently went untested. The
original diagnosis is kept below for the audit trail.

**Original diagnosis (2026-08-13).** The first live run's `verify` stage failed
(`test_fulltext_log_decision_states_final`,
`test_no_remaining_errors_in_fulltext_log`) because all 3 items that
passed abstract screening ended up `error`/`no_pdf` in
`fulltext_code.py` — no PDF was ever attached. Root cause, fully
diagnosed via live reproduction (not a guess):

1. `stage_trim` in `scripts/dev/mini_slr.py` calls
   `filter_search_results.py --top-n 8`, which sorts by year descending
   and keeps the first 8 rows. Every row in this corpus is year 2019
   (a tie), so the sort is stable and just keeps whatever 8 rows came
   first in `search.py`'s deduped output — no guarantee of covering all
   three configured journals. In the live run, **all 8 trimmed items
   were Small Business Economics (Springer, `0921-898X`) — zero JBV
   (Elsevier), zero SEJ (Wiley)**, even though `ELSEVIER_API_KEY` and
   `WILEY_TDM_TOKEN` are both configured and never got exercised.
2. Springer's PDF fetch
   ([fetchers/springer.py](scripts/pipelines/fetchers/springer.py))
   is a bare unauthenticated GET against the public
   `link.springer.com/content/pdf/<doi>.pdf` URL — no API token, no
   proxy routing. Reproduced directly: returns HTTP 200 with
   `Content-Type: text/html` (a landing page), not a PDF. The user
   confirmed via their library's SFX listing that Small Business
   Economics access at their institution genuinely exists (FinELib
   SpringerLink from 1997) — so this isn't a missing-entitlement dead
   end, it's that the *automated* path doesn't use the mechanism that
   actually grants access. `library_resolver.py`'s SFX/OpenURL
   resolution (`sfx_lookup_dual`, `first_fulltext_target_preferred`) is
   wired into `enrich_pdfs.py` **only inside `_run_browser_in_process`
   (Pass 2 / `--sources browser`)**, never into `_run_api_cascade`
   (Pass 1, the automated path `mini_slr.py`'s `stage_enrich` actually
   runs). Confirmed by grep: no `library_resolver` import anywhere in
   `_run_api_cascade`'s call path. `LIBRARY_OPENURL_BASE` also isn't
   configured on the dev machine, which would matter for Pass 2 but not
   Pass 1 either way.
3. By contrast, Elsevier
   ([fetchers/sciencedirect.py](scripts/pipelines/fetchers/sciencedirect.py))
   and Wiley
   ([fetchers/wiley.py](scripts/pipelines/fetchers/wiley.py)) both
   authenticate via a proper TDM API token
   (`X-ELS-APIKey` against `api.elsevier.com`; the `wiley-tdm` library)
   checked server-side against the token — not IP/proxy-dependent. If
   the corpus had included JBV/SEJ items, Pass 1 would have had a
   genuine, network-independent shot at them.

**Decision (settled with the user, 2026-08-13): fix trim-stage journal
diversity.** `stage_trim` should sample across all three configured
journals instead of blind top-N-by-year, so a run actually exercises
Elsevier/Wiley/Springer as the corpus was designed to. This does not
guarantee zero `no_pdf` outcomes (real entitlement gaps can hit any
publisher) — Springer via Pass 1 specifically may keep failing at this
institution regardless of diversity, since the automated path
structurally can't reach the SFX-mediated access route (that's Pass
2/browser territory, which `live_slr` deliberately never runs
unattended, same as the existing `live_browser` doctrine). The user
explicitly confirmed `verify` should keep hard-failing on unresolved
`error`/`no_pdf` items — that behavior is correct and should NOT be
weakened; a `no_pdf` item is a real, surfaced pipeline outcome, not
noise to suppress.

**What it would take:** change `stage_trim` (or add a step before it)
to group the pre-trim deduped CSV by journal/ISSN and take a balanced
sample (e.g. up to ~3 per journal, capped at ~8 total) rather than
`filter_search_results.py --top-n 8` verbatim. Consider whether this
still shells out to `filter_search_results.py` (per-journal, then
merge) or needs new logic directly in `mini_slr.py` — the shipped
script has no per-group/stratified mode today.

**Live Zotero state still open as of 2026-08-13:** run `20260813T082726Z`
was NOT torn down (verify failure aborts before the `teardown` stage
runs, by design — so a failed run's Zotero state stays inspectable).
Group `academic-research-e2e` (id `6637302`) currently holds collection
`e2e-20260813T082726Z` (key `GAA3VANG`) with 8 items (keys recorded in
`output/e2e/20260813T082726Z/.mini_slr_state.json`'s
`created_item_keys`). Tear down with `uv run scripts/dev/mini_slr.py
--stage teardown --run-id 20260813T082726Z` once done inspecting, or
just let the next full `--stage all` run reuse a fresh run-id and
leave this one for manual comparison.

**Also still open:** most of L1's own files are uncommitted (only the
Semantic Scholar key-fallback fix — see below — landed as a commit so
far). `git status` on this branch shows `scripts/dev/` (mini_slr.py),
`scripts/pipelines/zotero_io.py`, `scripts/pipelines/import_to_zotero.py`,
`templates/test_systematic_review.py`, `tests/live/README.md`,
`tests/live/e2e/`, `tests/live/test_mini_slr.py`, and this file itself
all modified/untracked. Commit once the trim-diversity fix lands and a
live run passes clean, or sooner if preferred.

**Unrelated fix already landed (commit `95f2e24`, 2026-08-13):** the
first live run also hit a dead `SEMANTIC_SCHOLAR_API_KEY` (403
Forbidden on every S2 Graph API endpoint while anonymous calls to the
same endpoints succeeded — confirmed a revoked/invalid key, not a
header or scope bug). Both call sites
([searchers/semantic_scholar.py](scripts/pipelines/searchers/semantic_scholar.py),
[fetchers/semantic_scholar.py](scripts/pipelines/fetchers/semantic_scholar.py))
now warn once and fall back to unauthenticated on a 403-with-key
instead of crashing the whole search. Covered by
`tests/unit/test_semantic_scholar_key_fallback.py` (6 tests). This is
unrelated to the trim-diversity issue and needs no further action.

**Setup precondition: DONE (verified 2026-08-12).** The group
`academic-research-e2e` exists (id `6637302`), is the unique exact
name match among the user's groups, is synced to the local Zotero
client (`localhost:23119` returns HTTP 200 for it), and *was* empty at
that time. **No longer empty as of 2026-08-13** — see "Live Zotero
state still open" above; it currently holds one leftover run's
collection pending teardown or reuse. The id is recorded here for
information only — the harness resolves it at run time from the
hardcoded name and must never read it from config.

**Why:** every existing test either mocks the network or probes a
single endpoint. Nothing exercises search → import → enrich → screen →
code → export as one run, so whole-pipeline defects survive CI. Three
were found while merely *designing and implementing* this (D1-D3 below).

**Worktree layout (already created):**

- `feat/live-mini-slr` — this work. Primary checkout.
- `chore/zotero-mcp-overlap` — parallel dead-code / zotero-mcp-overlap
  pass in a sibling worktree at `../claude-academic-research-cleanup`.
- Both fork from `1f8c67f` (tip of `feat/zotero-mcp-resync`).
- `scripts/pipelines/zotero_io.py` is the one contended file — see the
  coordination note under "Enabling changes" below.

**Scope decisions (settled with the user — do not relitigate):**

- **Target library: a Zotero *group* the user creates by hand**, found
  by a **hardcoded name** (`academic-research-e2e`). No env var, no
  config section, no `--group` flag — credentials and user id come
  from the live installation via `core.config_loader`. The Zotero Web
  API cannot create groups (items / collections / saved searches only)
  and the local API at `:23119` is read-only, so manual creation is
  unavoidable; a missing group produces a clean pytest skip with
  instructions.
- **Not My Library.** Empirically disqualified: the personal library
  already holds 2019 Small Business Economics papers carrying live
  `SLR-search` / `growth-aspirations` tags from real reviews.
  `import_to_zotero.py`'s dedup scans the whole library
  (`_fetch_existing_items` → `zot.journal_articles()`), so those items
  would be patched, added to the test collection, then tagged
  `abstract:*` / `fulltext:*` and given `SLR Coding` notes —
  corrupting the record the pipeline treats as ground truth.
- **Corpus:** 3 journals × a *closed* year window (`FROM_YEAR =
  TO_YEAR = 2019`, so the corpus is near-frozen and runs stay
  comparable), trimmed to ~8 items. JBV `0883-9026` (Elsevier/
  ScienceDirect TDM), SEJ `1932-4391` (Wiley TDM, second pass via
  `--sources wiley`), Small Business Economics `0921-898X` (Springer).
  PMC / Unpaywall / OpenAlex fire as cascade fallbacks. All four
  search databases run, because cross-database dedup and the
  `search_run.json` DOI hash are only exercised by a multi-DB run.
- **Teardown deletes only keys recorded from `_create_new_items`'
  success map** — never by tag match, which would also target items
  the harness merely patched. `--keep` skips teardown.

**Deliverables:**

1. `scripts/dev/mini_slr.py` — resumable stage driver (`--stage
   search|trim|collection|import|sync|enrich|audit|screen|code|export|verify|teardown|all`),
   artefacts under `output/e2e/<run-id>/` (already gitignored).
2. `tests/live/e2e/{search_config,screening_config}.py` — mini fixtures.
3. `tests/live/test_mini_slr.py` — new `live_slr` marker; drives the
   driver and asserts invariants.
4. `live_slr` marker registered in `pyproject.toml`; `tests/live/
   README.md` gains a runbook + the one-time group-creation step.

**Load-bearing findings (expensive to rediscover):**

- **PDFs can bypass the Zotero sync wait.** `enrich_pdfs.py` caches to
  `output/pdf_cache/<doi with / and : → _>.pdf`, and
  `fulltext_code.py`'s `_find_pdf_path` step 4 looks for exactly
  `<pdf_dir>/<doi.replace("/","_")>.pdf`. So `fulltext_code.py
  --pdf-dir <run>/output/pdf_cache` reads what enrich just downloaded,
  with no wait for attachment bytes to sync down from S3.
- **Reads are local-only with no cloud fallback.** Every pipeline
  script except `audit_zotero_library.py` constructs `ZoteroClient`
  with `prefer_local=True`, and `_read_client()` has no fallback. So
  Zotero desktop must be running and synced, and two sync waits sit in
  the run: after `import_to_zotero.py` (cloud write → local read) and
  after `abstract_screen.py` (cloud tag write → local tag read).
- **Three stages are library-wide, not collection-scoped.**
  `enrich_abstracts.py` / `enrich_pdfs.py` / `enrich_dois.py` take only
  `--filter-keys-file` (the harness generates it at import time);
  `audit_zotero_library.py` has *no* input filter at all — despite
  `systematic-review/SKILL.md` claiming "`--filter-keys-file` for
  enrichment / audit / export scripts". It only *writes* keys files.
- **Model IDs in `templates/screening_config.py` are current** —
  `claude-haiku-4-5-20251001` and `claude-sonnet-4-6` are both live
  (verified 2026-08-12). No migration needed; `claude-sonnet-5` is an
  optional upgrade, not a fix.

**Enabling changes:**

- `zotero_io.find_group_by_name(name)` — **promote to public** (agreed
  with user). Implement over the existing private
  `_list_accessible_groups(api_key, user_id)`; `user_id` is written
  automatically by `/setup` (`wizard.py:_verify_zotero` persists
  `userID` from `api.zotero.org/keys/<key>`), with a fallback to
  re-deriving it the same way. Error on >1 name match; never guess.
- `zotero_io.create_collection` / `delete_collection` — neither
  exists today (only `delete_item`). **Coordination note for the
  cleanup branch:** these are *not* redundant with zotero-mcp /
  `zotero-cli`. The "zotero-cli evaluation (2026-07-19)" entry in
  House-keeping already settled it — zotero-cli costs ~1.5–2 s startup
  per invocation, has no batch-by-keys mode, no reliable `--json`, and
  no HTTP-412 retry, so pipeline-shaped code stays on pyzotero.
- *(Optional, high leverage)* **`ZOTERO_PREFER_REMOTE=1`** — an env
  check inside `ZoteroClient.__init__` covers every construction path
  (`from_args`, `from_config`, `for_user_library`) in ~3 lines,
  default unchanged. Removes both sync waits and the Zotero-desktop
  requirement, making the run a single unattended command — and
  unblocks headless / CI / Antigravity users who have no desktop.
- *(Optional)* **tiered runs** so a first run needs 2 keys, not 8:
  `search` (~1 min, no Zotero writes — would have caught D1),
  `oa` (~4 min, `ZOTERO_API_KEY` + `ANTHROPIC_API_KEY` only),
  `full` (~10 min, adds Scopus/WoS + Elsevier/Wiley/Springer).

**Cost / runtime:** ~5–12 min. Scopus/WoS negligible at 3 ISSNs × 1
year; OpenAlex + S2 free; OpenAlex Content API $0.01/download only if
the earlier cascade fails, capped at 8; 8 Haiku calls on title+abstract;
`--limit 3` Sonnet calls on full PDF text (the dominant cost). The
driver prints a per-stage token tally so spend is measured, not guessed.

**Assertions:** never absolute counts (search results drift) — copy the
four test templates into `<run-id>/scripts/` (matching
`test_common.py`'s `PROJECT_ROOT` = parent of the script dir) and run
`test_systematic_review.py` for PRISMA arithmetic, no duplicate DOIs,
`search_run.json` ↔ CSV agreement, decision-state whitelists,
`temperature=0`, config round-trip, ghost handling, and `SLR Coding`
notes on every `fulltext:include`.

---

## Defects found while designing / implementing L1

- **D1 — fixed (2026-08-12).** `templates/test_systematic_review.py`
  failed against any real pipeline run.
  `test_search_metadata_has_required_fields` asserted `"queries" in
  meta`, but [search.py:236](scripts/pipelines/search.py#L236) writes
  the key as `query_defs`. Fixed by asserting `query_defs` OR
  `block_a_terms` (search.py writes one or the other depending on
  whether the project's `search_config.py` declares `QUERY_DEFS`
  (Scopus/WoS) or `BLOCK_A_TERMS`/`BLOCK_B_TERMS`
  (OpenAlex/Semantic-Scholar-only projects) — asserting the literal
  Scopus/WoS key unconditionally would have just traded one false
  failure for another. Same commit also fixed a second bug in the same
  test function, found by L1's own `FROM_YEAR == TO_YEAR = 2019`
  fixture: `assert meta["from_year"] < meta["to_year"]` used a strict
  `<`, rejecting the legitimate single-year-window case. Now `<=`.
  Files: [templates/test_systematic_review.py](templates/test_systematic_review.py)
  (~L105-118), [scripts/pipelines/search.py](scripts/pipelines/search.py#L236).

- **D3 — found while implementing L1, deliberately left unfixed.**
  `skills/systematic-review/SKILL.md` documents (around L644-645):
  "`fulltext_code.py` processes items tagged `abstract:include` OR
  `abstract:borderline`." The script itself does no such filtering —
  `main()` builds `to_code` from every collection item not yet tagged
  `fulltext:*` ([fulltext_code.py](scripts/pipelines/fulltext_code.py),
  `to_code` construction in `main()`), with no reference to
  `abstract:*` tags anywhere in the file. The doc line describes the
  *intended* pipeline behavior (skill + agent together), which relies
  on the agent computing the abstract-include/borderline key set itself
  and passing `--only-keys` (SKILL.md L410 already documents combining
  `--only-keys` with `fulltext_code.py`) — it is not something the
  script enforces on its own. A caller that skips this step would
  full-text-code items that failed abstract screening.
  `mini_slr.py`'s `code` stage does this correctly (queries the
  collection for `abstract:include`/`abstract:borderline` tags after a
  local-sync wait, then passes the resulting key set via `--only-keys`)
  precisely because it has to stand in for that agent step — see
  `stage_code` in [scripts/dev/mini_slr.py](scripts/dev/mini_slr.py).
  Left unfixed here because it's outside L1's authorized scope (not
  named in BACKLOG's L1 "Enabling changes", and changing
  `fulltext_code.py`'s default item selection is a behavior change to a
  shipped, tested script that deserves its own review — either add the
  filter to the script itself, matching the doc, or fix the doc to
  describe the two-step contract accurately). Re-evaluate as its own
  item.
  Files: [scripts/pipelines/fulltext_code.py](scripts/pipelines/fulltext_code.py),
  [skills/systematic-review/SKILL.md](skills/systematic-review/SKILL.md#L644-L645).

- **D2** — dev dependencies have two sources of truth and they have
  drifted. `pyproject.toml`'s `[dependency-groups] dev` lists only
  `pytest`, `responses`, `ruff`, `zotero-mcp-server`, while
  `.github/workflows/ci.yml` installs a longer hand-maintained pip
  line adding `tenacity`, `pyzotero`, `httpx`, `anthropic`,
  `google-genai`. A fresh `uv sync` therefore **cannot collect 14 test
  modules** (`ModuleNotFoundError: tenacity`), even though CI is green
  — so the documented `uv run pytest` / `.venv/bin/pytest` workflow
  (see S11) does not reproduce from `pyproject.toml`.
  **What it would take:** add the five to the `dev` group and switch
  CI to `uv sync --group dev` for one source of truth.
  Files: [pyproject.toml](pyproject.toml), `.github/workflows/ci.yml`.

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

- **S22** — "a cluster is not a provider" (batch-screening design
  decision, 2026-08-16).
  The obvious-looking design was `provider = "cluster"` alongside
  `anthropic`, `openai`, `gateway` and the two local servers. It was
  rejected, and the reasoning is worth keeping because the shape will
  look tempting again. A `ProviderSpec` describes something the plugin
  can *call*: it has an address, a credential, a listable set of models,
  and a health probe that either answers or does not. A batch scheduler
  has none of those. There is nothing to probe, nothing to list, and no
  answer while the script waits — the defining property of the whole
  provider abstraction. Modelling it as one would have meant a provider
  whose `verify` cannot verify, whose `list_models` cannot list, and
  whose `generate` returns hours later or not at all, which pushes a
  fiction through every surface that reports provider status.
  The execution mode is orthogonal to the provider instead: emit and
  apply need **no** provider at all, and the same manifest runs on a
  cluster, on a laptop, or through a provider the plugin has never heard
  of.
  **Reopen if** a real asynchronous Batch API becomes the target — the
  hosted ones have a real endpoint, a real credential, a real submit and
  a real poll, so they are callable and therefore genuinely provider-
  shaped. That is a different thing from a scheduler, and it should be
  modelled differently.
  Files: [scripts/pipelines/batch_manifest.py](scripts/pipelines/batch_manifest.py),
  [scripts/core/providers.py](scripts/core/providers.py).

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

- **S13** — `--full-recode` clears every targeted item's stage tag and
  then codes nothing, because the refresh read is stale.
  Found twice while live-validating B1 (`fulltext_code.py`, 2026-08-16).
  The flag writes tag removals through the **Zotero Web API**, then
  immediately re-reads the collection through the **local** API at
  `localhost:23119`, which has not synced yet. `_already_tagged` on that
  stale read still sees `fulltext:include`, so `to_code` comes out empty
  and the run prints "Nothing to code." and exits 0 — having already
  destroyed the tags. Re-running a minute later works, so the damage is
  recoverable, but the failure is silent, destructive, and looks like a
  no-op. `abstract_screen.py --full-recode` has the same shape.
  Options: re-read through the Web API after a write-then-read (correct,
  but slower and quota-bearing); or drop the refresh entirely and
  subtract the just-cleared keys from `tagged` locally, since the script
  already knows exactly which items it untagged — that is the cheap fix
  and needs no network at all.

- **P16** — the OA aggregators can attach a preprint without saying so.
  Found while implementing `--allow-preprints` (Change 2, layer C3).
  `PreprintSource` is off by default and tags everything it produces
  `pdf:preprint-version`, but `UnpaywallSource` takes
  `best_oa_location.url_for_pdf` and `OpenAlexSource` takes
  `open_access.oa_url` with **no version filter at all**, and both run
  in the default cascade. Unpaywall's `best_oa_location` can be a
  `submittedVersion` on arXiv or SSRN, so a preprint can already land
  untagged, by the default path, today. That makes the new opt-in a
  partial guard rather than a complete one.
  The fix is cheap in code — both APIs report `version`, and the hosts
  are already recognised by `fetchers.preprint.preprint_server_for` —
  but it is a **coverage decision, not a bug fix**: filtering
  `submittedVersion` out of the default cascade would drop PDFs some
  reviews currently get, and tagging them instead changes what those
  reviews' audits report. Deliberately not decided here. Two options
  when it is picked up: (a) tag rather than reject, so coverage is
  unchanged and the coding stage sees the same warning it now gets for
  `PreprintSource`; (b) reject unless `--allow-preprints`, which makes
  the flag mean what it says. (a) is the smaller change and probably
  the right one.

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

- **P12** — Semantic Scholar rate-limit model is inverted in our docs,
  and our bulk pacing exceeds the API-key ceiling.
  Found during the 2026-08-13 S2 API review (see House-keeping below for
  the verified API facts). Two halves, one root cause:

  - **Defect.** `RATE_LIMIT_SLEEP = 0.5` in
    [searchers/semantic_scholar.py:32](scripts/pipelines/searchers/semantic_scholar.py#L32)
    paces bulk pagination at 2 req/s. The current introductory API-key
    limit is **1 RPS on all endpoints**, so a user who follows our own
    wizard's advice and configures a key trips 429s *more* than one who
    runs anonymous. Not fatal — the 429 handler at
    [L116-119](scripts/pipelines/searchers/semantic_scholar.py#L116-L119)
    sleeps 5 s and retries — but it is needless churn on exactly the long
    paginated runs the bulk endpoint exists for. Note the comment on that
    line (`# unauthenticated tier is aggressive`) records the *opposite*
    of the real constraint: 0.5 s was chosen for the anonymous tier, and
    the keyed tier is the stricter of the two.
  - **Wrong prose in two places.** The module docstring at
    [searchers/semantic_scholar.py:10-13](scripts/pipelines/searchers/semantic_scholar.py#L10-L13)
    says unauthenticated is "1 rps shared across all unauthenticated
    callers" and that a key "moves you into the per-user higher tier".
    The wizard's `impact` text at
    [wizard.py:355-357](scripts/setup/wizard.py#L355-L357) repeats the
    "much lower rate limit" framing. Both describe a tier structure that
    is backwards: unauthenticated is a **1000 RPS pool shared globally**
    (throttled further under load), a key is a **dedicated 1 RPS**. The
    honest pitch for a key is *determinism*, not throughput — anonymous
    calls 429'd within a few requests during the review, so the shared
    pool is visibly saturated in practice.

  **What it would take:** raise `RATE_LIMIT_SLEEP` to `1.0` when a key
  is present (the `api_key` is already resolved in `run()` and threaded
  into `_fetch_all`, so this is a parameter, not new plumbing), keep the
  faster anonymous pacing, and rewrite the two prose blocks to describe
  shared-pool-vs-dedicated rather than low-tier-vs-high-tier. Consider
  also surfacing the ~60-day inactive-key pruning (see House-keeping) in
  the wizard's `where=` text — a user who configures a key during
  `/setup` and then runs no review for two months will find it dead with
  no signal from us.
  **Why deferred:** not urgent (current behavior degrades rather than
  fails), and it touches a shipped searcher plus user-facing wizard copy,
  so it deserves its own review rather than riding along with unrelated
  work.
  Files: [scripts/pipelines/searchers/semantic_scholar.py](scripts/pipelines/searchers/semantic_scholar.py)
  (L10-13, L32, L94-132), [scripts/setup/wizard.py:345-360](scripts/setup/wizard.py#L345-L360).

- **S14** — multi-model array sweeps as a first-class workflow.
  Deferred out of the batch-screening pass (2026-08-16). The mechanism
  already exists — `MODELS` is colon-separated and indexed by
  `$SLURM_ARRAY_TASK_ID`, so `--array=0-2` runs three models over one
  manifest — but nothing downstream knows what to do with three response
  files for one `run_id`. Applying them all would tag each item three
  times, last write winning silently.
  **What it would take:** a decision about what a sweep *is*. If the
  purpose is choosing a model, the output is a comparison, not a set of
  tags, and apply should refuse a sweep outright and point at S15. If
  the purpose is an ensemble, someone has to define the adjudication
  rule and where the disagreement is recorded.
  **Why deferred:** it is a research-design question wearing a plumbing
  costume, and getting it wrong writes decisions into a review's audit
  trail. The first pass ships the capability and stops short of blessing
  a use for it.
  Files: [scripts/cluster/run_batch.sbatch](scripts/cluster/run_batch.sbatch),
  [scripts/pipelines/batch_manifest.py](scripts/pipelines/batch_manifest.py).

- **S15** — an agreement mart: one cluster run against one API run, on
  the same manifest.
  Deferred out of the batch-screening pass (2026-08-16). The single most
  useful thing a user can do before trusting an open-weight model with a
  corpus is screen ~50 papers both ways and look at the disagreements —
  and every input for it already exists, because a manifest is the same
  file whoever executes it.
  **What it would take:** a reader that joins two response files on
  `request_id`, plus percent agreement and Cohen's κ per decision class,
  and a listing of the disagreements with both reasons side by side.
  Roughly a hundred lines and no new dependency; the work is deciding
  where the artefact lives and whether it belongs in the manuscript's
  methods (it does — it is a validation, and `empirical-integrity`
  would want it generated rather than hand-typed).
  **Why deferred:** it is a new output artefact with its own reporting
  conventions, and the first pass had no real cluster run to compare
  against yet.

- **S16** — per-document request grouping in the manifest.
  Deferred out of the batch-screening pass (2026-08-16). The batch-
  inference literature groups requests by document so a serving engine
  can reuse the shared prefix. `write_manifest` already sorts by
  `(item_key, ordinal)` in anticipation, but nothing emits more than one
  request per item, so the `ordinal` field is always 0.
  **What it would take:** a stage that asks several questions of one
  full text — per-construct coding is the obvious candidate — plus an
  applier that merges N answers into one coding note.
  **Why deferred:** no caller needs it. Note the inversion recorded in
  `write_manifest`'s docstring: here the *system prompt* is the shared
  prefix and the document is the per-item part, so the ordinary
  system-then-user layout is already prefix-optimal and a naive port of
  the literature's grouping gets it backwards.
  Files: [scripts/pipelines/batch_manifest.py](scripts/pipelines/batch_manifest.py).

- **S17** — shipped per-site module profiles for `SITE_ENV`.
  Deferred out of the batch-screening pass (2026-08-16), and deliberately
  so: `SITE_ENV` is the one file the user writes, and
  `tests/unit/test_cluster_is_generic.py` exists to keep any site's
  `module load` lines out of this repository. A profile directory would
  be the most reasonable-sounding way to erode that.
  **What it would take:** a place for profiles that is not this
  repository — a documented convention for a user-local
  `~/.config/academic-research/cluster/<name>.sh`, discovered by name
  rather than shipped — plus wizard support for selecting one.
  **Why deferred:** the value is real (every user at one site writes the
  same six lines) but it is a distribution problem, and shipping the
  contents here is the wrong answer to it.

- **S18** — vLLM guided decoding for the full-text coding stage.
  Deferred out of the batch-screening pass (2026-08-16). Coding demands
  strict JSON from what may be an 8B–30B open-weight model, and every
  parse failure costs a whole GPU pass to repeat — which is why the
  skill requires a ~10-item pilot first. Guided decoding is the actual
  fix: vLLM can constrain generation to a JSON schema, so the model
  cannot emit unparseable output at all.
  **What it would take:** deriving a JSON schema from `coding_fields`
  (already frozen into the manifest, so the schema can travel with it),
  a `guided_json` field on the request row, and a runner that passes it
  to `SamplingParams` when the installed vLLM supports it and ignores it
  otherwise.
  **Why deferred:** it puts a vLLM-version-dependent feature into the
  manifest schema, and the manifest's whole value is that it is readable
  by anything. Wants its own pass, after a real corpus has shown how bad
  the parse-failure rate actually is.
  Files: [scripts/cluster/run_batch.py](scripts/cluster/run_batch.py),
  [scripts/pipelines/fulltext_code.py](scripts/pipelines/fulltext_code.py).

- **S19** — two-argument `KeySpec.verify`.
  Deferred out of the gateway pass (2026-08-16) and untouched by the
  cluster pass. `verify` takes a key and probes a hosted endpoint; a
  provider whose *address* is also user-supplied needs both, and the
  three surfaces that build a URL each work around it differently. The
  cluster path sidestepped the whole area by having no `KeySpec` at all —
  an automation level is a policy statement, not a credential — which is
  also why it needed no `test_live_coverage.py` entry.
  **What it would take:** `verify(key, base_url)` across every spec, and
  a re-read of the two guards that were re-anchored on
  `(toml_section, toml_key)` when the gateway turned out to have no
  environment variable.
  **Why deferred:** it is a refactor of a signature every provider
  implements, for the benefit of one provider that currently works.

- **S20** — the `ca_bundle` contingency: setup probes and the run path
  do not share a TLS trust store.
  Deferred out of the gateway pass (2026-08-16); relevant to any
  institution-hosted endpoint, including a cluster's. `scripts/setup/`
  is stdlib-only and probes with `urllib`; the run path goes through
  `openai` → `httpx`. On a site with an internal CA, or with corporate
  TLS interception, a fix applied to one leaves the other broken — and
  the failure is asymmetric in the worst direction, where the probe
  passes and the run fails.
  **What it would take:** a `[llm] ca_bundle` setting honoured by both,
  which for `urllib` means an `ssl.SSLContext` and for `httpx` means a
  client-level `verify=`.
  **Why deferred:** speculative until someone hits it. Recorded so that
  when they do, the asymmetry is the first thing read rather than the
  last thing discovered.

- **S23** — a crashed vLLM job still reports `RUNNING`, so scheduler
  state is not evidence of progress.
  Found during live validation on a real cluster (2026-08-16), not by
  us: reported by a peer session running a working vLLM batch pipeline
  on the same site, with measurements. vLLM v1 runs its engine in a
  separate process; an unhandled exception kills the driver while the
  child survives holding the GPU, and `srun` will not finish the step
  until the cgroup empties. `set -euo pipefail` exits the shell, not the
  orphan. Their job sat `RUNNING` for **15 minutes having died at 90
  seconds**, and would have held two GPUs for the full reservation.
  Compounding it: **vLLM logs to stderr**, so a crashed job's `.out`
  ends tidily at "init engine … took NN seconds" and looks healthy —
  the 15 minutes were spent reading the wrong file.
  **The direction that matters.** `tests/live/test_cluster_batch.py`'s
  `_wait_for` infers *absence from `squeue`* → finished, which is sound:
  a job that has left the queue really is done. The unsound inference is
  the opposite one — *presence* → still working — and that is what
  [skills/cluster-screening/SKILL.md](skills/cluster-screening/SKILL.md)
  tells the agent to do when it polls `squeue` once per turn, and what
  any future progress or health reporting built on that poll would
  inherit. So this is a skill-guidance defect first and a test
  robustness improvement second.
  **What it would take:** two one-line checks, either of which would
  have caught it in 90 seconds — on exit, tail the `.err` for a
  traceback, and separately assert the responses file has the expected
  number of lines. Plus a sentence in the skill saying scheduler state
  is not evidence of progress, and that `.err` is where vLLM failures
  are.
  **Why deferred:** not reproduced here — our three live runs never
  orphaned an engine — and this branch's remaining scope was validation,
  not hardening. Recorded with the peer's measurements so the next
  person does not pay the 15 minutes to rediscover it.
  Files: [tests/live/test_cluster_batch.py](tests/live/test_cluster_batch.py),
  [skills/cluster-screening/SKILL.md](skills/cluster-screening/SKILL.md),
  [scripts/cluster/README.md](scripts/cluster/README.md).

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
- **P13** — adopt Semantic Scholar's `/paper/search/match` in the
  abstract fetcher instead of hand-rolling title matching.
  `_fetch_by_title` in
  [fetchers/semantic_scholar.py:47-65](scripts/pipelines/fetchers/semantic_scholar.py#L47-L65)
  hits `/paper/search?limit=5` and then compares DOIs client-side to
  pick a hit. S2 has shipped a purpose-built endpoint for this —
  `/graph/v1/paper/search/match` returns the single best title match
  plus a `matchScore` (verified live 2026-08-13, HTTP 200). Swapping to
  it would drop the 5-result fan-out and the manual compare loop, and
  give a confidence score we currently have no equivalent of.
  **Why deferred:** the current path works and is covered by tests; the
  gain is tidiness plus a score we don't yet consume. Worth doing when
  next touching this fetcher — likely alongside P12, which edits the
  sibling searcher module. Check first whether `matchScore` is
  calibrated well enough to replace or feed
  [_title_match.py](scripts/pipelines/fetchers/_title_match.py)'s
  `matches()` threshold; if it is, this shrinks two modules, not one.
  Files: [scripts/pipelines/fetchers/semantic_scholar.py](scripts/pipelines/fetchers/semantic_scholar.py),
  [scripts/pipelines/fetchers/_title_match.py](scripts/pipelines/fetchers/_title_match.py).

- **S12** — evaluate S2's `/snippet/search` for `verifying-citations` /
  `fact-check`.
  `/graph/v1/snippet/search` searches text snippets across S2's
  full-text corpus and returns matching passages. That is close to a
  purpose-built primitive for what
  [verifying-citations/SKILL.md](skills/verifying-citations/SKILL.md)
  actually does — decide whether a cited paper contains support for a
  specific claim — where today the staged procedure goes abstract first,
  then full text via Zotero, which needs the PDF to be in the library at
  all. A snippet query could sit between those stages: cheaper than
  full-text retrieval, far more specific than an abstract, and it works
  for papers the user has no PDF for.
  **Why deferred:** unverified. The endpoint exists in the v1 swagger
  but returned 429 on unauthenticated probes during the 2026-08-13
  review, so its response shape, corpus coverage, and snippet quality
  were never actually inspected. Evaluate with a real key before
  designing anything. Two known constraints to check against: S2's
  agreement with Springer excludes those abstracts from the API
  (unclear whether the same applies to snippets), and a keyed account is
  capped at 1 RPS, which matters because fact-check dispatches parallel
  per-citation subagents — N concurrent citation checks would serialize
  against a 1 RPS ceiling. That interaction may sink the idea on its
  own; establish it early.
  Files: [skills/verifying-citations/SKILL.md](skills/verifying-citations/SKILL.md),
  [skills/fact-check/SKILL.md](skills/fact-check/SKILL.md).

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

- **Semantic Scholar API review (2026-08-13)** — triggered by the
  "new API" framing on
  https://www.semanticscholar.org/product/api. **There is no new API
  version and no migration to do.** Recorded here so nobody re-runs
  these probes. Verified live:

  - `graph/v2` → **404**. `graph/v1/swagger.json` reports
    `swagger: 2.0`, `basePath: /graph/v1`, `title: Academic Graph API`,
    `version: 1.0`. `recommendations/v1` and `datasets/v1` swagger both
    200. The three advertised services are the ones that already
    existed; every base URL this repo ships is current.
  - Our exact bulk-search param shape from
    [searchers/semantic_scholar.py:102-115](scripts/pipelines/searchers/semantic_scholar.py#L102-L115)
    still returns 200 with a valid continuation `token`.
  - Full v1 path list (14): `/paper/search`, `/paper/search/bulk`,
    `/paper/search/match`, `/paper/batch`, `/paper/autocomplete`,
    `/paper/{id}`, `/paper/{id}/authors|citations|references`,
    `/author/search`, `/author/batch`, `/author/{id}`,
    `/author/{id}/papers`, `/snippet/search`. The last two of interest
    to us — `search/match` and `snippet/search` — are unused here; see
    P13 and S12.
  - **What actually changed is the rate plan, not the API**:
    unauthenticated is a 1000 RPS pool shared across *all* anonymous
    callers (throttled further under load), while an API key's
    introductory limit is 1 RPS on all endpoints. This is the opposite
    of what our docs claim — see **P12**.
  - `paper/DOI:<doi>` 404s for some legitimate DOIs (e.g.
    `10.1038/nature14539`) and 200s for others. Corpus coverage gaps,
    not breakage; `fetch_abstract` already falls back to title search,
    so no action needed.
  - **The public changelog is dead.**
    [allenai/s2-folks/API_RELEASE_NOTES.md](https://github.com/allenai/s2-folks/blob/main/API_RELEASE_NOTES.md)
    now opens with "RELEASE NOTES DISCONTINUED" and stops at November
    2024. There is nothing left to watch for breaking changes; the API
    Service Status Page linked from the product page is the only
    remaining signal. Standing constraints from those final notes, all
    still in force: abstracts for Springer papers are excluded from the
    API by agreement (mitigated here — `enrich_abstracts.py` treats S2
    as one source in a cascade), exponential backoff is required, keys
    are not issued to free email domains or third-party apps, and
    **keys inactive for ~60 days are pruned automatically**.

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
