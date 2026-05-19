---
name: verifying-citations
description: Use when verifying that a citation in a manuscript is honestly supported by its source — a sub-skill called by `fact-check` and by the evidence critic inside `critic-loop`. Loaded via `REQUIRED SUB-SKILL: verifying-citations` from a calling skill; not invoked standalone by end users. Defines the classification scheme, the staged abstract-then-fulltext rule, and the always-escalate triggers so both callers share one source of truth.
---

# verifying-citations

## Overview

A citation is verified when an externally consulted source (Zotero PDF fulltext or abstract — never training memory) directly supports the claim the manuscript attributes to it. Verification is staged: read the abstract first; escalate to full text only when the abstract is silent, ambiguous, or insufficient for the kind of claim being made.

This skill is the single source of truth for citation-verification doctrine. `fact-check` (one-shot audits) and the evidence critic in `critic-loop` (iterative revision) both load it so that when the rules change, two skills don't drift.

**Insertion vs verification.** The complementary skill `grounded-citations` governs *inserting* a citation during drafting — same externalised-consultation rule, but it escalates on **recency / context staling** (a faded context-window memory needs re-fetching). This skill governs *auditing* an existing citation, and it escalates on **claim type** — quoted passages, specific β coefficients, and method details always require fulltext regardless of what the abstract says. Same demand for grounding; different decision axis.

**Unit of dispatch: one subagent per unique citation, not per mention.** A paper cited five times in a manuscript becomes one subagent (with five mentions in its prompt), not five subagents. This (a) fetches the source once and (b) lets the subagent catch *cross-mention inconsistency* — e.g. the manuscript citing the same paper for X in Methods and ¬X in Discussion — which per-mention dispatch can't see. See *Multiple mentions of one source* below.

## Classification scheme

| Classification | When to assign |
|---|---|
| **VERIFIED** | The source directly supports the claim — sign, direction, finding, and magnitude all match. |
| **MINOR** | The claim overreaches the source: oversimplified finding, missing caveat the source considers important, paraphrase that mildly overstates. |
| **MAJOR** | The paper doesn't exist under that key; wrong paper for the key; direction reversal; fabricated quote; specific finding not supported by the source. Blocks publication. |
| **UNVERIFIABLE** | Verification requires the full text and no PDF is attached to the Zotero item. The issue is access, not accuracy — recommend running `enrich_pdfs.py` to populate the library, or replacing the citation. Callers may map UNVERIFIABLE to MAJOR for their own report when the workflow can't ship an unresolved citation (e.g. inside `critic-loop`). |

## Staged verification rule

### Stage 0 — resolve the citation to a Zotero item

The caller may pass the citation in one of two forms:

- **`@bbtkey` form** (rendered manuscripts, where citations have been
  resolved already) → `mcp__zotero__zotero_search_by_citation_key` to
  resolve the BBT key to a Zotero item key.
- **Author-Year prose form** (pasted excerpts in fact-check console
  mode, or any other unrendered prose) → `mcp__zotero__zotero_search_items`
  with the surname(s) and year (and a second co-author surname if
  present in the prose to disambiguate). If multiple candidates match,
  pick the one whose title/venue is consistent with the surrounding
  context.

If nothing resolves — neither the BBT key nor any Author-Year search
finds a matching item — the citation is **MAJOR** (paper not in
library) for **every mention** of it, with the recommendation: "add
the paper to Zotero, or replace the citation with one that supports
the claim."

### Stage A — fetch the abstract

`mcp__zotero__zotero_get_item_metadata` on the resolved item key.

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

## Multiple mentions of one source

The caller dispatches one subagent per **unique citation** with all of
the manuscript's mentions of that citation in the prompt. The subagent
should:

1. Resolve once (Stage 0).
2. Fetch the abstract once (Stage A). Fetch the full text at most once
   (Stage C), only if any mention's claim type requires it.
3. Classify **each mention independently**, applying Stages A–C per
   mention's specific claim. Two mentions of the same paper may
   legitimately get different classifications (one VERIFIED for a
   headline finding the abstract supports, another MAJOR for a
   subgroup result the body contradicts).
4. **Cross-mention check.** Once each mention is classified, look
   across the set: does the manuscript use this paper consistently?
   Flag as a separate finding if (a) the paper is cited for X in one
   place and ¬X in another, (b) a directional claim and its inverse
   are both attributed to the same source, or (c) the same finding is
   attributed with materially different magnitude or scope across
   mentions. This catches a class of error per-mention checks miss.

## Return format (for callers that dispatch this skill to a subagent)

A subagent returns one block per mention plus, optionally, one
cross-mention block:

```
@citekey   (or the Author-Year surface form when no @key exists yet)

Mention 1 — location: "<section / quoted excerpt>"
Classification: VERIFIED | MINOR | MAJOR | UNVERIFIABLE
Stage: abstract | fulltext | unverifiable
Evidence: <one line — direct quote or paraphrase of the relevant source content>
Recommendation: <one line — what the author should do, if anything>

Mention 2 — location: "..."
Classification: ...
...

Cross-mention (only if applicable):
Finding: <one line — describe the internal inconsistency>
Severity: MAJOR | MINOR
Recommendation: <one line>
```

When a citation has only one mention, the cross-mention block is
omitted and there is exactly one mention block.

This is the contract between this skill and its callers. Callers
reshape these blocks into their own report formats — `fact-check`'s
console table or report file, `critic-loop`'s `ISSUES:` numbered list.

## Red flags — stop and reclassify

- About to mark a specific-statistic or quoted-passage claim VERIFIED based on the abstract alone — escalate to Stage C, or to UNVERIFIABLE.
- About to verify against training memory instead of an MCP fetch. Grounding is non-negotiable — re-fetch via MCP or read the Zotero child note. (`grounded-citations` enforces the same rule at insertion time; this skill enforces it at audit time.)
- Softening a direction-reversal flag to MINOR because the contradiction is "interpretive". Direction reversals are MAJOR.
- Marking UNVERIFIABLE as MAJOR by default. The user's PDF isn't being accused of being wrong; it isn't being read at all. The recommendation is to attach the PDF or change the citation — not to flag the manuscript.
- About to attempt OpenAlex / Semantic Scholar / publisher MCP fulltext as a fallback inside this skill. That's `enrich_pdfs.py`'s responsibility. UNVERIFIABLE is the correct outcome here.
- Subagent has multiple mentions of the same source but is treating them in isolation — the cross-mention check is the whole point of per-citation (not per-mention) dispatch. Even when every individual mention is VERIFIED, the paper may still be cited in mutually contradictory ways across the manuscript.
- Fetching the same source's fulltext twice within one subagent run. Fetch once, classify many. The exception is if the first fetch returned empty content (no PDF) — then UNVERIFIABLE for every mention; do not retry.
