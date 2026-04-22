---
name: manuscript-revision
description: Use when the user asks to revise, polish, improve, critique, or finalize an existing academic draft — manuscripts, papers, theses, chapters, dissertations, or publications. Trigger phrases "revise this draft", "polish this chapter", "finalize this manuscript", "improve this paper", "critique this section", "review this thesis". Enforces that revision goes through the parallel-critic loop — not a single polish pass — and hands execution to `/critic-loop`. Do NOT use for first-draft writing or blank-page work — this skill's doctrine applies once a draft exists and is ready for revision.
---

# Manuscript revision

## Core rule

Academic prose is revised against multiple parallel critic perspectives,
not polished in a single pass. After drafting (or after any substantial
revision), run the **critic loop** via `/critic-loop`: tests must pass
→ render → parallel critic subagents → adjudicate → revise → repeat
until no critic asks for a MAJOR revision. The loop has explicit
termination rules; do not exit early and do not paper over unresolved
MAJOR issues.

This skill is the *doctrine* — *why* revision works this way and *what*
the critics are for. The `critic-loop` skill is the *procedure* — how
to actually run it, with CLI flags, Agent prompts, and file schemas.
Read `critic-loop` for the executable details; everything below is the
justification for that procedure's shape.

## Why a loop, not a pass

A single critic produces a shallow pass. Four differently-framed critics
catch non-overlapping classes of problem — each perspective covers one
independent axis on which an academic paper can fail:

- **evidence** — are the paper's factual claims honestly supported by
  the sources and data it invokes? Catches fabricated findings,
  direction reversals, misattributed citations, and prose numbers that
  don't match the pipeline output.
- **method** — is the procedure defensible and transparently disclosed?
  Catches causal overreach, thin limitations, missing validity threats,
  and under-disclosed tools / prompts / models. Reviewer #2 energy.
- **argument** — does the prose make a coherent case from research
  question to contribution? Catches scope drift, structural incoherence,
  one-paper-at-a-time narration in review papers, undefined terms, and
  framing that doesn't match the target venue.
- **expert** — does the manuscript hold up against what a senior
  reviewer in the field already knows? Catches missing seminal works,
  dated framings, contradictions with well-established findings, and
  claimed "gaps" that aren't actually gaps.

Running them in parallel keeps latency ≈ max-of-four rather than
sum-of-four. Running them against a **rendered** build — not the
authoring source — means they evaluate what the reader will see
(resolved tables, inline expressions, citations), not what the author
typed.

## What critics do not do

- **Critics don't rewrite.** They flag; the main agent adjudicates and
  applies.
- **Critics don't rerun tests.** The main agent gates each iteration on
  tests passing; critics only ever see green builds.
- **Critics don't adjudicate between each other.** When they disagree,
  the main agent writes a brief adjudication note and decides.
- **Critics don't see the authoring source.** They see the rendered
  markdown so they evaluate what the reader sees.

## See also

- **`/critic-loop`** — the executable procedure: CLI arguments, the
  generic prompt preamble, the four default perspective prompts,
  termination conditions, the decisions.md / final-report.md schemas,
  and full red-flags list. Single source of truth for how the loop
  actually runs.
- **`grounded-citations`** — governs citation hygiene during both
  drafting and revision. When a critic flags a weak citation, the fix
  goes through `grounded-citations`' four-part rule (Zotero-backed
  BBT key, externalised consultation, claim-supporting source).
- **`empirical-integrity`** — governs how numbers enter prose. When a
  critic suggests sharpening a statistic, the fix must route through
  the pipeline (inline expression reading `analysis/results/`) — never
  hand-typed. Critics must not be used to bypass this rule.
