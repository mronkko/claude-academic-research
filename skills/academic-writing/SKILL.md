---
name: academic-writing
description: Use when drafting, revising, editing, polishing, or reviewing academic prose — manuscripts, papers, theses, chapters, dissertations, or publications. Enforces that academic text is revised against multiple parallel critic perspectives via the critic-loop skill, not polished in a single pass.
---

# Academic writing

## Core rule

Academic prose is revised against multiple parallel critic perspectives,
not polished in a single pass. After drafting (or after any substantial
revision), run the **critic loop**: tests must pass → render → parallel
critic subagents → adjudicate → revise → repeat until no critic asks for
a MAJOR revision. The loop has explicit termination rules; do not exit
early and do not paper over unresolved MAJOR issues.

The `critic-loop` skill executes this procedure. This skill describes
*why* and *what*; the `critic-loop` skill describes *how*.

## Why a loop, not a pass

A single critic produces a shallow pass. Four differently-framed critics
catch non-overlapping classes of problem. The four defaults are chosen
to be general enough to apply to both literature reviews and empirical
manuscripts, while collectively covering the four independent axes on
which an academic paper can fail:

- **evidence** — are the paper's factual claims honestly supported by
  the sources and data it invokes? Catches fabricated findings,
  direction reversals, misattributed citations, and prose numbers that
  don't match the pipeline output.
- **method** — is the procedure defensible and transparently disclosed?
  Catches causal overreach, thin limitations, missing validity threats,
  and under-disclosed tools / prompts / models. Reviewer #2 energy,
  applied to method sections of any paper type.
- **argument** — does the prose make a coherent case from research
  question to contribution? Catches scope drift, structural incoherence,
  one-paper-at-a-time narration in review papers, undefined terms, and
  framing that doesn't match the target venue.
- **expert** — does the manuscript hold up against what a senior
  reviewer in the field already knows? Catches missing seminal works,
  dated framings, contradictions with well-established findings, and
  claimed "gaps" that aren't actually gaps. Uses the critic's own domain
  training — no external lookups.

Running them in parallel keeps latency ≈ max-of-four rather than
sum-of-four, and running them against a **rendered** build — not the
authoring source — means they evaluate what the reader will see
(resolved tables, inline expressions, citations), not what the author
typed.

## The critic contract

Each critic is a Claude subagent with a focused perspective prompt.
Every critic must return:

```
VERDICT: BLOCK | SHIP-WITH-REVISIONS | SHIP

ISSUES:
1. [MAJOR|MINOR|NIT] <section or short quoted passage>
   Issue: <what is wrong, in one or two sentences>
   Suggested revision: <concrete replacement prose or instruction>

2. [MAJOR] ...
```

**Severity semantics (strict):**

- **MAJOR** — factual error, direction reversal, fabricated citation,
  misrepresentation of a cited source, missing critical content,
  specific missing seminal work, specific contradicted finding. Blocks
  publication.
- **MINOR** — weak argument, thin evidence, unclear phrasing, missing
  secondary citation, "consider also" suggestions from training. Should
  be fixed but does not block.
- **NIT** — style, word choice, optional rephrasing.

**Verdict semantics:**

- **BLOCK** — at least one MAJOR issue.
- **SHIP-WITH-REVISIONS** — no MAJOR issues; MINOR and/or NIT issues remain.
- **SHIP** — no issues. Rare; honest critics should rarely emit this.

A verdict of `SHIP-WITH-REVISIONS` that somehow has `[MAJOR]` items in
the list is a contract violation — treat as `BLOCK` and tune the
critic's prompt.

## Default perspectives

Four defaults; any can be replaced or supplemented per-invocation.

1. **evidence** — verify that every factual claim in the manuscript is
   honestly supported. For citations, fetch the cited paper (full text
   or abstract) via the reference-manager MCP and check that the
   attribution matches the source. For quantitative claims, check that
   numbers in prose match the authoritative results file produced by
   the pipeline (per the `empirical-integrity` skill). Treats
   fabricated findings, direction reversals, wrong-paper-for-key,
   fabricated quotes, and prose numbers absent from the results file as
   MAJOR. This lens subsumes source-checking and substance.
2. **method** — methodology, validity threats, limitations,
   causal-language calibration (cross-sectional ⇒ *associated with* not
   *predicts*; mediation claims require proper tests; moderator claims
   require interaction terms). Applies to both empirical and review
   papers (for reviews: screening procedure, coder reliability, search
   reproducibility, LLM disclosure). Does not repeat evidence's
   source-checking work.
3. **argument** — academic writing quality and scope coherence. Topic
   sentences, paragraph unity, signposting, consistent terminology,
   synthesis over enumeration in review papers (the "Smith (2019) found
   X. Jones (2020) found Y." failure mode is MAJOR). Also: does the
   manuscript address its stated research question consistently from
   Introduction through Discussion? Does framing match the target
   venue's conventions? This lens subsumes style and fit.
4. **expert** — evaluate the manuscript the way a senior reviewer in
   the target field would: using their own training, not by re-reading
   the cited papers. Flags missing seminal works, dated theoretical
   framings, contradictions with well-known findings in the field, and
   claimed "gaps" that aren't actually gaps. MAJOR requires a
   *specific* missing work, *specific* contradicted finding, or
   *specific* dated framing — vague "this feels off" concerns must be
   marked MINOR or NIT. Expert critic must not hallucinate citations:
   if it names a work, it should be genuinely confident the work exists
   and is relevant.

All four can be replaced or supplemented via the `critic-loop` skill's
`--critics` argument.

## Termination rules

The loop exits when **any** of these holds:

1. All critics returned `SHIP` **or** all returned `SHIP-WITH-REVISIONS`
   with no `[MAJOR]` items outstanding across the whole set.
2. The iteration cap (`MAX_ITER`, default **4**) is reached. If
   unresolved `[MAJOR]` items remain, the final report lists them
   explicitly — no silent burial.
3. A critic loop-back: the same MAJOR item is flagged, applied, and
   flagged again by the same critic on the next iteration. This signals
   genuine disagreement that requires human adjudication — stop and
   surface.

The loop does **not** exit because "critics still have suggestions."
Critics always will. It exits when no critic asks for a MAJOR revision.

## What critics do not do

- **Critics don't rewrite.** They flag. The main agent adjudicates and
  applies.
- **Critics don't rerun tests.** The main agent gates each iteration on
  tests passing; critics only ever see green builds.
- **Critics don't adjudicate between each other.** When critics disagree
  (e.g., expert wants a rival theory added, argument wants scope
  tightened), the main agent writes a brief adjudication note, decides,
  and logs the reasoning in `decisions.md`.
- **Critics don't see the authoring source.** They see the rendered
  markdown so they evaluate what the reader sees.
- **Critics don't get re-invoked on untouched sections** in later
  iterations unless their first-iteration feedback was not fully
  applied. This is optional optimization; full re-review is the safer
  default.

## Adjudication and logging

Every numbered item from every critic must receive one of three
dispositions:

- **applied** — revision made this iteration.
- **deferred** — logged for a later pass with reason (e.g. "needs a
  primary source we don't have yet"; "requires author judgment").
- **rejected** — explicit disagreement with the critic, with written
  reason.

Silently dropping a critic's item is a red flag. If the same item is
deferred across three iterations, it is effectively rejected — force
the decision and log it.

The expert critic deserves extra scrutiny at adjudication: its claims
are not backed by a source lookup, so the main agent should verify any
named seminal work via an MCP query (OpenAlex, Semantic Scholar, or
Zotero) before applying. A flagged "missing" work that does not in fact
exist, or does not in fact say what the expert critic claimed, is a
`rejected` item — and is signal that the expert critic's prompt needs
tuning.

## Per-iteration artifacts

```
/tmp/critic-loop/
  iter-1/
    rendered.md              # snapshot of the rendered manuscript
    critic-{name}.md         # one file per critic, raw response
    decisions.md             # per-item disposition + reason
  iter-2/
    ...
  final-report.md            # verdict timeline + unresolved items + counts
```

These files make the loop auditable. If a future reviewer asks "did
anyone check for X", the `iter-N/critic-*.md` files answer it.

## Integration with empirical-integrity

Critics cannot be used to bypass the rule that every number in prose
must come from an inline expression reading `analysis/results/`. If a
critic flags a passage as weak and the suggested fix would hand-type a
statistic, the main agent must instead add the statistic to the
pipeline or reject the suggestion. The evidence critic is explicitly
instructed to apply the `empirical-integrity` rule when evaluating
numbers.

## Red flags

- You are about to exit the loop because "critics will always find
  something."
- A critic returned `SHIP-WITH-REVISIONS` but the list contains
  `[MAJOR]` items.
- You are letting a critic rewrite a passage directly instead of
  extracting its suggestion and applying it yourself.
- You skipped the test-gate step because "tests passed last iteration."
- You are about to silently drop a critic's numbered item without
  recording a disposition.
- You are applying a critic's revision that would hand-type a
  statistic, in violation of the `empirical-integrity` rule.
- You are applying an expert-critic MAJOR flag naming a missing seminal
  work without first verifying the work exists and says what the critic
  claims.
- You reached the iteration cap with unresolved MAJORs and are about to
  call the loop "done" without listing them explicitly in the final
  report.
