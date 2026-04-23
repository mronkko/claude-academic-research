# CLAUDE.md

This file gives Claude Code the context it needs to edit this research
report without re-discovering the layout every session. Adapt
placeholders in angle brackets for your project.

This template is for **manuscript-only projects** — editing an existing
paper, chapter, thesis, or research report written in Quarto, R
Markdown, LaTeX, or plain Markdown. If you're running a full systematic
review (search → screen → code → write), use `sr_claude_md.md` instead.

## What this project is

<One paragraph: the paper's research question, scope, target venue.
Keep it short. The purpose is to let the critics assess whether the
manuscript answers its own stated question.>

## Layout

- `manuscript/manuscript.qmd` — authoring source. Adjust the extension
  to match your setup (`.Rmd`, `.tex`, `.md`, `.ipynb`).
- `manuscript/references.bib` — bibliography. If you manage citations
  through Zotero, generate via
  `${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/generate_bib.py`; otherwise
  keep it hand-curated but never hand-edit entries for keys that
  Better BibTeX owns.
- `analysis/manuscript_stats.py` *(optional)* — project-owned flat-dict
  builder. Its `build_stats()` function is imported by the manuscript's
  setup chunk and called live at render time. Skip if the paper has
  no computed statistics.
- `analysis/results/manuscript_stats.json` *(optional)* — the JSON the
  builder writes for inspection and the content-integrity test. Never
  hand-edit; regenerate via `python3 analysis/manuscript_stats.py`.
- `analysis/results/` *(optional)* — authoritative CSV / JSON outputs
  the manuscript's code chunks read from. Every number in prose traces
  back through this directory.
- `scripts/test_citations.py`, `scripts/test_empirical_integrity.py`,
  `scripts/test_common.py` — the project's regression tests. One file
  per skill.

## Test command

Run both before every milestone (render, co-author review, submission):

```bash
python3 scripts/test_citations.py && \
  python3 scripts/test_empirical_integrity.py
```

`critic-loop`'s Step 1 test gate runs them in this order. If any test
fails, diagnose and fix before rendering — don't skip the gate.

## Render command

```bash
quarto render manuscript/manuscript.qmd --to gfm
```

Produces `manuscript/manuscript.md` for the critic-loop snapshot.
For R Markdown, LaTeX, or Jupyter notebooks, override via
`/critic-loop --render-cmd '…' --rendered-path …` — see the
`critic-loop` skill's *Rendering: what and why* for the per-format
commands.

## House style

- Every citation in prose is a `[@BBT_KEY]`. Never hand-craft keys
  (`Smith2019`-style); never write a bare *Author (YYYY)* mention
  without a governing `@key`. See the `grounded-citations` skill.
- Every number in prose is an inline expression
  (`` `{python} s['key']` ``), never hand-typed. See the
  `empirical-integrity` skill.
- Labels for cross-references: `{#tbl-*}`, `{#fig-*}`, `{#sec-*}`.
  Referenced as `@tbl-foo`, etc. Hard-coded numbers like "Table 3"
  break when content is reordered.

## Forbidden literals

Add project-specific hand-typed values to the `FORBIDDEN_LITERALS`
tuple at the top of `scripts/test_empirical_integrity.py` — model
names, version strings, specific counts, date stamps that would
silently drift. The test greps prose for these and fails if any appear
literally, enforcing the inline-expression rule.
