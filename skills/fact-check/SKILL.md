---
name: fact-check
description: Use when the user asks to fact-check a manuscript, verify citations, audit sources, or check whether a paper's cited sources actually support the claims made about them. Trigger phrases: "fact-check", "verify citations", "audit citations", "check the sources", "do these papers actually say that", "verify the numbers in this draft". Runs citation-by-citation verification against Zotero / MCP-retrieved sources, and quantitative claims against the authoritative results file. Unlike the `critic-loop` evidence critic, this skill is a standalone one-shot audit with no revision loop.
---

# fact-check

## Pre-flight (ALWAYS run first)

Before any step below, verify the plugin has been configured:

```bash
test -f ~/.config/academic-research/config.toml && echo "configured" || echo "NOT CONFIGURED"
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

This skill is the standalone cousin of the `critic-loop` evidence
critic. The evidence critic fires inside the revision loop; fact-check
runs one-shot when the user explicitly asks for a citation/claim audit.

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

Write the report to `/tmp/fact-check/report.md`:

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

## Red flags

- You are about to mark a hand-typed prose number VERIFIED because it
  happens to match the results file today — mark MAJOR regardless
  (empirical-integrity violation).
- You are skipping citation checks because "the authors are reputable".
- You are verifying against training memory instead of an MCP fetch or
  Zotero fulltext — the `mcp-research` rule applies.
- The paper's abstract contradicts the manuscript claim, and you are
  softening the flag to MINOR because the contradiction is
  "interpretive". Direction reversals are MAJOR.
- You are about to read `~/.config/academic-research/config.toml` via
  `cat`, `head`, `tail`, `grep`, `less`, `more`, `awk`, `sed`, a
  Python script, or any other command. **NEVER read that file.** It
  holds API keys. No part of a fact-check audit needs those keys in
  your context.
