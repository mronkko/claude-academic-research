---
name: risk-of-bias
description: Use when appraising risk of bias or study quality in a biomedical or dental systematic review (QUIN for in vitro studies, QUADAS-2 for accuracy studies, routing to ROBINS-I or RoB 2). Trigger phrases "risk of bias", "QUIN", "QUADAS-2", "quality assessment", "RoB". Do NOT use for screening or coding — use `systematic-review`; for checking manuscript citations — use `fact-check`.
---

# risk-of-bias

> **Glossary:** unfamiliar with **PRISMA**, **BBT** or **stage tag**? See
> [skills/_glossary.md](../_glossary.md).

**Plugin tools.** Steps name the tools of the plugin's own
**academic-research** MCP server by bare name (`score_risk_of_bias`); in
Claude Code it appears as
`mcp__plugin_academic-research_academic-research__score_risk_of_bias`, on
other hosts with that host's prefix. If the tools are not available, run
the *terminal equivalent* given beside each with Bash instead, and tell
the user once how to enable the server (see **Terminal equivalent** in
[skills/_glossary.md](../_glossary.md)).

## Overview

`systematic-review` targets social-science reviews and lists medical
risk-of-bias instruments as out of scope. This skill is that missing
stage for a **biomedical or dental** review. It runs after full-text
coding, on the studies coded `include`, and produces one judged
record per study plus a deterministic summary for the manuscript
(PRISMA 2020 items 11 and 18).

**The judgements belong to the reviewers. The arithmetic belongs to the
script.** You prepare evidence and a draft; two humans decide; a script
computes every percentage and count that reaches the manuscript.

## Hard rules

1. **A model draft is never the assessment.** Two human reviewers rate
   each study independently, then resolve disagreements by discussion or
   a third reviewer. If the protocol names a model as an additional
   rater, report that and the agreement between model and humans
   (Cohen's kappa); the humans still decide.
2. **No inference.** If the paper does not say it, the item is "not
   specified" (QUIN `0`) or "unclear" (QUADAS-2). Never infer that an
   operator was trained, an assessor was blinded, or a sample size was
   calculated.
3. **Every item cites where the evidence is** (section, table or page).
   Paraphrase; do not paste long passages from the paper.
4. **Read the full text.** Use the Zotero PDF fulltext of the item, not
   its abstract. If no PDF is attached, mark the study `no-fulltext`,
   skip it, and send it back to `retrieving-pdfs`; do not rate from the
   abstract.
5. **Do the arithmetic with `score_risk_of_bias`**, never by hand or in your
   head. See `empirical-integrity`: every number in the manuscript comes
   from a file a reviewer signed off.
6. **Cite the tools correctly and cite what you used.** Name the tool
   version and any adaptation of its items in the protocol and methods.

## Step 1 — Route each included study

The coding stage writes `rob_tool` for each include (see
`templates/medical/screening_config.py`). Check it; reviewers may
override it.

| Study design | Tool | Notes |
|---|---|---|
| In vitro, on casts, phantoms or models | **QUIN** | 12 criteria, scored 2 / 1 / 0 / NA |
| Compares scanned data against a reference in people or ears (dimensional or categorical accuracy) | **QUADAS-2** | Adapt the signalling questions (Step 3), declare it in the protocol |
| Non-randomised clinical comparison of interventions | **ROBINS-I** | Use the official Cochrane tool |
| Randomised trial | **RoB 2** | Use the official Cochrane tool |
| Technical report with measured data, no comparison | none | Record as "not assessable", list in the characteristics table |

For **ROBINS-I** and **RoB 2** this skill gives routing only: use the
official forms and signalling questions from Cochrane, and the `robvis`
package for the figure. Do not reproduce them from memory.

A study with in vitro and clinical parts is assessed once per part with
the matching tool, and reported that way.

## Step 2 — QUIN (in vitro studies)

Source: Sheth VH, Shah NP, Jain R, Bhanushali N, Bhatnagar V. Development
and validation of a risk-of-bias tool for assessing in vitro studies
conducted in dentistry: the QUIN. *J Prosthet Dent* 2024;131(6):1038-1042.
**Before use, open the paper and copy its exact criterion wording into
the protocol**; the labels below are a working summary, not the tool.

The twelve criteria cover: (1) clearly stated aims and objectives,
(2) explanation of the sample size calculation, (3) explanation of the
sampling technique, (4) details of the comparison group,
(5) explanation of the methodology, (6) operator details,
(7) randomisation, (8) method of measurement of the outcome,
(9) outcome assessor details, (10) blinding, (11) statistical analysis,
(12) presentation of results.

Score each criterion **2** adequately specified, **1** inadequately
specified, **0** not specified, or **NA** not applicable (excluded from
the calculation). Percent = 100 × total ÷ (2 × applicable criteria).
**> 70 % low risk, 50–70 % medium risk, < 50 % high risk.**

How the criteria bite on scanner-accuracy studies (record these as
reviewer guidance in the protocol, so both raters read the items the same
way):

- **Operator details (6):** who operated each scanner, their experience,
  and whether one or several operators were used.
- **Randomisation (7):** order of scanner use or of scan acquisition;
  often NA for a single ear cast scanned by all devices.
- **Method of measurement (8):** the software and algorithm for the
  trueness / precision calculation, the superimposition method and the
  reference data.
- **Blinding (10):** whether the person measuring deviations knew which
  scanner produced the data.
- **Sample size (2):** counts of casts or ears **and** repeated scans per
  condition; a repeated-scan count is not a sample-size calculation.

## Step 3 — QUADAS-2 (accuracy against a reference)

Source: Whiting PF, et al. QUADAS-2: a revised tool for the quality
assessment of diagnostic accuracy studies. *Ann Intern Med*
2011;155(8):529-536. The tool has four **risk-of-bias** domains and
three **applicability** domains, each rated low / high / unclear:

| Domain | Risk of bias | Applicability |
|---|---|---|
| 1 Patient selection | yes | yes |
| 2 Index test (the scanner) | yes | yes |
| 3 Reference standard | yes | yes |
| 4 Flow and timing | yes | — |

QUADAS-2 is built for diagnostic accuracy; scanner **trueness and
precision** are continuous measurement outcomes, so the signalling
questions must be tailored, as the tool's authors allow. Write the
tailored questions into the protocol before rating, for example:

- **Domain 1:** was a consecutive or random set of ears or patients
  scanned? Were inappropriate exclusions avoided (e.g. dropping ears
  that were hard to scan)?
- **Domain 2:** was the scanning protocol pre-specified? Were the
  deviations measured without knowing which scanner produced the data?
- **Domain 3:** is the reference (reference scanner, digitised
  conventional impression, direct measurement) likely to measure the
  true geometry? What is its own stated accuracy?
- **Domain 4:** was one reference applied to all scans? Were all scans
  included in the analysis? Was the time between index and reference
  acquisition short enough that the ear could not have changed?

## Step 4 — Prepare evidence for the reviewers

For each study, read the Zotero fulltext and write a per-item evidence
sheet to `analysis/results/rob/evidence_<study_key>.md`:

```
Item | Draft rating | Where in the paper | Short paraphrase of the evidence
```

Mark the draft clearly as a **proposal**. Do not fill the rating CSVs
used for scoring: reviewers do that.

## Step 5 — Score deterministically

Reviewers record final ratings in `analysis/results/rob/quin_ratings.csv`
(`study_key`, `c01` ... `c12`, `notes`) and/or `quadas2_ratings.csv`
(`study_key`, `d1_rob` ... `d4_rob`, `d1_app` ... `d3_app`). Then call
**`score_risk_of_bias`** once per file, with `instrument` `quin` or
`quadas2`, `input` the ratings CSV and `out` the scored CSV (terminal
equivalent:

```
uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/rob_score.py --instrument quin \
    --input analysis/results/rob/quin_ratings.csv \
    --out   analysis/results/rob/quin_scored.csv
```

and the same with `--instrument quadas2` and the QUADAS-2 files).

The script rejects an empty `study_key`, a value outside `2/1/0/NA`
(QUIN) or `low/high/unclear` (QUADAS-2), a missing column, and a study
whose QUIN criteria are all NA. Read its error, fix the CSV, rerun. The
QUADAS-2 overall judgement is `low` only when every domain is low, `high`
when any domain is high, and `unclear` otherwise.

## Step 6 — Report and carry forward

- Methods: the tool and version, any tailoring, the number of raters,
  how disagreements were resolved, and whether and how a model assisted.
- Results: a per-study table from the scored CSV, plus a traffic-light
  figure (for example `robvis` in R, or a plotting script that reads
  the scored CSV).
- Synthesis: say how risk of bias affects the pooled estimate (a
  sensitivity analysis without high-risk studies is typical) and feed it
  into the certainty-of-evidence rating if the protocol uses GRADE.
- Manuscript numbers come from the scored CSV via `manuscript_stats.py`,
  never typed by hand.

## Red flags

- You are about to rate a study from its abstract.
- You are about to write a QUIN percentage into the text without running
  `score_risk_of_bias`.
- A criterion is rated adequate with nothing citable in the paper.
- Only one person rated the study and the review is described as having
  dual-reviewer appraisal.
- The QUIN criterion wording was copied from this skill rather than from
  the source paper.
