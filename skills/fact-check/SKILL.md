---
name: fact-check
description: Use when the user asks to fact-check a manuscript, verify citations, audit sources, or check whether cited papers actually support the claims attributed to them. Trigger phrases "fact-check", "verify citations", "audit citations", "check the sources", "do these papers actually say that", "verify the numbers in this draft". Do NOT use during or immediately after `/critic-loop` — the evidence critic inside that loop covers the same ground and burns MCP / Zotero quota twice.
---

# fact-check

## Pre-flight (ALWAYS run first)

Before any step below, verify the plugin has been configured:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup/check_configured.py"
```

If the result is `NOT CONFIGURED`, stop immediately and tell the user:

> The academic-research plugin has not been set up on this machine
> yet. Run `/setup` first — fact-check depends on Zotero and MCP
> citation lookups, which require API keys and MCP servers that
> `/setup` configures.

Do not call MCP tools or proceed with the audit. `/setup` is the
required first step.

If the result is `configured`, proceed.

---

## Bootstrap (first run in this project)

Before running the audit, check that the regression-test backstop
this skill relies on is installed in the project:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup/check_project_scaffold.py" \
    scripts/test_common.py scripts/test_citations.py
```

If the output lists missing files, install them:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup/install_templates.py" \
    test_common.py:scripts/test_common.py \
    test_citations.py:scripts/test_citations.py
```

Tell the user what was installed and flag that the top of
`scripts/test_citations.py` has project-specific paths they should
review. Then proceed with the audit.

---

## Relationship to `critic-loop`

This skill is the standalone cousin of the `critic-loop` evidence
critic. The evidence critic fires inside the revision loop; fact-check
runs one-shot when the user explicitly asks for a citation/claim audit.

**Mutual-exclusion rule.** If a `/critic-loop` session is currently
running, or just completed with no unresolved evidence-critic MAJORs,
fact-check is redundant — skip unless the user explicitly asks for a
second pass. The two cover the same ground for citations and
quantitative claims; running both on the same draft in the same
session burns MCP lookups twice without new information. If the user
invokes fact-check while a `critic-loop` is clearly in progress (e.g.
`critic-reviews/iter-*/` directories exist and were written in
the last few minutes), ask whether they want to wait for the loop to
finish, or proceed with fact-check as an additional audit.

**When to prefer which.**

- **`critic-loop`** — during revision rounds; citations are one of
  four concerns (with method, argument, expert) and the loop applies
  fixes iteratively.
- **`fact-check`** — pre-submission audit, supervisor hand-off,
  journal-submission checklist, or a deliberate citation-only
  spot-check without the other three critic perspectives. File mode
  produces a durable report at `fact-check-reports/report.md` the
  author can share; console mode prints an inline table for paragraph
  excerpts.

## Invocation

The user will typically say something like:
- "fact-check the methods section of the manuscript"
- "verify the citations in chapter 2"
- "do these sources actually support the claims"
- "audit the citations against Zotero"

If the user did not name a target document, ask before proceeding.

## Procedure

### 1. Identify the target and choose output mode

Resolve the document path. If the project has a rendered build (Quarto,
R Markdown), **fact-check the rendered output**, not the authoring
source — the rendered form shows resolved inline expressions and
citations as the reader will see them.

**Output mode.** Two modes, picked from the input shape:

| Input shape | Mode | Output |
|---|---|---|
| Quoted prose block in the prompt (no resolvable file path) | **console** | Compact inline table in chat. No report file. |
| File path (`manuscript.qmd`, `chapter.md`, …) | **file** | Durable report at `fact-check-reports/report.md` plus a ~100-word console summary. |

If both are present ("fact-check this paragraph from `manuscript.qmd`"),
the quoted block wins — the user is auditing that excerpt, not the
whole document. Announce the chosen mode at the start of the run
("Mode: console" or "Mode: file") so the user knows what to expect.

### 2. Extract claim mentions and group by citation

Walk the target document and extract three lists:

- **Cited claim mentions** — every `@citekey` / `[@citekey]` reference
  OR every *Author (YYYY)* / *(Author, YYYY)* prose mention, with
  ~20 words of surrounding context per mention. **Then group by the
  underlying citation** — the same paper cited in five places is one
  *citation* with five *mentions*, not five separate items. For
  `@citekey` form, the grouping is exact; for Author-Year prose in
  console mode, treat the surface form as the grouping key
  (subagent's resolution step will catch ambiguities).
- **Bare author–year mentions in rendered manuscripts (file mode
  only)** — *Author (YYYY)* prose without a governing `@citekey`
  inside a rendered `.qmd` / `.md` is **MAJOR automatically**: the
  manuscript should be using BBT keys, not raw author-year, and
  `scripts/test_citations.py` treats this as a regression class. In
  **console mode** the input is normal prose, not rendered output —
  Author-Year is the normal form, *not* an error.
- **Quantitative claims** — numbers, percentages, p-values, effect
  sizes, sample sizes, date ranges mentioned in prose.

### 3. Verify cited claims (per-citation parallel dispatch)

**REQUIRED SUB-SKILLS:**

- `verifying-citations` — defines the staged resolve-then-abstract-then-
  fulltext rule, the VERIFIED / MINOR / MAJOR / UNVERIFIABLE
  classification, the always-escalate triggers (quoted passages,
  specific statistics, method details, subgroup findings), the
  multiple-mentions handling, and the cross-mention consistency check.
  Every dispatched subagent loads it.
- `superpowers:dispatching-parallel-agents` — the pattern for fanning
  work out to subagents in a single assistant message.

**Decide audit scope** (once, before dispatching):

- ≤ 30 *unique citations* → audit every one.
- \> 30 unique citations → audit a sample. Sample size
  `n = max(20, ⌈0.25 × total_unique⌉)`. Quoted passages and specific
  statistics cited from a paper are **never sampled** — every citation
  containing one of these claim types is audited (all of its mentions).
  Sample the remainder, prioritising in order: citations whose
  mentions include directional claims about the manuscript's core
  contribution → other directional claims → topical/biographical
  references. Record `n` of `total_unique` in the first line of the
  output.

**Dispatch.** In a single assistant message, launch **one `Agent`
per unique citation** with `subagent_type="general-purpose"`. Each
subagent receives:

- The citation identifier — `@citekey` for rendered docs, the
  Author-Year surface form for console mode — and **every mention of
  it** with ~20 words of context each.
- Instructions to follow `verifying-citations`: Stage 0 (resolve once),
  Stage A (fetch abstract once), Stage C only if any mention requires
  it (fetch fulltext once); classify each mention independently; run
  the cross-mention consistency check at the end.
- The return contract from `verifying-citations` (per-mention blocks
  plus an optional cross-mention block).

The unit of work is the unique citation, not the mention — a paper
cited five times is one subagent with five mentions in its prompt,
not five subagents. This (a) fetches the source once and (b) lets the
subagent catch internal inconsistency in how the manuscript uses that
source. The main agent aggregates returned blocks directly — no
per-citation file writes (scale doesn't justify the disk-write
overhead that `critic-loop` uses for its four critics). Both
**console** and **file** modes use the parallel dispatch; the
difference is only in how the aggregate is presented.

### 4. Verify quantitative claims

For each number in prose:

1. Identify the expected source — usually a file under
   `analysis/results/` per the `empirical-integrity` skill.
2. Open the results file and check that the literal value appears.

Classify each:

- **VERIFIED** — number matches authoritative file.
- **MAJOR** — number absent from or inconsistent with the results file;
  or the number was hand-typed rather than produced by an inline
  expression.

Hand-typed numbers are MAJOR regardless of whether they happen to match
— they violate the empirical-integrity rule and will drift when the
pipeline re-runs.

## Report format

Two formats, picked by the output mode chosen in Step 1.

### File mode — `fact-check-reports/report.md`

Create the directory first if needed:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup/ensure_dir.py" fact-check-reports
```

Report layout:

```markdown
# Fact-check report

**Document:** <path>
**Mode:** file
**Sampled:** <n> of <total_unique> citations (or "all")
**Date:** <YYYY-MM-DD>

## Summary

| Classification | Cited-claim mentions | Quantitative claims |
|---|---:|---:|
| VERIFIED      | N | N |
| MINOR         | N | N |
| MAJOR         | N | N |
| UNVERIFIABLE  | N | — |

(Mentions are counted here, not unique citations — a paper cited
three times with one MAJOR and two VERIFIED contributes 1 MAJOR and
2 VERIFIED to the table.)

## MAJOR issues (fix before submission)

1. `@citekey` in section X: <the claim>.
   Source: <what the paper actually says>.
   Recommended fix: <replace / remove / find different source>.

...

## UNVERIFIABLE (cannot audit — resolve before submission)

1. `@citekey`: <the claim>. Reason: <e.g. no PDF attached to Zotero>.
   Recommended fix: run `enrich_pdfs.py` then re-audit, or replace the
   citation.

## Cross-mention inconsistency (one source used in contradictory ways)

(Omit this section if no subagent flagged a cross-mention finding.)

1. `@citekey` cited in sections X (claim Y) and Z (claim ¬Y).
   Source: <what the paper actually says>.
   Recommended fix: <reconcile the two mentions — drop one, qualify
   one, or pick a different source for one>.

## MINOR issues (tighten when convenient)

...

## VERIFIED (sampled)

- `@citekey1`, `@citekey2`, … — checked and match sources.
```

### Console mode — inline output only

For pasted-paragraph excerpts the entire output is an in-chat table; no
file is written. One row per unique citation in single-mention cases,
one row per mention when a citation appears more than once in the
excerpt (rare in a single paragraph):

```
Fact-check: <first ~10 words of the excerpt> …
Mode: console
Sampled: all (n=5 unique citations)

@citekey1  VERIFIED      — claim matches abstract.
@citekey2  MAJOR         — direction reversal vs paper's β = −0.23.
@citekey3  UNVERIFIABLE  — no PDF attached; run enrich_pdfs.py.
@citekey4  VERIFIED      — fulltext supports the claim.

Summary: 2 VERIFIED, 1 MAJOR, 1 UNVERIFIABLE.
Action: address MAJOR and UNVERIFIABLE before relying on this paragraph.
```

If any subagent returned a cross-mention finding, append a single
extra line:

```
CROSS-MENTION  @citekey   — used for X in §A and ¬X in §C; reconcile.
```

The console table *is* the report. Do not also write a file; the user
asked for an inline check.

## Reporting to the user

- **File mode** — write a concise summary (~100 words) pointing at
  `fact-check-reports/report.md`, with MAJOR and UNVERIFIABLE counts
  and a one-line description of each. Do not paste the full report
  into chat.
- **Console mode** — the inline table is the report. Do not also write
  a file, do not append a separate summary block (the table already
  ends with one).

## Regression backstop

Fact-check is a one-shot audit. The recurring companion is
`scripts/test_citations.py` (installed by Bootstrap above; source at
`${CLAUDE_PLUGIN_ROOT}/templates/test_citations.py`) — it catches the
regressions that would show up on the *next* audit: unresolved
`@citekey`s, bare *Author (YYYY)* mentions without a governing `@key`,
and BBT-key drift in `coded_papers.csv`. Run it in the `critic-loop`
test gate so fact-checkable issues never rebuild silently between
audits.

**Grow the suite with the audit.** Every MAJOR item in a fact-check
report is a regression class. Before closing out the audit, promote
each MAJOR into a test in `scripts/test_citations.py` (or
`scripts/test_empirical_integrity.py` for hand-typed numbers) if a
static check could catch it. The test failure then becomes the
sentinel for the next cycle — the audit's value compounds rather
than resetting each time.

## Red flags

(Citation-verification red flags — direction reversals, training-memory
fetches, abstract-only verification of specific stats — live in
`verifying-citations`. The flags here are scoped to fact-check's own
procedure.)

- You are about to mark a hand-typed prose number VERIFIED because it
  happens to match the results file today — mark MAJOR regardless
  (empirical-integrity violation; numbers must come from inline
  expressions).
- You are skipping citation checks because "the authors are reputable".
- You are about to write a report file in console mode, or skip the
  report file in file mode. Mode is decided in Step 1; do not switch
  mid-run.
- You are about to dispatch citation checks sequentially instead of in
  one parallel `Agent`-call batch — re-read
  `superpowers:dispatching-parallel-agents`.
- You are about to read `~/.config/academic-research/config.toml` via
  `cat`, `head`, `tail`, `grep`, `less`, `more`, `awk`, `sed`, a
  Python script, or any other command. **NEVER read that file.** It
  holds API keys. No part of a fact-check audit needs those keys in
  your context.
