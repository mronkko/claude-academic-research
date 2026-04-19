---
name: empirical-integrity
description: Use when editing manuscripts (.qmd, .Rmd, .ipynb, .tex, .md) that contain numbers, statistics, tables, test results, or methodology facts (search dates, model names, keyword strings). Enforces the rule that every quantitative or methodological claim in prose must come from a pipeline-generated authoritative file via code chunk or inline expression — never hand-typed.
---

# Empirical integrity

## Core rule

Never write a number, table, statistic, percentage, test result, or
quantitative claim that does not come directly from a script reading the
underlying data. Do not round, adjust, or approximate data values.

This rule extends to **methodological facts**: search dates, database
names, model names, keyword strings, year bounds, screening counts, and
any other statement that describes how the data was produced. A search
date typed as "October 2025" is just as much a hallucination risk as a
p-value typed as "0.03". Methodological facts must come from a metadata
file written by the pipeline (e.g. `search_metadata.json`), surfaced as
inline expressions just like data-derived numbers.

## The authoritative pipeline

Every quantitative claim in a research report must trace back through
this chain:

```
analysis/raw/  →  generate_tables.py  →  analysis/results/  →  manuscript
                                                               (code chunks or inline expressions)
```

**`analysis/results/` contains the single source of truth.** It is
written exclusively by the table generation script. The manuscript reads
from it — it never reads raw data directly or modifies these files.

> **Project-specific paths:** Check CLAUDE.md. If the project defines
> different locations, use those. Default conventions: raw data in
> `analysis/raw/`, authoritative outputs in `analysis/results/`, table
> generation script at `analysis/generate_tables.py`, stats module at
> `analysis/stats.py`.

## Two acceptable ways to put numbers in the manuscript

### 1. Code chunks reading authoritative CSVs (preferred for tables)

A code chunk that reads from `analysis/results/` and renders a table is
correct:

```python
# Quarto example
import pandas as pd
df = pd.read_csv("analysis/results/table2_results.csv")
print(df.to_markdown(index=False))
```

The chunk **must** read from the authoritative results directory — not
from raw data, not from intermediate files. If the CSV does not exist
yet, run the table generation script first.

### 2. Inline expressions from the stats module (preferred for prose numbers)

Prose statistics must use inline expressions driven by the stats module,
not literal values:

| System | Syntax |
|--------|--------|
| Quarto | `` `{python} s['key']` `` |
| R Markdown | `` `r stats$key` `` |
| Jupyter | Variable in prior code cell, referenced in narrative |

Never write a literal number in prose that comes from the data (e.g.,
"the mean was −2.3%").

## What is never acceptable

- A static Markdown table with typed-in data values.
- A literal number in prose not driven by an inline expression.
- A code chunk reading from raw data or intermediate files instead of
  `analysis/results/`.
- Rounding or adjusting a value from the script output.
- A hand-typed methodological fact (search date, database name, model
  name, keyword string) that exists as a pipeline-generated metadata
  entry. If the pipeline knows it, the manuscript must read it.

## Before writing any results or discussion

1. Run the table generation script to produce the authoritative CSVs and
   summary statistics file.
2. Confirm the output files exist in `analysis/results/` before writing
   any code chunk or inline expression.
3. After writing or editing a chapter with numbers, re-run the script
   and verify every figure still matches.

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
submission, supervisor review). Check CLAUDE.md for the test suite
location and runner command.

If no test suite exists yet, **create one**. Building a test suite is
strongly preferred over manual verification. At minimum, tests should
cover:

| What to test | What it catches |
|---|---|
| Citation keys in manuscript vs. reference manager | Missing or mismatched Zotero entries |
| Dynamic labels (`@tbl-*`, `@fig-*`, `@sec-*`) | Broken cross-references; hard-coded numbers |
| Numbers in manuscript vs. authoritative CSVs | Hallucinated or stale statistics |
| Metadata file values vs. source script constants | Drift between pipeline config and its self-description |
| Grep for forbidden methodology literals in manuscript | Hand-typed search dates, model names, keyword strings |
| Figure files referenced in text | Missing image files |
| Full build (PDF/HTML render) | Compilation errors |

Any test failure is a blocking issue. Do not proceed until resolved.

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
