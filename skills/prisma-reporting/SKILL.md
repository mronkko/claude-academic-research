---
name: prisma-reporting
description: Use when preparing the PRISMA 2020 reporting of a systematic review (flow diagram counts, search reporting, checklist, AI-use disclosure, protocol registration). Trigger phrases "PRISMA flow diagram", "PRISMA checklist", "PRISMA 2020", "PRISMA-S", "register protocol", "PROSPERO". Do NOT use for running search, screening or coding - use `systematic-review`; for study quality appraisal - use `risk-of-bias`.
---

# prisma-reporting

> **Glossary:** unfamiliar with **PRISMA**, **BBT** or **stage tag**? See
> [skills/_glossary.md](../_glossary.md).

**Plugin tools.** Steps name the tools of the plugin's own
**academic-research** MCP server by bare name (`prisma_flow`); in Claude
Code it appears as
`mcp__plugin_academic-research_academic-research__prisma_flow`, on other
hosts with that host's prefix. If the tools are not available, run the
*terminal equivalent* given beside each with Bash instead, and tell the
user once how to enable the server (see **Terminal equivalent** in
[skills/_glossary.md](../_glossary.md)).

## Overview

This skill covers what a PRISMA 2020 report needs beyond running the
pipeline: the flow-diagram numbers, the search report, the checklist, and
the disclosure of any AI assistance. The numbers come from the pipeline's
own logs through `prisma_flow`. **Never type a PRISMA number by hand and
never adjust one to make a check pass**: a failed check is a finding to
explain in the manuscript or to fix in the data.

Source documents the reviewers must read and cite: Page MJ et al., *BMJ*
2021;372:n71 (PRISMA 2020); Rethlefsen ML et al., *Syst Rev* 2021;10:39
(PRISMA-S, for the search). This skill summarises; the papers and the
official checklist are authoritative.

## Step 1 - Protocol and registration (before screening)

Register the protocol before screening starts (PROSPERO accepts reviews
with a health-related outcome; OSF otherwise) and record the ID. If the
review deviates from the registered protocol later, say how and why in
the methods. Put the PICO-style eligibility criteria and the planned
outcomes in the protocol; the same text drives
`templates/medical/screening_config.py`.

## Step 2 - Search reporting (PRISMA 2020 items 6-7, PRISMA-S)

For each database report the platform or interface, the date it was last
searched, and the full query as run. `search_metadata.json` (written by
`start_search`) holds the databases, date range, queries and fields
searched; cite it rather than retyping. Searches run outside the pipeline
(Embase, Cochrane, registers, reference-list checking) must be written
down by the reviewers with the same details and the number of records
each returned.

## Step 3 - Flow counts

Call **`prisma_flow`** after screening and full-text coding finish.
Terminal equivalent:
`uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/prisma_flow.py --strict`.

It reads `search_metadata.json`, the abstract log and the full-text log
(latest decision per item) and writes `analysis/results/prisma_flow.json`,
`prisma_flow.mmd` (Mermaid diagram) and `prisma_flow.md` (table).

Counts the pipeline cannot know go in a reviewer-written JSON file passed
as `extra`, for example `analysis/prisma_extra.json`:

```json
{
  "databases_extra": {"embase": 412},
  "registers": {"clinicaltrials.gov": 3},
  "duplicates_removed_extra": 188,
  "other_methods_extra": {"reference list checking": 14},
  "reports_not_retrieved": 2,
  "studies_included": 11
}
```

`studies_included` is set only when several reports describe one study.
Records the reviewers add by hand are labelled in the output as reported
by the reviewers, not computed.

Read the result before using it:

- Every failed check is listed with the two numbers that disagree. Find
  the cause (items screened twice, an error decision, a hand-imported
  record missing from the extra file) and fix the data or the extra file.
- A note says when sought reports are not in the full-text log yet. Rerun
  once coding is finished, or set `reports_not_retrieved`.
- Every full-text exclusion needs a reason (PRISMA item 16b). The count
  by reason comes from the `exclusion_code` column.

Render the `.mmd` file into the figure for the manuscript. The numbers in
the text come from `prisma_flow.json`, through `manuscript_stats.py`,
never typed.

## Step 4 - Checklist

Copy `${CLAUDE_PLUGIN_ROOT}/templates/medical/prisma_2020_checklist.csv`
to the project and fill the `where_reported` and `status` columns as the
manuscript is written. Item wording in that file is a short paraphrase;
submit the official checklist from the PRISMA statement site with the
manuscript, using this file to track progress.

## Step 5 - Disclose AI assistance

State in the methods what a language model did (for example abstract
screening as a second reviewer, drafting of extraction fields), the model
name and version, the prompt version recorded in the logs, the date, and
how it was validated: the human-screened sample, the agreement (Cohen's
kappa) and the sensitivity against the human decisions. A model decision
that no human checked is not a screening decision. Include the prompt
files (`screening_config.py`) as supplementary material.

## Red flags

- A PRISMA number in the manuscript that is not in `prisma_flow.json`.
- A check was failing and you changed the log or the extra file to make
  it pass, rather than finding the cause.
- Hand-imported records (Embase, Cochrane) are in Zotero but not in the
  extra file, so identification counts are too low.
- The flow diagram says "studies included" but the number is reports.
- The AI-use disclosure is missing or does not name the validation.
