---
name: fact-check
description: Use when the user asks to fact-check a manuscript, verify citations, audit sources, or check whether a paper's cited sources actually support the claims made about them. Trigger phrases: "fact-check", "verify citations", "audit citations", "check the sources", "do these papers actually say that", "verify the numbers in this draft". Runs citation-by-citation verification against Zotero / MCP-retrieved sources, and quantitative claims against the authoritative results file. Produces a one-shot report. Do NOT use during or immediately after a `/critic-loop` run — the evidence critic inside that loop performs the same verification as part of iterative revision; invoking fact-check on top of it duplicates the work and spends MCP / Zotero quota twice. Use `critic-loop` for verification during revision rounds; use `fact-check` for pre-submission audits, standalone spot-checks, or a focused citation-only pass without the other critic perspectives.
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
`.claude/critic-loop/iter-*/` directories exist and were written in
the last few minutes), ask whether they want to wait for the loop to
finish, or proceed with fact-check as an additional audit.

**When to prefer which.**

- **`critic-loop`** — during revision rounds; citations are one of
  four concerns (with method, argument, expert) and the loop applies
  fixes iteratively.
- **`fact-check`** — pre-submission audit, supervisor hand-off,
  journal-submission checklist, or a deliberate citation-only
  spot-check without the other three critic perspectives. Produces a
  durable report at `.claude/fact-check/report.md` that the author can
  share.

## Invocation

The user will typically say something like:
- "fact-check the methods section of the manuscript"
- "verify the citations in chapter 2"
- "do these sources actually support the claims"
- "audit the citations against Zotero"

If the user did not name a target document, ask before proceeding.

## Procedure

### 1. Identify the target

Resolve the document path. If the project has a rendered build (Quarto,
R Markdown), **fact-check the rendered output**, not the authoring
source — the rendered form shows resolved inline expressions and
citations as the reader will see them.

### 2. Extract claims

Walk the target document and extract two lists:

- **Cited claims** — each `@citekey` or `[@citekey]` reference with
  ~20 words of surrounding context.
- **Quantitative claims** — numbers, percentages, p-values, effect
  sizes, sample sizes, date ranges mentioned in prose.

### 3. Verify cited claims

For each `@citekey`:

1. Resolve the key to a Zotero item via
   `mcp__zotero__zotero_search_by_citation_key`.
2. Fetch source content in order of preference:
   - `mcp__zotero__zotero_get_item_fulltext` when a full-text PDF is
     attached.
   - `mcp__zotero__zotero_get_item_metadata` for the abstract (often
     sufficient).
   - `mcp__openalex__get_work` or `mcp__semantic-scholar__get-paper-abstract`
     if Zotero has no abstract.
3. Compare the manuscript's claim against the source. Check:
   - Does the paper exist under that key?
   - Does the attributed finding match the abstract / full text?
   - Is the direction of the claim correct (especially for regression
     coefficients, moderators, mediators)?
   - Is any quoted text actually in the paper? Paraphrases are OK;
     fabricated quotes are not.

Classify each citation:

- **VERIFIED** — claim matches source.
- **MINOR** — overreaching paraphrase, oversimplified finding, missing
  caveat the source considers important.
- **MAJOR** — missing paper, wrong paper for the key, direction
  reversal, fabricated finding, fabricated quote, claim not supported.

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

### 5. Spot-check scaling

For manuscripts with > 30 citations, spot-checking is acceptable.
Prioritize:

- Directional claims (A "increases", "predicts", "is higher than" B).
- Quoted passages.
- Statistics cited from specific papers.
- Claims that support the manuscript's core contribution.

Report sample size in the first report entry.

## Report format

Write the report to `.claude/fact-check/report.md`. Create the
directory first if needed:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup/ensure_dir.py" .claude/fact-check
```

Report layout:

```markdown
# Fact-check report

**Document:** <path>
**Mode:** full / spot-check (n=<N> of <total>)
**Date:** <YYYY-MM-DD>

## Summary

| Classification | Cited claims | Quantitative claims |
|---|---:|---:|
| VERIFIED | N | N |
| MINOR | N | N |
| MAJOR | N | N |

## MAJOR issues (fix before submission)

1. `@citekey` in section X: <the claim>.
   Source: <what the paper actually says>.
   Recommended fix: <replace with supported claim / remove / find different source>.

...

## MINOR issues (tighten when convenient)

...

## VERIFIED (sampled)

- `@citekey1`, `@citekey2`, ... — spot-checked and match sources.
```

## Reporting to the user

Write a concise summary (~100 words) pointing at the report file, with
MAJOR counts and one-line description of each MAJOR issue. Do not paste
the full report into the chat.

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

- You are about to mark a hand-typed prose number VERIFIED because it
  happens to match the results file today — mark MAJOR regardless
  (empirical-integrity violation).
- You are skipping citation checks because "the authors are reputable".
- You are verifying against training memory instead of an MCP fetch or
  Zotero fulltext — the `grounded-citations` rule applies.
- The paper's abstract contradicts the manuscript claim, and you are
  softening the flag to MINOR because the contradiction is
  "interpretive". Direction reversals are MAJOR.
- You are about to read `~/.config/academic-research/config.toml` via
  `cat`, `head`, `tail`, `grep`, `less`, `more`, `awk`, `sed`, a
  Python script, or any other command. **NEVER read that file.** It
  holds API keys. No part of a fact-check audit needs those keys in
  your context.
