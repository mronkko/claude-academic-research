---
name: academic-style
description: Use when drafting or editing academic prose — topic sentences, paragraph structure, APA-style citations, active voice where it strengthens clarity, tense and hedging conventions, term definitions. Fires eagerly on any .qmd / .Rmd / .md / .tex file in an academic-research project. Enforces house-style invariants so prose is publishable-adjacent on first draft, not only after the critic loop catches them. Do NOT use for revision workflow — use `manuscript-revision` + `/critic-loop`. Do NOT use for factual or citation accuracy — use `grounded-citations` / `empirical-integrity` / `fact-check`.
---

# Academic style

## Core rule

Academic prose follows a small set of conventions that Claude applies
**during drafting**, not only at revision time. These are the rules the
`critic-loop` argument critic checks later — but writing them in
correctly the first time reduces how much the critic loop has to
fix.

This skill governs *style and conventions only*. It does not govern:

- Citation sourcing (Zotero-backed, consulted, claim-supporting) →
  `grounded-citations` rule-book.
- Quantitative claims (numbers from pipeline files) →
  `empirical-integrity` rule-book.
- When and how to revise a draft → `manuscript-revision` + `/critic-loop`.

## Citations (APA-style)

Formatting only; sourcing is governed by `grounded-citations` — follow
both. Format conventions:

- Inline: `[@key]` produces "(Smith, 2019)".
- Parenthetical with multiple: `[@key1; @key2]` produces
  "(Jones, 2020; Smith, 2019)".
- Narrative: `@key [-@key]` produces "Smith (2019)" / "Smith's (2019)".
- Three or more authors: render as `et al.` automatically via the
  CSL; do not hand-type "et al.".
- Cite the most specific pinpointable source. When a claim could come
  from a review or from the primary paper, cite the primary.

## Voice and tense

- **Active voice** where it strengthens clarity: *"We coded 1,243
  papers"* over *"1,243 papers were coded"*. Passive is acceptable
  when the actor is unknown or unimportant.
- **Methods** in past tense (this study's actions): *"We ran a pilot
  search against Scopus"*.
- **Theory and established findings** in present (ongoing truth):
  *"Self-efficacy predicts persistence"*.
- **Discussion** is present when interpreting (*"Our findings
  suggest"*) and past when describing what this study did (*"We
  found"*).

## Hedging calibration

Strong claims require strong evidence. Match hedging to design:

- **Cross-sectional** data warrants "associated with", "is correlated
  with", "differs across" — never "predicts", "causes", or "leads to".
- **Observational longitudinal** data without identification strategy
  warrants "is longitudinally associated with" or "precedes" — still
  not "causes".
- **RCT / natural experiment / well-identified quasi-experiment**
  warrants causal language.
- Don't hedge findings the field considers well-established (this
  signals false novelty). Don't over-claim findings that are novel.

(The `critic-loop` method critic flags violations at revision time —
writing to this standard up-front means the critic has less to catch.)

## Structure

- **Empirical papers**: IMRAD (Introduction, Methods, Results,
  Discussion). Each section scope-checked against Introduction's
  stated research question.
- **Review papers**: **synthesis over enumeration**. The text must
  analyse *across* cited studies, not march through them one at a
  time. Long stretches of "Smith (2019) found X. Jones (2020) found
  Y. Kim (2021) found Z." are a red flag — replace with thematic
  prose that names patterns, tensions, or cumulative findings and
  cites multiple papers per claim.
- **Topic sentences** open paragraphs. **Signposting** at section
  transitions (*"Having established X, we now turn to Y"*).

## Terms and acronyms

- **Define terms** on first substantive use, not first mention.
- **Expand acronyms** on first occurrence: *"better BibTeX (BBT)"*,
  then `BBT` thereafter.
- **Consistent terminology** throughout. Don't switch between "growth
  aspirations", "growth intentions", and "growth motivation" for the
  same construct without explaining the distinction.

## Red flags

- You are writing "Smith (2019) found X. Jones (2020) found Y." chains
  in a review paper — replace with thematic synthesis citing multiple
  papers per claim.
- You are hedging a finding the field considers well-established —
  drop the hedge.
- You are over-claiming from cross-sectional data ("predicts",
  "causes") — switch to associational language.
- You introduced an acronym without expanding it.
- Terminology drift between sections — same construct, different
  names.
- You are using passive voice where the actor matters and is known.
- You are adding an `et al.` by hand instead of relying on CSL
  rendering from the BBT key.
