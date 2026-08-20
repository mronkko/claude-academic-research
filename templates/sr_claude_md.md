# CLAUDE.md

This file gives Claude Code and Antigravity the context they need to work on this
systematic review without re-discovering the layout every session.
Adapt placeholders in angle brackets for your project.

## What this project is

<One paragraph: research question, scope, target venue. Keep it short.
The purpose is to let the evidence and argument critics assess whether
the manuscript answers its own stated question.>

## Layout

- `manuscript/manuscript.qmd` — authoring source (Quarto). Inline
  expressions call `build_stats()` live at render time; pipeline
  provenance from `search_metadata.json` is folded into the same dict.
  No hand-typed methodology numbers.
- `manuscript/references.bib` — generated from Zotero via
  `${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/generate_bib.py`. Do not
  hand-edit.
- `analysis/manuscript_stats.py` — project-owned flat-dict builder;
  produces `analysis/results/manuscript_stats.json`. Extend as the
  manuscript needs new facts. Never hand-edit the JSON output — the
  `.claude/settings.json` deny rules block direct `Write`/`Edit`.
- `manuscript/manuscript_tables.py` — pandas-based table functions
  called from Quarto code chunks.
- `search_config.py` — journal list, queries, year bounds.
- `screening_config.py` — system prompts, model and prompt-version
  constants.
- `scripts/test_citations.py`, `scripts/test_empirical_integrity.py`,
  `scripts/test_systematic_review.py`, `scripts/test_common.py` —
  the project's regression tests. One file per skill.

## Test command

Run all three before every milestone (manuscript render, supervisor
review, submission):

```bash
python3 scripts/test_citations.py && \
  python3 scripts/test_empirical_integrity.py && \
  python3 scripts/test_systematic_review.py
```

`critic-loop`'s Step 1 test gate runs them in this order. If any test
fails, diagnose and fix before rendering — don't skip the gate.

## Render command

```bash
quarto render manuscript/manuscript.qmd --to gfm
```

Produces `manuscript/manuscript.md` for the critic-loop snapshot.
Override via `/critic-loop --render-cmd '…' --rendered-path …` if your
output path differs.

## Pipeline stages

```
search.py → import_to_zotero.py → enrich_abstracts.py → enrich_pdfs.py →
abstract_screen.py → fulltext_code.py → QA evaluators → human
adjudication → export_coded_includes.py → generate_bib.py → manuscript
```

Every stage is a shipped script under
`${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/`. Never improvise Bash or
Python that touches the Zotero API or handles API keys — invoke the
named script. See the `systematic-review` skill for the full matrix of
invocations.

## Autonomous runs

"Work autonomously" is honoured, and it changes *when* you ask, not
*whether*. Search, import and enrichment run unattended — the database
APIs cost nothing per call. Before the **first paid LLM call**
(`abstract_screen.py`, `fulltext_code.py`), stop once and put a single
proposal to the user:

- the scope summary,
- the model proposed for each stage, with the comparison that justifies
  it (newest of the required tier; never a dated `-YYYYMMDD` snapshot
  when a newer sibling exists),
- the item count,
- the projected cost from an actual `--dry-run`.

One "proceed" covers every remaining stage. If nobody answers, run up
to the paid stage and stop there with the proposal on screen.

Autonomy never covers: writing outside the library below, deleting
items, hand-composed Zotero metadata, hand-typed numbers in the
manuscript, or a stage that would cost materially more than what was
approved.

## Zotero library

*Populate during the systematic-review bootstrap — the agent will
ask `mcp__zotero__zotero_list_libraries` and offer options.*

- **Library:** group (or `user` for personal)
- **Group ID:** `<numeric id>`   (omit if `Library: user`)
- **Collection key:** `<8-char Zotero key>`   (omit if collection is
  created fresh at import time)

All pipeline scripts take `--group <id>` (group library) or `--user`
(personal library) and, where supported, `--collection <key-or-name>`
as explicit CLI flags. Do not set `ZOTERO_GROUP` as an env var — the
canonical record is here.

If Zotero Desktop is not running on this machine (headless, container,
CI), add `--remote` to **every** stage: reads default to Desktop's
local server and it answers "no items" rather than erroring when it is
absent or has not yet synced.

**Never write Zotero items from metadata you composed yourself.** New
items come from `import_to_zotero.py` (database-retrieved rows) or from
an identifier-based add (`mcp__zotero__zotero_add_item` with a
DOI/ISBN/URL, `zotero-cli add doi`). Typing out a citation, even to
repair one bad record, is a defect signal — say what is missing
instead.

For Zotero housekeeping on a *different* library or group than the
one above — adding abstracts, attaching PDFs, fixing BBT keys, finding
duplicates, etc. — use the `zotero-operations` skill. It runs the same
pipeline scripts, parameterized by `--group <id>` or `--user`,
independent of this project's configured library.

## API keys

All API keys (`ZOTERO_API_KEY`, `ANTHROPIC_API_KEY`,
`ELSEVIER_API_KEY`, `WOS_API_KEY_EXTENDED`, etc.) live in
`~/.config/academic-research/config.toml` — never read or inspect
that file from Claude Code or Antigravity.

## Screening defaults

- Haiku for abstract screening, Sonnet for full-text coding.
- Temperature=0 pinned in both; the test suite asserts it.
- Append-only screening logs; last-row-wins on `item_key`.

## House style

- Every citation in prose is a `[@BBT_KEY]` from Zotero. Never
  hand-craft keys; never write a bare *Author (YYYY)* mention.
- Every number in prose is an inline expression
  (`` `{python} s['screen.n_included']` ``), never hand-typed.
- See the `grounded-citations` and `empirical-integrity` skills for
  the full rule books.
