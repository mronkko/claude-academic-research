"""Per-project screening configuration for a systematic review.

Copy this file to the root of your SLR project and edit the tag
prefix and the two prompts (abstract screening + full-text coding) for
your specific review. `abstract_screen.py` and `fulltext_code.py` read
this module by path (via `--config`); `import_to_zotero.py`,
`export_coded_includes.py`, `apply_qa_adjudications.py` and
`manage_tags.py` read `TAG_PREFIX` from it.

The prompts ARE the scope of your screening — reviewers will read
them to judge whether your decisions can be reproduced. Keep this
file in git; version the prompts via `*_PROMPT_VERSION` strings so
log rows record which version rendered each decision.

Usage:
    uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/abstract_screen.py \\
        --config ./screening_config.py --group <id> --collection <key>
"""

# =============================================================================
# Tag prefix — the namespace for every Zotero tag this review writes
# =============================================================================

# MANDATORY. Every tag recording *this review's* judgement is written as
# `<TAG_PREFIX>/<family>:<value>` — `agentic-ai/abstract:include`,
# `agentic-ai/fulltext:exclude`, `agentic-ai/qa-adjudicated-include`.
#
# Two reasons it exists, both of which bite on a library you actually use:
#
#   1. Filtering. A library with hundreds of personal tags gives Zotero's tag
#      selector no way to show just this review's. `abstract:` and `fulltext:`
#      are the plugin's vocabulary, not yours, so they distinguish nothing.
#      Typing your prefix into the selector isolates exactly one review.
#   2. Collisions. One library commonly holds several reviews. Without a
#      namespace they share the `abstract:*` family, and a Zotero duplicate
#      merge unions tag sets — leaving one item carrying two reviews'
#      contradictory decisions, with no way to tell which said what.
#
# Rules: 1-32 characters, lowercase letters, digits and interior hyphens.
# No spaces, no `/`, no `:`, and it may not start or end with a hyphen.
# Pick something short and recognisable — you will read it on every tag.
#
# Set it with the helper (which validates and shows you the resulting tags):
#     python3 ${CLAUDE_PLUGIN_ROOT}/scripts/setup/set_tag_prefix.py \
#         --prefix <your-prefix>
#
# Changing it mid-review orphans every tag already written under the old
# value; the scripts will treat already-screened items as unscreened.
TAG_PREFIX = ""


# =============================================================================
# Abstract screening (stage 1) — the fast tier, on title + abstract
# =============================================================================

# Model pin — filled in at bootstrap by `scripts/setup/resolve_models.py`,
# which asks your configured provider which models it currently serves and
# writes the newest one in the "fast" tier here.
#
# Leave it empty and the pipeline falls back to the plugin's shipped
# catalogue, which may be out of date; run resolve_models.py for a
# current pin. Once written, this is the project's committed default —
# keep it in git alongside the prompt, because it is what a reviewer
# reads to reconstruct the review.
#
# For a one-off run with a different model, pass `--model fast|balanced|
# deep` (or a short alias, or a full model ID) on the command line
# instead of editing this file; the effective model is recorded in the
# `model` column of the CSV log either way.
ABSTRACT_SCREENING_MODEL = ""
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
# Full-text coding (stage 2) — the balanced tier, on full PDF text
# =============================================================================

# Model pin — filled in at bootstrap by `scripts/setup/resolve_models.py`
# with the newest model in your provider's "balanced" tier.
#
# Same rule as ABSTRACT_SCREENING_MODEL above: `--model` overrides this
# for a single run without touching the file.
FULLTEXT_CODING_MODEL = ""
FULLTEXT_CODING_PROMPT_VERSION = "v1-2026-04-21"


# Define every coding field the script should extract. Each entry:
#   name         — snake_case column name (goes into CSV + manuscript)
#   description  — free-text guidance given to the LLM
#   example      — optional one-sentence example of what a good value looks like
#   values       — optional closed vocabulary. The permitted answers appear
#                  in the prompt's JSON schema, and anything else the model
#                  returns is still recorded but flagged.
#   tag          — optional, requires `values`. Also write the coded value as
#                  a Zotero tag, `<TAG_PREFIX>/<field>:<value>`, so you can
#                  filter and browse the corpus by it in Zotero. Re-coding
#                  replaces the tag; `--full-recode` clears it.
#
# Only a categorical field can be tagged — free text would produce one tag
# per paper. Think about how many categories a field really has before
# tagging it: five is useful in the tag selector, forty is noise. You can
# change your mind either way — add `tag: True` and re-code, or drop a
# family you regret with
#     uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/manage_tags.py \
#         --collection <KEY> --prune <field> --apply
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
    # A categorical field, tagged. Every include gets exactly one
    # `<TAG_PREFIX>/research-design:<value>` tag, so the Zotero tag
    # selector becomes a way to browse the corpus by design. Delete this
    # entry, or drop `"tag": True`, if it is not how you want to slice
    # your papers.
    {
        "name": "research_design",
        "description": "The paper's primary empirical design.",
        "values": [
            "experiment",
            "survey",
            "panel",
            "case-study",
            "simulation",
            "meta-analysis",
            "conceptual",
        ],
        "tag": True,
    },
    # Add as many fields as your coding schema demands. 5–15 is typical.
    # Suggested additions for entrepreneurship SLRs:
    #   theories_and_references  — theoretical lenses used
    #   direction_of_relationship — sign of the main effect (categorical;
    #                               a good `values` + `tag` candidate)
    #   moderators_boundary_conditions — specified boundary conditions
    #   causal_inference_strength — RCT / quasi-experiment / observational
    #                               (categorical; another good one to tag)
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
