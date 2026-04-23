---
name: critic-loop
description: Use when the user asks to revise, polish, or critique an academic manuscript, chapter, thesis, or paper through parallel critic perspectives. Trigger phrases: "run critic-loop", "/critic-loop", "revise this manuscript", "run critics on this draft", "run a critical review", "review this chapter". Main agent edits → tests pass → render → four critics review in parallel (evidence, method, argument, expert) → main agent adjudicates and applies → loop until no critic asks for a MAJOR revision. Invoke as `/critic-loop <document-path>`.
---

# critic-loop

**Invocation:** `/critic-loop <document-path> [--critics evidence,method,argument,expert] [--max-iter 4] [--no-test] [--render-cmd "quarto render {doc} --to gfm"]`

## Pre-flight (ALWAYS run first)

Before any step below, verify the plugin has been configured:

```bash
python -c "from pathlib import Path; print('configured' if (Path.home()/'.config'/'academic-research'/'config.toml').is_file() else 'NOT CONFIGURED')"
```

If the result is `NOT CONFIGURED`, stop immediately and tell the user:

> The academic-research plugin has not been set up on this machine
> yet. Run `/setup` first — the evidence critic depends on MCP
> citation lookups (Zotero, OpenAlex, Semantic Scholar), which
> require MCP servers that `/setup` registers.

Do not launch critics or proceed with the loop. `/setup` is the
required first step.

If the result is `configured`, proceed.

---

Execute the editing loop documented in the `manuscript-revision` skill.
This skill is the *procedure*; that skill is the *doctrine*. Read it
first if you have not recently.

## Argument parsing

Required:

- `<document-path>` — path to the authoring source (e.g.
  `manuscript/motivation_slr.qmd`). Relative paths are resolved against
  the project root.

Optional:

- `--critics <csv>` — comma-separated list of critic perspectives.
  Defaults to `evidence,method,argument,expert`. Unknown names are
  treated as custom perspectives; the main agent must write a focused
  prompt for each (describe the lens in 1–2 sentences when launching
  the Agent).
- `--max-iter <N>` — iteration cap. Default **4**.
- `--no-test` — skip the test-suite gate. Only use when explicitly
  asked (e.g., when the repo has no test suite yet). Red flag otherwise.
- `--render-cmd <cmd>` — shell template for the render step. `{doc}`
  is substituted with the document path. Default:
  `quarto render {doc} --to gfm` which produces a `.md` sibling of the
  `.qmd`. For R Markdown, pass
  `Rscript -e "rmarkdown::render('{doc}', output_format='md_document')"`.
- `--rendered-path <path>` — override the rendered output path if the
  render command writes somewhere non-obvious.

## Rendering: what and why

Rendering is a first-class step of the loop, not a side-effect of
"let's see the output". Three reasons the critics read the rendered
file, not the source:

1. **Render is the final integrity test.** A missing `s['key']` lookup
   or an unresolved `@citekey` crashes the renderer louder than any
   grep. `test_empirical_integrity.py` catches the static version of
   this; render catches the runtime version (stats module throws on a
   downstream computation, pipeline output file is stale). Render
   failure is a specific-class-of-integrity signal — treat it as a
   test failure and route the fix through Step 1, not around it.
2. **Critics should see what the reader sees.** Resolved numbers and
   rendered citations, not `{python} s['screen.n_included']`
   placeholders. The evidence critic's "does this claim match the
   source?" is a different question when the claim is still an
   expression.
3. **The rendered snapshot is the iteration diff anchor.** Iteration
   N+1's critics compare against iteration N's `rendered.md`; the
   source file is too volatile.

Document-format defaults assume Quarto → gfm, with the snapshot landing
at `.claude/critic-loop/iter-{N}/rendered.md`. For other formats:

- **Plain `.md`** — no render needed; snapshot is a file copy.
- **`.ipynb`** — `jupyter nbconvert --to markdown {doc}` (add
  `--execute` if cells are runnable).
- **`.tex`** — `latexmk -pdf {doc}` yields a PDF, not markdown. Either
  add a `pandoc` post-step back to markdown, or set
  `--rendered-path` to the PDF and tell the critics to accept PDF.
- **`.Rmd` → `html_document`** — default advice is `md_document`
  because critics prefer markdown; `--to html` works, but if you
  override the render command that way you must also pass
  `--rendered-path` so the snapshot step still finds the output.

Whenever you override `--render-cmd` to produce something other than a
sibling `.md`, pass `--rendered-path` to match. They vary together.

## Procedure

Create the iteration working directory up front (project-local; portable):

```bash
python -c "from pathlib import Path; Path('.claude/critic-loop').mkdir(parents=True, exist_ok=True)"
```

Then:

```
iter = 1
while iter <= MAX_ITER:

  ── Step 1: test gate ──────────────────────────────────────────────
  Unless --no-test: run every `test_*.py` script the project ships in
  `scripts/`, in the order the project's CLAUDE.md names (typical order:
  `test_citations.py`, `test_empirical_integrity.py`,
  `test_systematic_review.py`). Common invocations: `pytest`,
  `npm test`, or the bare `python3 scripts/test_<name>.py` sequence.
  If any file fails:
    - diagnose the failure
    - fix the underlying cause (do not suppress or skip tests)
    - re-run
    - only proceed to Step 2 when all tests pass.
  Each test file maps to a skill — a failure in `test_citations.py` is
  a `grounded-citations` / `fact-check` regression; a failure in
  `test_empirical_integrity.py` is an `empirical-integrity` regression;
  a failure in `test_systematic_review.py` is a `systematic-review`
  pipeline regression. Read the relevant skill if the fix is not
  obvious.
  If tests cannot be made to pass without the user's input, stop the loop and
  surface the failure to the user. Do NOT call critics on a broken build.

  ── Step 2: render ──────────────────────────────────────────────────
  Run the render command. Verify the rendered output file exists and is
  non-empty. If render fails, treat as a test failure (go to Step 1 fix loop).
  Snapshot: copy the rendered .md to .claude/critic-loop/iter-{N}/rendered.md.

  ── Step 3: launch critics IN PARALLEL ─────────────────────────────
  Single message, multiple Agent tool calls — one per critic. Each Agent call
  uses subagent_type="general-purpose" (model="sonnet" is a reasonable default)
  and receives the generic prompt preamble below plus the perspective prompt.

  Save each Agent's returned text to .claude/critic-loop/iter-{N}/critic-<name>.md.

  ── Step 4: adjudicate ─────────────────────────────────────────────
  For every numbered item across all critics, pick one disposition:
    applied  — revision will be made this iteration
    deferred — log reason (needs user input / needs new data / out of scope)
    rejected — log disagreement with written reason
  On inter-critic disagreement (e.g. expert wants a theory added, argument
  wants scope tightened): write a brief adjudication note and decide.

  Extra scrutiny for expert-critic MAJOR items: the expert critic does not
  back its claims with a source lookup. Before applying a "missing seminal
  work" flag, verify via MCP (OpenAlex / Semantic Scholar / Zotero) that the
  named work exists and says what the critic claimed. If not, reject the item
  and note that the expert critic's prompt may need tuning.

  Write everything to .claude/critic-loop/iter-{N}/decisions.md (format below).

  ── Step 5: apply edits ────────────────────────────────────────────
  Apply every "applied" item to the authoring source (not the rendered
  markdown). Use Edit/Write on the .qmd / .Rmd / .md source file.

  ── Step 6: termination check ──────────────────────────────────────
  Exit the loop if ANY of these holds:
    (a) All critic verdicts are SHIP, OR all are SHIP-WITH-REVISIONS with zero
        [MAJOR] items remaining across the whole set (i.e. every MAJOR item
        was applied this iteration);
    (b) iter == MAX_ITER;
    (c) Loop-back detected: the same MAJOR item was flagged in iter N-1 by
        the same critic, marked "applied", and flagged again by that critic
        in iter N. Surface this as a human-adjudication request.

  iter += 1
```

## Generic prompt preamble (all critics)

Append the perspective-specific prompt below this preamble:

```
You are an <perspective> critic reviewing the manuscript at
.claude/critic-loop/iter-{N}/rendered.md.

Research context: <one-paragraph summary of the project's research question,
scope, and data — pulled from the project's CLAUDE.md>.

Scope boundaries — STRICT:
  Your domain is <perspective>. The other critics (evidence, method, argument,
  expert — whichever are active in this run) cover their own domains in
  parallel. Do NOT duplicate their work. If you see an issue that clearly
  belongs to another critic's scope, skip it — they will catch it.
  The per-perspective prompt below defines your exact scope.

Anti-sycophancy — STRICT:
  Each iteration you see a revised manuscript. Evaluate the current iteration
  on its own merits. Do NOT soften your assessment because the author has
  "clearly been working hard" or because "progress has been made since the
  last iteration". If a MAJOR issue remains after revision, flag it MAJOR
  again. If a new MAJOR issue has been introduced by the revision, flag it.
  The loop's purpose is to exit when no MAJOR issue remains, not to exit
  because you are tired of flagging.

Your role: FLAG issues, do NOT rewrite. The author will adjudicate and apply.

<perspective-specific prompt — see Perspective prompts section>

Output format — strict, no prose outside this structure:

VERDICT: BLOCK | SHIP-WITH-REVISIONS | SHIP

ISSUES:
1. [MAJOR|MINOR|NIT] <section title or short quoted passage (~20 words)>
   Issue: <one or two sentences>
   Suggested revision: <concrete replacement prose or specific instruction>

2. ...

Severity rules:
  MAJOR = factual error, direction reversal, fabricated citation,
          misrepresentation, missing critical content, specific missing
          seminal work, specific contradicted finding. Blocks publication.
  MINOR = weak argument, thin evidence, unclear phrasing, "consider also".
  NIT   = style, word choice, optional rephrasing.

Verdict rules:
  BLOCK                = at least one MAJOR issue.
  SHIP-WITH-REVISIONS  = no MAJOR, but MINOR/NIT remain.
  SHIP                 = no issues. Rare.

Return your report as the Agent result — the main agent will save it.
```

## Perspective prompts

These are appended to the generic preamble. Keep them focused and
non-overlapping.

### evidence  *(default)*

```
Your scope: verify that every factual claim in the manuscript is honestly
supported by its source — either a cited paper or an authoritative pipeline-
output file. You do NOT evaluate method rigour, writing quality, or missing-
seminal-work judgments — those belong to method / argument / expert.

For in-text citations, use the Zotero MCP tools (or equivalent reference-
manager MCP) in this order of preference:
  1. mcp__zotero__zotero_get_item_fulltext — when a full-text PDF is attached.
  2. mcp__zotero__zotero_get_item_metadata — for the abstract (sufficient for
     many claims) and for basic bibliographic details.
  3. mcp__zotero__zotero_search_by_citation_key — to resolve a BBT citation
     key to a Zotero item key.

For each @citekey in the rendered manuscript, check:
  - Does the paper actually exist under that key?
  - Does the attributed finding match the paper's abstract/full text?
  - Is the direction of the claim correct (especially for regression
    coefficients, moderators, mediators)?
  - Is the quoted text actually in the paper? Paraphrases are OK; fabricated
    quotes are not.

For quantitative claims in prose and tables, check that numbers match the
authoritative results file (usually analysis/results/*.csv or *.json, per the
empirical-integrity skill). If a project-specific coded corpus exists (e.g.
analysis/results/coded_papers.csv for SLRs), spot-check prose synthesis
claims against coded entries.

Treat as MAJOR: missing paper, wrong paper for the key, direction reversal,
fabricated finding, fabricated quote, claim not supported by the source,
prose number absent from or inconsistent with the authoritative results file.
Treat as MINOR: overreaching paraphrase, oversimplified finding, missing
caveat that the source considers important.

Spot-check is acceptable when citation count exceeds ~30; prioritize high-
stakes claims (MAJOR findings, directional claims, quoted passages). Report
the sample size in the first ISSUES entry if spot-checking.
```

### method  *(default)*

```
Your scope: methodological scrutiny — reviewer #2 energy. This lens applies
to both empirical and review papers. You do NOT verify citations against
sources (evidence critic's job), you do NOT evaluate prose quality (argument
critic's job), and you do NOT flag missing seminal works from your training
(expert critic's job).

Treat as MAJOR:
  - causal language that overreaches the design (cross-sectional data should
    say "associated with", not "predicts" or "causes");
  - mediation claims without proper tests; moderator claims without
    interactions;
  - limitations section that omits the obvious threats (single coder, LLM
    bias, language restriction, sample selection, overreliance on one data
    source);
  - missing disclosure of tools / models / prompt versions used;
  - for reviews: missing disclosure of search strategy, screening reliability,
    coder agreement, or LLM prompts used in the pipeline.

Treat as MINOR: imprecise method descriptions, missing effect sizes,
over-broad generalization, under-specified sample characteristics.
```

### argument  *(default)*

```
Your scope: the manuscript as academic prose AND as a coherent case for its
stated research question. You do NOT verify individual citations (evidence
critic), you do NOT evaluate methodological rigour (method critic), and you
do NOT flag missing seminal works (expert critic).

Check:
  - Good academic writing conventions: topic sentences, paragraph unity,
    clear logical flow between paragraphs, signposting between sections,
    appropriate hedging, active voice where it strengthens clarity.
  - Terms defined on first substantive use; jargon introduced with a brief
    gloss; acronyms expanded on first occurrence.
  - Consistent terminology (e.g. don't switch between "growth intentions",
    "growth aspirations", and "growth motivation" for the same construct
    without explaining the distinction).
  - SYNTHESIS over enumeration: for review papers, the text should analyze
    *across* cited studies, not merely march through them one at a time.
    Long stretches of "Smith (2019) found X. Jones (2020) found Y. Kim (2021)
    found Z." are a MAJOR flag — replace with thematic synthesis that names
    patterns, tensions, or cumulative findings and cites multiple papers per
    claim.
  - Scope coherence: does the manuscript address its stated research question
    consistently from Introduction through Discussion? Does Method scope
    match Introduction scope? Does Findings deliver on Introduction's
    promises? Does Discussion's contribution claim match what Findings
    demonstrated?
  - Venue fit: framing and structure appropriate to the target journal's
    conventions.

Treat as MAJOR: single-article-description prose where synthesis is required;
Introduction promising a question Findings does not deliver; core term used
without definition; scope mismatch between sections; large-scale logical
disorder.
Treat as MINOR: weak topic sentences, paragraph length problems, inconsistent
hedging, structural imbalance.
Treat as NIT: word-choice issues, minor repetition, awkward phrasing.
```

### expert  *(default)*

```
Your scope: evaluate the manuscript the way a senior reviewer in the target
field would — using your own domain training, NOT by re-reading the cited
papers. You do NOT verify citations the author has made (evidence critic), you
do NOT scrutinise method sections line-by-line (method critic), and you do
NOT critique prose quality (argument critic). Your job is the expert-reader
gut check: what is missing from this manuscript that a seasoned reviewer in
the field would expect to see?

Check for:
  - Missing seminal works or foundational theories that any competent
    reviewer would expect to see. Name specific authors and works.
  - Dated theoretical framings — does the review reflect the current state
    of the field or a textbook version from ten years ago? Is there a major
    recent development the manuscript misses?
  - Contradictions with well-known findings in the field. If the manuscript
    claims X but a well-established meta-analysis or stream of work says
    otherwise, flag it and name the source.
  - Plausibility of claimed "research gap" — does the gap actually exist, or
    has it been addressed elsewhere in the literature?
  - Interpretive fit — are constructs defined/grouped the way experts in the
    field actually use them? Are well-known distinctions respected?
  - Omissions of prominent scholars whose work on this topic is central.

Severity discipline is critical for this perspective:
  - MAJOR ONLY when you can name a SPECIFIC missing seminal work, a SPECIFIC
    contradicted finding, or a SPECIFIC dated framing. "I feel something is
    missing" is NOT MAJOR.
  - MINOR for "consider also" suggestions from your training — useful
    additions but not blocking. If the flag is speculative, mark it MINOR
    and say so.
  - NIT for expert-stylistic preferences.

Do NOT hallucinate citations: if you name a work, you should be genuinely
confident it exists and is relevant. The main agent will verify expert-critic
MAJOR items against OpenAlex / Semantic Scholar / Zotero before applying, and
a hallucinated citation will be rejected.
```

### Custom perspectives

Add via `--critics`. Examples of reasonable additions:

- **practitioner** — "would a practitioner reader find this actionable?"
- **methods-for-non-methodologists** — "does this explain methods in a way a
  reader without your statistical background can follow?"
- **ethics** — "are ethical considerations adequately disclosed and discussed?"

Each custom perspective needs a focused prompt following the same format as
the defaults above, including a scope-boundaries clause that names the
active critics it must not duplicate.

## decisions.md format

```markdown
# Iteration {N} decisions

## Critic: evidence — VERDICT: BLOCK
- Item 1 [MAJOR]: <short restatement> — **applied**. <reason/how>
- Item 2 [MINOR]: <short restatement> — **deferred**. <reason>
- Item 3 [NIT]:   <short restatement> — **rejected**. <reason>

## Critic: method — VERDICT: ...
...

## Adjudications
- Evidence item 1 conflicts with expert item 3 — <decision + reason>
```

## Final report

After the loop exits, write `.claude/critic-loop/final-report.md`:

```markdown
# Critic-loop final report

**Document:** <path>
**Iterations run:** <N> / <MAX_ITER>
**Exit reason:** <(a) all critics satisfied / (b) iteration cap hit / (c) loop-back>

## Verdict timeline

| Iter | evidence | method | argument | expert |
|---|---|---|---|---|
| 1 | BLOCK | BLOCK | BLOCK | BLOCK |
| 2 | BLOCK | SHIP-WITH-REVISIONS | SHIP-WITH-REVISIONS | SHIP-WITH-REVISIONS |
| 3 | SHIP-WITH-REVISIONS | SHIP-WITH-REVISIONS | SHIP-WITH-REVISIONS | SHIP-WITH-REVISIONS |

## Item counts (cumulative across iterations)

| critic   | MAJOR | MINOR | NIT | applied | deferred | rejected |
|---|---:|---:|---:|---:|---:|---:|
| evidence | 4 | 7 | 1 | 10 | 2 | 0 |
| method   | 3 | 6 | 2 | 9  | 2 | 0 |
| argument | 2 | 9 | 6 | 14 | 3 | 0 |
| expert   | 2 | 7 | 1 | 6  | 3 | 1 |

## Unresolved MAJOR items (if any)

- <critic> iter <N> item <M>: <restate>. Reason unresolved: <…>

## Deferred items carried forward

- <critic> iter <N> item <M>: <restate>. Reason: <…>
```

If there are zero unresolved MAJOR items and no loop-back, the final
report's verdict line should read `**Final status: LOOP COMPLETE — no
unresolved MAJORs.**`. Otherwise `**Final status: LOOP COMPLETE WITH
UNRESOLVED ITEMS — see below.**`.

## Reporting to the user

After writing the final report, send the user a concise summary message
(~100 words): exit reason, iterations used, count of applied/deferred/
rejected items, and any unresolved MAJORs. Do not paste the full critic
reports into the chat — point to the files under `.claude/critic-loop/`.

## Red flags

- Skipping the test gate "just this once" because tests passed last
  iteration.
- Dropping a critic's numbered item without recording a disposition.
- Applying a critic's suggestion that would hand-type a statistic
  (violates the `empirical-integrity` skill — route it through the
  pipeline instead).
- Applying an expert-critic MAJOR flag that names a missing seminal
  work without first verifying the work exists and says what the critic
  claims.
- Treating a critic that returned BLOCK as SHIP-WITH-REVISIONS to force exit.
- Reaching MAX_ITER with unresolved MAJORs and calling the loop done.
- Letting a critic rewrite prose directly (the contract is flag-only).
- A critic that flagged MAJORs in iter N-1 now returns SHIP in iter N
  despite minimal visible revision — anti-sycophancy violation; re-prompt
  or discard that iteration's critic output.
- You are about to read `~/.config/academic-research/config.toml` via
  `cat`, `head`, `tail`, `grep`, `less`, `more`, `awk`, `sed`, a
  Python script, or any other command. **NEVER read that file.** It
  holds API keys. The critic loop has no legitimate need for them.
