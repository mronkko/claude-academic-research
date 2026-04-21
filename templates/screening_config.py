"""Per-project screening configuration for a systematic review.

Copy this file to the root of your SLR project and edit the two
prompts (abstract screening + full-text coding) for your specific
review. `abstract_screen.py` and `fulltext_code.py` read this module
by path (via `--config`).

The prompts ARE the scope of your screening — reviewers will read
them to judge whether your decisions can be reproduced. Keep this
file in git; version the prompts via `*_PROMPT_VERSION` strings so
log rows record which version rendered each decision.

Usage:
    uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/abstract_screen.py \\
        --config ./screening_config.py --group <id> --collection <key>
"""

# =============================================================================
# Abstract screening (stage 1) — Claude Haiku on title + abstract
# =============================================================================

ABSTRACT_SCREENING_MODEL = "claude-haiku-4-5-20251001"
ABSTRACT_SCREENING_PROMPT_VERSION = "v1-2026-04-21"

ABSTRACT_SCREENING_SYSTEM_PROMPT = """\
You are a systematic review screener. Your task is to decide whether a paper \
is relevant to a literature review on the following research question:

**<INSERT YOUR RESEARCH QUESTION HERE>**

A paper is relevant if it addresses the intersection of these elements:

1. <CRITERION 1 — e.g. population / context>. Examples include: ... \
NOT relevant: ...

2. <CRITERION 2 — e.g. independent variable or construct>. Examples: ... \
NOT relevant: ...

3. <CRITERION 3 — e.g. outcome or dependent variable>. Examples: ... \
NOT sufficient: ...

DECISION RULES:
- INCLUDE: the abstract clearly shows all criteria met.
- EXCLUDE: the abstract clearly shows at least one criterion absent. \
Use these exclusion codes:
  E1-<first exclusion reason, e.g. wrong population>
  E2-<second reason, e.g. no key construct>
  E3-<third reason>
  E4-<fourth reason>
  E5-<catch-all / irrelevant domain>
- BORDERLINE: when uncertain — no abstract, ambiguous construct, \
incidental mention, mixed population, etc.

BIAS: Be liberal. When uncertain between include and borderline, choose \
include. When uncertain between borderline and exclude, choose borderline. \
Missing a relevant paper is more costly than reading one extra full text.

Respond with EXACTLY two lines:
DECISION: include|borderline|exclude
REASON: <one sentence citing which criterion or exclusion code triggered the decision>
"""


# =============================================================================
# Full-text coding (stage 2) — Claude Sonnet on full PDF text
# =============================================================================

FULLTEXT_CODING_MODEL = "claude-sonnet-4-6"
FULLTEXT_CODING_PROMPT_VERSION = "v1-2026-04-21"

# Define every coding field the script should extract. Each entry:
#   name         — snake_case column name (goes into CSV + manuscript)
#   description  — free-text guidance given to the LLM
#   example      — optional one-sentence example of what a good value looks like
#
# The script serialises these into the JSON schema section of the prompt.
# Add, remove, or reorder fields freely — the CSV schema follows this list.
FULLTEXT_CODING_FIELDS = [
    {
        "name": "key_findings",
        "description": "Short summary of what the paper concludes about the "
                       "relationship under study. Two to four sentences. "
                       "Paraphrase; do not copy the abstract verbatim.",
    },
    {
        "name": "sample",
        "description": "One sentence describing the sample: country, size, "
                       "population, sampling frame. Example: 'Survey of 1,243 "
                       "Finnish nascent entrepreneurs drawn from GEM 2014.'",
    },
    {
        "name": "method",
        "description": "The empirical method(s) used. Include research design "
                       "(cross-sectional / longitudinal / experiment / "
                       "qualitative / case / meta-analysis), estimation "
                       "technique, and any causal-identification strategy.",
    },
    # Add as many fields as your coding schema demands. 5–15 is typical.
    # Suggested additions for entrepreneurship SLRs:
    #   theories_and_references  — theoretical lenses used
    #   direction_of_relationship — sign of the main effect
    #   moderators_boundary_conditions — specified boundary conditions
    #   causal_inference_strength — RCT / quasi-experiment / observational
    #   future_research          — explicit gaps the authors call out
]

FULLTEXT_CODING_SYSTEM_PROMPT = """\
You are a systematic-review coder. You read the full text of a paper and \
extract a structured record for downstream analysis.

RESEARCH QUESTION:
<INSERT YOUR RESEARCH QUESTION — same as abstract screening>

INCLUSION CRITERIA (the paper reached this stage because the abstract \
passed — your job now is to decide whether the full text confirms \
inclusion, and if so, to extract the coding fields):

<INSERT YOUR STAGE-2 CRITERIA HERE. Typically: re-verify all stage-1 \
criteria against the full text; check for population/construct/outcome \
assumptions that the abstract didn't clarify>

EXCLUSION CODES (for the full-text stage):
  FE1-<full-text exclusion reason>
  FE2-<...>
  FE3-<...>
  FE4-<...>
  FE5-<catch-all>

OUTPUT FORMAT — strict JSON, one object. Fields:

{{
  "decision": "include" | "exclude",
  "exclusion_code": "<code or empty if include>",
  "reason": "<one to three sentences justifying the decision>",
  {coding_fields_json_placeholder}
}}

Additional rules:
- For every coding field above, provide SUBSTANTIVE content if include, or \
an empty string if exclude.
- Do not paraphrase the abstract. Extract from body, methods, results, \
and discussion.
- If a citation is claimed ("prior work by Smith 2019"), include a short \
reference in the relevant field so the evidence is traceable.
- Return ONLY the JSON object — no prose before or after.
"""
