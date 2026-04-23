# CLAUDE.md

This file gives Claude Code the context it needs to work on this
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

## Zotero library

*Populate during the systematic-review bootstrap — the agent will
ask `mcp__zotero__zotero_list_libraries` and offer options.*

- **Group ID:** `<numeric id>`
- **Collection key:** `<8-char Zotero key>`   (omit if collection is
  created fresh at import time)

All pipeline scripts take `--group <id>` and (where supported)
`--collection <key>` as explicit CLI flags. Do not set
`ZOTERO_GROUP` as an env var — the canonical record is here.

## API keys

All API keys (`ZOTERO_API_KEY`, `ANTHROPIC_API_KEY`,
`ELSEVIER_API_KEY`, `WOS_API_KEY_EXTENDED`, etc.) live in
`~/.config/academic-research/config.toml` — never read or inspect
that file from Claude Code.

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
