---
name: verifying-citations
description: Use when verifying that a citation in a manuscript is honestly supported by its source — a sub-skill called by `fact-check` and by the evidence critic inside `critic-loop`. Loaded via `REQUIRED SUB-SKILL: verifying-citations` from a calling skill; not invoked standalone by end users. Defines the classification scheme, the staged abstract-then-fulltext rule, and the always-escalate triggers so both callers share one source of truth.
---

# verifying-citations

## Overview

A citation is verified when an externally consulted source (Zotero PDF fulltext or abstract — never training memory) directly supports the claim the manuscript attributes to it. Verification is staged: read the abstract first; escalate to full text only when the abstract is silent, ambiguous, or insufficient for the kind of claim being made.

This skill is the single source of truth for citation-verification doctrine. `fact-check` (one-shot audits) and the evidence critic in `critic-loop` (iterative revision) both load it so that when the rules change, two skills don't drift.

**Insertion vs verification.** The complementary skill `grounded-citations` governs *inserting* a citation during drafting — same externalised-consultation rule, but it escalates on **recency / context staling** (a faded context-window memory needs re-fetching). This skill governs *auditing* an existing citation, and it escalates on **claim type** — quoted passages, specific β coefficients, and method details always require fulltext regardless of what the abstract says. Same demand for grounding; different decision axis.

## Classification scheme

| Classification | When to assign |
|---|---|
| **VERIFIED** | The source directly supports the claim — sign, direction, finding, and magnitude all match. |
| **MINOR** | The claim overreaches the source: oversimplified finding, missing caveat the source considers important, paraphrase that mildly overstates. |
| **MAJOR** | The paper doesn't exist under that key; wrong paper for the key; direction reversal; fabricated quote; specific finding not supported by the source. Blocks publication. |
| **UNVERIFIABLE** | Verification requires the full text and no PDF is attached to the Zotero item. The issue is access, not accuracy — recommend running `enrich_pdfs.py` to populate the library, or replacing the citation. Callers may map UNVERIFIABLE to MAJOR for their own report when the workflow can't ship an unresolved citation (e.g. inside `critic-loop`). |

## Staged verification rule

### Stage A — resolve the key and fetch the abstract

1. `mcp__zotero__zotero_search_by_citation_key` — resolve the BBT key to a Zotero item key.
2. `mcp__zotero__zotero_get_item_metadata` — read the abstract.

If the key doesn't resolve at all, the citation is **MAJOR** (missing paper) — stop.

### Stage B — decide from what the abstract says

| Abstract says... | Action |
|---|---|
| Directly states the claim, with matching direction / sign / finding | VERIFIED — done. |
| Directly contradicts the claim (direction reversal, contrary finding) | MAJOR — done. |
| Is silent on the specific claim, or consistent but inconclusive | Escalate to Stage C. |

### Stage C — fetch the full text

`mcp__zotero__zotero_get_item_fulltext` — works when the PDF is attached. Verify against the body:

- Does the attributed finding match the full text?
- Is the direction of the claim correct (especially for regression coefficients, moderators, mediators)?
- Is any quoted text actually in the paper? Paraphrases are OK; fabricated quotes are not.

If `zotero_get_item_fulltext` returns no content (no PDF attached, OCR empty), classify **UNVERIFIABLE** with a message naming what's missing — e.g. *"no PDF attached for `@key`; run `enrich_pdfs.py` then re-run, or replace the citation."*

**Do not fall back to OpenAlex / Semantic Scholar / publisher MCPs inside this skill.** The plugin already has `scripts/pipelines/enrich_pdfs.py` for populating Zotero from publisher sources (ScienceDirect, Wiley TDM, browser fallbacks). Broadening the fulltext source pool is that script's job, not the auditor's.

## Always-escalate triggers

Skip Stage B and go straight to Stage C — the abstract cannot conclusively support these claim types regardless of how it reads:

- **Quoted passages.** Abstracts paraphrase; verbatim text must be checked in the body.
- **Specific statistics** cited from the paper (β, p, effect size, R², sample size). Abstracts rarely contain the exact numbers.
- **Method-detail claims** ("they used fixed effects", "2×2 ANOVA"). Methods sections, not abstracts.
- **Subgroup / moderator / mediator findings.** Almost always in the results section, not the abstract.

## Return format (for callers that dispatch this skill to a subagent)

A subagent loading this skill should return one block per citation:

```
@citekey
Classification: VERIFIED | MINOR | MAJOR | UNVERIFIABLE
Stage that resolved it: abstract | fulltext | unverifiable
Evidence: <one line — direct quote or paraphrase of the relevant source content>
Recommendation: <one line — what the author should do, if anything>
```

This is the contract between this skill and its callers. Callers reshape these blocks into their own report formats (`fact-check`'s console table or report file, `critic-loop`'s `ISSUES:` numbered list).

## Red flags — stop and reclassify

- About to mark a specific-statistic or quoted-passage claim VERIFIED based on the abstract alone — escalate to Stage C, or to UNVERIFIABLE.
- About to verify against training memory instead of an MCP fetch. Grounding is non-negotiable — re-fetch via MCP or read the Zotero child note. (`grounded-citations` enforces the same rule at insertion time; this skill enforces it at audit time.)
- Softening a direction-reversal flag to MINOR because the contradiction is "interpretive". Direction reversals are MAJOR.
- Marking UNVERIFIABLE as MAJOR by default. The user's PDF isn't being accused of being wrong; it isn't being read at all. The recommendation is to attach the PDF or change the citation — not to flag the manuscript.
- About to attempt OpenAlex / Semantic Scholar / publisher MCP fulltext as a fallback inside this skill. That's `enrich_pdfs.py`'s responsibility. UNVERIFIABLE is the correct outcome here.
