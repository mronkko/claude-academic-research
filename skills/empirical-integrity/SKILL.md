---
name: empirical-integrity
description: Use when editing manuscripts (.qmd, .Rmd, .ipynb, .tex, .md) that contain numbers, statistics, tables, test results, or methodology facts (search dates, model names, keyword strings). Enforces the rule that every quantitative or methodological claim in prose must come from a pipeline-generated authoritative file via code chunk or inline expression — never hand-typed.
---

# Empirical integrity

## Bootstrap (first run in this project)

Before applying the rules below, check that this skill's regression
tests are installed in the project:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup/check_project_scaffold.py" \
    scripts/test_common.py scripts/test_empirical_integrity.py
```

If the output lists missing files, install them:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup/install_templates.py" \
    test_common.py:scripts/test_common.py \
    test_empirical_integrity.py:scripts/test_empirical_integrity.py
```

Then tell the user what was installed and flag that the top of
`scripts/test_empirical_integrity.py` has project-specific settings
(paths to manuscript / stats / figures, plus the `FORBIDDEN_LITERALS`
tuple) they should review.

Next, check whether the project's `.claude/settings.json` has the
`analysis/results/` deny rules that protect `manuscript_stats.json`
from hand-editing:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup/check_deny_rules.py" \
    "Write(//**/analysis/results/**)" "Edit(//**/analysis/results/**)"
```

If the output lists missing rules, add them via the shipped helper:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup/add_deny_rules.py" \
    "Write(//**/analysis/results/manuscript_stats.json)" \
    "Edit(//**/analysis/results/manuscript_stats.json)" \
    "Write(//**/analysis/results/**)" \
    "Edit(//**/analysis/results/**)"
```

The helper is idempotent (no-ops for rules already present) and
creates `.claude/settings.json` if it doesn't exist.

These rules block Claude's `Write` and `Edit` tools from touching
anything under `analysis/results/` (including `manuscript_stats.json`
and `coded_papers.csv`). Regeneration via
`Bash(python3 analysis/manuscript_stats.py)` is unaffected because the
deny rules target only `Write`/`Edit`, not `Bash`.

If the project has no `CLAUDE.md` yet, suggest using
`${CLAUDE_PLUGIN_ROOT}/templates/manuscript_claude_md.md` as a starting
point — but don't write it without the user's say-so. CLAUDE.md is
user-owned.

## Core rule

Never write a number, table, statistic, percentage, test result, or
quantitative claim that does not come directly from a script reading the
underlying data. Do not round, adjust, or approximate data values.

This rule extends to **methodological facts**: search dates, database
names, model names, keyword strings, year bounds, screening counts, and
any other statement that describes how the data was produced. A search
date typed as "October 2025" is just as much a hallucination risk as a
p-value typed as "0.03". Methodological facts must come from a metadata
file written by the pipeline (e.g. `search_metadata.json`) and reach
the manuscript through the stats dictionary — never typed in prose,
never read directly from the raw metadata file. See *Inline
expressions* below for the pattern.

## The authoritative pipeline

Every quantitative claim in a research report must trace back through
this chain:

```
analysis/raw/  →  analysis scripts  →  analysis/results/  →  manuscript
                                                             (code chunks or inline expressions)
```

**`analysis/results/` is the single source of truth for numbers cited
in the manuscript.** The directory is written exclusively by analysis
scripts — typically several of them, each owning a slice of the output
(e.g. model-fitting, descriptive statistics, figures, and the
`manuscript_stats.py` flat-dict builder). Analysis scripts routinely
produce far more files than the manuscript cites — exploratory
outputs, supplementary tables, diagnostic plots. That is fine. The
subset that *ends up in the manuscript* is whatever the code chunks
and inline expressions actually read; the rest lives in
`analysis/results/` as a record of the analysis but has no special
status until a code chunk or inline expression pulls from it.

The manuscript itself reads from these files — it never reads raw
data directly, never modifies them, and never produces numbers of its
own.

> **Project-specific paths:** Check CLAUDE.md. If the project defines
> different locations, use those. Default conventions: raw data in
> `analysis/raw/`, authoritative outputs in `analysis/results/`,
> analysis scripts in `analysis/`, stats-dictionary builder at
> `analysis/manuscript_stats.py`.

## Two acceptable ways to put numbers in the manuscript

### 1. Code chunks that render tables from authoritative results

A manuscript code chunk can render a table two ways, both acceptable
as long as every value traces back to `analysis/results/` and the
chunk contains presentation logic only, never analysis logic (no
model fitting, no recomputation, no data transformation).

**Pre-baked CSV or markdown.** The analysis script writes a ready-to-
print CSV or markdown file, and the chunk reads and emits it. Robust
because the manuscript has no dependency on analysis-time packages
beyond a minimal reader; portable across render environments; plain
text is human-inspectable.

```python
# Quarto / Python
import pandas as pd
df = pd.read_csv("analysis/results/table2_results.csv")
print(df.to_markdown(index=False))
```

```r
# Quarto / R
df <- read.csv("analysis/results/table2_results.csv")
knitr::kable(df, caption = "...")
```

**Stored estimation result formatted inline.** The analysis script
writes a stored object — a fitted model, a summary list, an `.rds`
or `.pkl` file — and the chunk formats it with a publication-quality
table package: `modelsummary`, `gt`, `kable`, `tinytable`,
`stargazer`, or `pandas.to_markdown`. No CSV intermediate. Use when
you want control over table features that are awkward to pre-bake —
model-comparison columns, fit-statistic footers, coefficient-star
conventions, custom rounding per column — and when the render
environment has the same package versions as the analysis environment.

```r
# Quarto / R — formatting a stored model fit with modelsummary
library(modelsummary)
fit <- readRDS("analysis/results/fit_main.rds")
modelsummary(fit, output = "markdown", stars = TRUE)
```

Either way, the chunk **must** read from the authoritative results
directory — not from raw data, not from intermediate files. If the
file does not exist yet, run the analysis script that produces it
first (check the `analysis/` directory for the matching producer —
the convention is one script per coherent output or output family).

### 2. Inline expressions from a pipeline-produced stats dictionary (preferred for prose numbers)

Every prose number comes from a **single flat dictionary** produced
by a project-owned stats module — typically `analysis/manuscript_stats.py` with a
`build_stats()` function — and written to
`analysis/results/manuscript_stats.json`. The module reads every relevant
pipeline output (raw data, screening logs, `search_metadata.json`,
coding tables, model config) and returns one dotted-key mapping:

```python
{
  "search.from_year": 2010,
  "search.databases": "Scopus, Web of Science, OpenAlex",
  "search.unique_dois": 1708,
  "screen.abstract.n_include": 143,
  "screen.fulltext.n_include": 81,
  "provenance.fulltext.model": "claude-sonnet-4-6",
  "provenance.fulltext.prompt_version": "v1-2026-04-14",
  # ...
}
```

The manuscript loads the dict once in a setup chunk and looks up keys
inline:

| System | Setup chunk | Prose lookup |
|---|---|---|
| Quarto | `from manuscript_stats import build_stats; s = build_stats()` | `` `{python} s['search.unique_dois']` `` |
| R Markdown | `stats <- read_stats()` | `` `r stats$search.unique_dois` `` |
| Jupyter | `s = build_stats()` in a prior cell | variable reference |

**One dictionary, not two.** All pipeline provenance — search dates,
database names, model identifiers, prompt versions, year bounds —
lives in the same dictionary under namespaced keys (`search.*`,
`provenance.*`). The manuscript never reads `search_metadata.json`,
`search_run.json`, or screening logs directly; the stats module is the
only consumer of those raw artefacts. If you discover a fact the
manuscript needs that isn't in the dict, add it to `build_stats()` and
re-run, not a new variable.

**The dictionary is flat on purpose.** Nested dicts silently return
`None` on typos (`s['screen']['xxx']` when `xxx` doesn't exist); flat
dotted keys fail loudly (`s['screen.xxx']` → `KeyError`). Loud failure
at render time is the point — the same invariant that
`test_empirical_integrity.py` catches statically (`inline s['…']`
keys resolve in `build_stats()`). Render-time and test-time enforcement
of one rule.

Never write a literal number in prose that comes from the data (e.g.,
"the mean was −2.3%").

> **Starting point.** `${CLAUDE_PLUGIN_ROOT}/templates/manuscript_stats.py` is a
> worked example of `build_stats()` for an SLR project — it ingests
> `search_metadata.json`, `search_run.json`, the screening logs, and
> `coded_papers.csv` and emits the flat dict above.
> `${CLAUDE_PLUGIN_ROOT}/templates/manuscript.qmd` shows the setup
> chunk and inline-expression patterns end to end.

### Ownership and lifecycle

`analysis/manuscript_stats.py` is **project-owned**. The plugin ships a
worked example under `${CLAUDE_PLUGIN_ROOT}/templates/`; you copy it
into `analysis/` as your starting point and extend `build_stats()`
every time the manuscript needs a new methodology fact or derived
number. The plugin has no way to regenerate it for you — it is not a
shipped pipeline script you can invoke via
`${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/...`, it is *your* code.

Two invariants protect the dictionary's integrity as you extend it:

1. **Only `python3 analysis/manuscript_stats.py` may write
   `analysis/results/manuscript_stats.json`.** Never Edit or Write
   the JSON directly, and never hand-write any file under
   `analysis/results/`. If a key is missing from the dict, add the
   derivation to `build_stats()` and regenerate via the CLI. The
   project's `.claude/settings.json` carries a permission deny rule
   enforcing this at the tool level — see the Bootstrap section.
2. **Every value in `build_stats()`'s return dict must trace to a
   pipeline artefact**, not a typed literal. Acceptable sources: (a)
   reading a pipeline file (`search_metadata.json`, screening CSVs,
   `coded_papers.csv`, `references.bib`); (b) file-system metadata
   (`os.path.getmtime`, file size); (c) a subprocess call to a
   pipeline tool (`git log`, `wc -l`, a Zotero MCP query). Never a
   literal value typed inline. A hardcoded `result["x.y"] = 42` in
   `build_stats()` is the exact same hallucination as a hand-typed
   number in prose — it's just been pushed one file upstream.

The `test_empirical_integrity.py` content-integrity test asserts that
the on-disk JSON equals `build_stats()`'s live output, catching both
staleness and tampering. The deny rule catches invariant (1) at the
tool layer. Invariant (2) is self-policed — Claude must read what
`build_stats()` contains before extending it and refuse to add a
literal.

The same ownership pattern applies to `manuscript/manuscript_tables.py`
(pandas functions for code chunks) and `manuscript/manuscript.qmd`
(the scaffold) — all three are per-project artefacts the template
seeds and the researcher owns.

## What is never acceptable

- A static Markdown table with typed-in data values.
- A literal number in prose not driven by an inline expression.
- A code chunk reading from raw data or intermediate files instead of
  `analysis/results/`.
- Rounding or adjusting a value from the script output.
- A hand-typed methodological fact (search date, database name, model
  name, keyword string) that exists as a pipeline-generated metadata
  entry. If the pipeline knows it, the manuscript must read it.
- Edit or Write on `analysis/results/manuscript_stats.json` (or any
  file under `analysis/results/`) from Claude's tools. The project's
  `.claude/settings.json` denies these — see the Bootstrap section.
  Only `python3 analysis/manuscript_stats.py` may produce these files.
- A hardcoded literal value inside `build_stats()` — e.g.,
  `result["x.y"] = 42`. Every returned value must trace to a pipeline
  artefact, file metadata, or a subprocess call. Hardcoded literals in
  the producer are the same hallucination as hand-typed numbers in
  prose, just pushed one file upstream.

## Before writing any results or discussion

1. Run every analysis script whose output feeds the chapter —
   typically `analysis/manuscript_stats.py` plus whichever table /
   model / figure producers are involved. The goal is that
   `analysis/results/` is fully up to date for the claims you are
   about to write.
2. Confirm the output files exist in `analysis/results/` before
   writing any code chunk or inline expression that reads from them.
3. After writing or editing a chapter with numbers, re-run the
   relevant analysis scripts and verify every figure in the prose
   still matches. `test_empirical_integrity.py` catches the subset
   that goes through the stats dictionary; hand-verify the rest.

## Dynamic cross-references

Never hard-code table, figure, or section numbers — they break when
content is reordered. Use the rendering system's native labelling
mechanism:

| Concept | Quarto | R Markdown |
|---------|--------|------------|
| Assign table label | `{#tbl-label}` on the table | `\\label{tab:label}` inside `kable()` |
| Reference table | `@tbl-label` | `\\ref{tab:label}` |
| Assign figure label | `{#fig-label}` on the figure | `fig.cap` + `\\ref` |
| Assign section label | `{#sec-label}` on heading | not native |

Jupyter does not have native cross-reference support; use a numbering
convention defined in CLAUDE.md.

## Test suite

Run the project's test suite before each milestone (chapter completion,
submission, supervisor review). Check CLAUDE.md for the exact test
command.

The plugin ships three fine-grained test templates under
`${CLAUDE_PLUGIN_ROOT}/templates/`, each mapped to one skill. Copy into
your project's `scripts/` directory:

| Template | Skill | Catches |
|---|---|---|
| `test_empirical_integrity.py` | **this skill** | Forbidden methodology literals; unique Quarto / Rmd labels; inline `s['…']` key resolution against the live `build_stats()` dict; figure-file existence; `manuscript_stats.json` content matches `build_stats()` |
| `test_citations.py` | `grounded-citations`, `fact-check` | `@citekey` resolution in `references.bib`; no bare *Author (YYYY)* without `@key`; BBT-key uniqueness in `coded_papers.csv` |
| `test_systematic_review.py` | `systematic-review` | PRISMA arithmetic; `search_run.json` ↔ dedup CSV; decision-state whitelists; `temperature=0` pinning; `screening_config` round-trip |

All three depend on `templates/test_common.py` — copy it alongside.

Pure manuscript-editing projects (no SLR pipeline) copy only
`test_empirical_integrity.py`, `test_citations.py`, and `test_common.py`.
Full SR projects copy all four.

Rendering is a further integrity gate — a broken `s['key']` or an
unresolved `@citekey` crashes the renderer louder than any grep. See
`critic-loop`'s *Rendering: what and why* subsection.
`test_empirical_integrity.py --with-render` invokes the renderer
explicitly; critic-loop renders on every iteration regardless.

Any test failure is a blocking issue. Do not proceed until resolved.

**Grow the suite with the project.** When you discover a new
hallucination or drift pattern this skill's rule would prevent —
a new forbidden literal, a new inline-expression convention, an
overlooked label or figure-path form — add a test to
`scripts/test_empirical_integrity.py` before closing out the task.
The failure becomes the sentinel so the same class of mistake can't
silently return.

## Qualitative tables

Tables that are purely descriptive (no counts or percentages) are exempt
from the data-pipeline rule but must be marked:

```
<!-- qualitative table — not data-derived -->
```

## Qualitative research with coded data

The pipeline still applies:

- Coded excerpts live in a single authoritative CSV (e.g.,
  `analysis/raw/coded_excerpts.csv`).
- All counts and percentages derive from that CSV via the table
  generation script.
- Code chunks read the output CSVs from `analysis/results/`, not the raw
  coding CSV directly.

## Red flags

- You are about to write a literal number in prose without an inline
  expression.
- A code chunk reads from raw data or intermediate files instead of
  `analysis/results/`.
- A static Markdown table contains data values.
- A number in the manuscript does not appear in the authoritative
  summary statistics file.
- A test reports a mismatch between a manuscript table and its
  authoritative CSV.
- You are about to Edit or Write `analysis/results/manuscript_stats.json`
  — **never**. Regenerate via `python3 analysis/manuscript_stats.py`.
  If a key is missing, extend `build_stats()` in
  `analysis/manuscript_stats.py` and re-run the CLI.
- You are about to add a literal value inside `build_stats()` —
  e.g. `result["screen.n_included"] = 81`. **Never.** Every value must
  trace to a pipeline artefact, file metadata, or a subprocess call.
  If the pipeline doesn't know the number, extend the pipeline first.
