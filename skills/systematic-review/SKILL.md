---
name: systematic-review
description: Use when running a full systematic literature review (SLR) — PRISMA-style search, screening, coding, and export. End-to-end pipeline from scripted Scopus/WoS search → Zotero import → abstract fetch → PDF attach → Claude-driven abstract and full-text screening → QA evaluator agents → human adjudication in Zotero → export to manuscript. Trigger phrases: "systematic review", "SLR", "PRISMA", "screen papers", "code the included papers", "run the search", "full-text screening". For isolated Zotero enrichment work (adding abstracts, attaching PDFs to an existing library) that is NOT part of a full SLR pipeline, use the `zotero-operations` skill instead. Targets social-sciences research; medical-SLR instruments (RoB 2, ROBINS-I, evidence hierarchies I–VII, PRISMA-P) are out of scope.
---

# systematic-review

## Pre-flight (ALWAYS run first)

Before any step below, verify the plugin has been configured:

```bash
test -f ~/.config/academic-research/config.toml && echo "configured" || echo "NOT CONFIGURED"
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
- **Zotero is the canonical manifest.** Scripts never delete items from
  Zotero. See the `zotero-operations` skill for Zotero-specific patterns.
- **Fix the data, don't work around it.** When a script hits records
  missing a DOI / ISSN / abstract, pause and surface the items. Missing
  DOIs are usually a data-capture bug (search-API field not mapped, manual
  entry, non-journal item). Do not add silent title-match fallbacks until
  the user confirms the data is genuinely unfixable.
- **Resumable stages.** Each stage writes an append-only CSV log. On
  start, scripts read the log, build a "done" set, and skip processed
  items. Every stage survives Ctrl+C.
- **Progress the user can follow.** Pipeline scripts use `flush=True` on
  every print; emit `[N/total]` counters; invoke via `| tee` to a log
  file. Never pipe to `/dev/null`.
- **Filterable.** Every reusable script accepts `--filter-keys-file`
  (one Zotero item key per line) so the next stage drives from the
  previous stage's decision log.

## Environment variables

| Variable | Used by | Purpose |
|----------|---------|---------|
| `ZOTERO_API_KEY` | All scripts | Zotero API authentication (required) |
| `ZOTERO_GROUP` | All scripts | Zotero group library ID (per-project, set in the project's own CLAUDE.md or shell) |
| `ANTHROPIC_API_KEY` | Screening scripts | Claude API (required for LLM screening) |
| `ELSEVIER_API_KEY` | `attach_pdfs.py` | Elsevier/ScienceDirect full-text retrieval |
| `SCOPUS_API_KEY` | Search scripts | Scopus API (often same as `ELSEVIER_API_KEY`; some institutions issue separately) |
| `WILEY_TDM_TOKEN` | `fetch_pdfs_wiley_tdm.py` | Wiley TDM UUID token |
| `OPENALEX_API_KEY` | PDF + abstract scripts | OpenAlex Content API ($0.01/download, paid) |
| `SEMANTIC_SCHOLAR_API_KEY` | `fetch_abstracts.py` | Semantic Scholar (higher rate limit with key) |
| `CROSSREF_MAILTO` | All scripts | Crossref polite pool (any email) |
| `WOS_API_KEY_EXTENDED` | Search scripts | WoS Expanded (full Boolean, `IS=` works) — **prefer this** |
| `WOS_API_KEY` | Search scripts | WoS Starter (field-limited, no `IS=`) — piloting only |

The `/setup` skill writes these to `~/.config/academic-research/config.toml`
(mode 0600) on first run. Environment variables take precedence over the
file.

## Pipeline scripts

All scripts live under `${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/`. Invoke
with `uv run`:

```bash
uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/import_to_zotero.py --project <dir>
uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/fetch_abstracts.py --project <dir>
uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/attach_pdfs.py --project <dir>
uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/abstract_screen.py --project <dir>
uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/fulltext_screen.py --project <dir>
uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/export_coded_includes.py --project <dir>
```

Project-specific scripts (search, export shape, test suite) live in the
project's own `scripts/` directory. The plugin ships a template at
`${CLAUDE_PLUGIN_ROOT}/templates/sr_claude_md.md` for new SLR projects.

## Key methodological rules

### Search

- **Always use WoS Expanded (`WOS_API_KEY_EXTENDED`)** for the formal
  search — Starter's `IS=` ISSN filter returns 0 results.
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

Cascade: publisher TDM API (Elsevier, Wiley) → Crossref TDM → PMC →
OpenAlex Content → Unpaywall → OpenAlex OA metadata.

- **Always validate `%PDF` magic bytes** *and* parse-test the PDF
  before caching. Some downloaders save HTML-with-200 or corrupted PDFs.
- **Cloudflare**: HTTP clients cannot solve the JS challenge. For
  CF-gated publishers (Sage, OUP, T&F, Emerald), use
  `${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/fetch_pdfs_browser.py`
  (Playwright; user passes CF once per publisher, script downloads the
  rest in the authenticated session).
- Disable Chromium's built-in PDF viewer via a `user_data_dir` with
  `plugins.always_open_pdf_externally=true` in Preferences — otherwise
  PDFs open inline and neither `expect_download` nor `expect_response`
  captures the bytes.

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

### Post-screening QA

After every automated screening or coding run, launch three evaluator
agents **in parallel** (single message, multiple Agent tool calls):

1. **Inclusion validator** — every row decided `include`, with
   decision reason and key coding fields. Flags false positives;
   distinguishes HARD (clearly fails criterion) from SOFT (borderline).
2. **Exclusion validator** — stratified sample across exclusion codes
   (6–8 per code). Flags false negatives. Also flags `WRONG_CODE` cases
   where the exclusion stands but the code is wrong.
3. **Coding quality validator** — random sample of included papers with
   **all** coding fields shown in full. Checks for bare labels,
   missing citations where theories are named, fabrication risk,
   inconsistency, thin/vague entries. Ends with a ship-it verdict.

Evaluator agents **flag**, they do **not** re-decide. The human
adjudicates. Flagged items get two Zotero tags: `qa-flag` (sentinel) and
one of `qa-hard` / `qa-soft-include` / `qa-soft-exclude` (severity).
Existing `fulltext:include` / `fulltext:exclude` tags stay until the
human decides.

After human review, replace `qa-*` severity tags with
`qa-adjudicated-include` / `qa-adjudicated-exclude`. If the decision
**flips**, append a new row to the decision CSV (last-row-wins picks it
up); re-run export. If the decision **kept**, the CSV needs no change.

Every adjudication writes one line to `screening/qa_review.md` under an
`## Adjudication log` section:

> `{item_key}` **{short citation}** — **{kept DECISION / flipped to
> DECISION}** — rationale. *(YYYY-MM-DD)*

This log is the methods-section evidence for the final manuscript.
Without it, the adjudication is not reproducible.

## Data integrity

These rules supplement the `empirical-integrity` skill with SR-specific
patterns:

- **Auto-extract script constants** into `search_metadata.json`. Never
  import scripts (side effects); parse with
  `re.search(r'CONSTANT\s*=\s*"([^"]+)"', source)`. Keywords, year
  bounds, model names all live in the metadata file; the manuscript
  reads them via inline expressions.
- **Forbidden methodology literals.** The project's test suite must
  grep the manuscript for hand-typed search dates, model names
  (`claude-haiku`, `claude-sonnet`), keyword strings, year bounds.
  These must use inline expressions from `search_metadata.json`.
- **PRISMA arithmetic test.** `include + borderline + exclude = total
  screened`; `coded include + exclude = total coded`. Catches missing
  items or pipeline drops.
- **Search integrity gatekeeper.** `search_run.json` records the
  canonical count of unique DOIs from the scripted search. Post-import
  invariant: Zotero DOIs == search DOIs. Abort if extras exist (items
  added outside the pipeline).

## Test suite patterns

See `empirical-integrity` for the baseline. SR-specific additions:

| Test | What it catches |
|------|-----------------|
| Results files exist and non-empty | Pipeline didn't run |
| Count consistency (stats JSON vs. raw CSV) | Export script bug or stale outputs |
| BBT keys non-empty and unique | Missing or duplicate citation keys |
| Qualitative table markers present | Empirical-integrity compliance |
| Script constant round-trip | Metadata staleness |
| PRISMA arithmetic | Screening funnel inconsistency |
| Forbidden methodology literals in manuscript | Hand-typed dates, models, keywords |
| Screened count == search unique count | Pipeline residue |
| No duplicates in target collection | Import dedup gaps (use `mcp__zotero__zotero_find_duplicates`) |
| Search run marker verified | Stale or missing integrity gatekeeper |
| Temperature=0 pinned in Claude API calls | Reproducibility regression |

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
