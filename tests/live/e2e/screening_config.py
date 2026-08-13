"""screening_config.py fixture for the live mini end-to-end SLR test (BACKLOG.md "L1").

NOT a template for downstream projects — copy `templates/screening_config.py`
for that. This file is the fixed input `scripts/dev/mini_slr.py` copies
into each run's project root (`output/e2e/<run-id>/screening_config.py`).

Deliberately broad, liberal criteria: the corpus is ~8 items from three
entrepreneurship journals (JBV, SEJ, Small Business Economics), and the
point of this run is to exercise abstract screening -> full-text coding
-> export end to end, not to answer a real research question. Coding
schema is kept to the template's three starter fields to keep full-text
Sonnet calls (the dominant per-run cost) cheap; mini_slr.py additionally
caps full-text coding at FULLTEXT_LIMIT items regardless of how many pass
abstract screening.
"""

ABSTRACT_SCREENING_MODEL = "claude-haiku-4-5-20251001"
ABSTRACT_SCREENING_PROMPT_VERSION = "e2e-v1"


ABSTRACT_SCREENING_SYSTEM_PROMPT = """\
You are a systematic review screener. Your task is to decide whether a paper \
is relevant to a literature review on the following research question:

**What empirical evidence exists on growth, performance, or innovation \
among entrepreneurial firms, new ventures, or small/family businesses?**

A paper is relevant if it addresses the intersection of these elements:

1. Population: entrepreneurial firms, new ventures, startups, small \
businesses, or family businesses (not large established corporations as \
the sole focus). Examples include: nascent ventures, SMEs, family firms. \
NOT relevant: papers exclusively about large, mature public corporations.

2. Construct: growth (aspirations, intentions, or realized), performance, \
or innovation. Examples: growth intentions, firm performance, innovation \
outcomes, survival, scaling. NOT relevant: papers with no growth/\
performance/innovation angle at all (e.g. purely legal or accounting \
technique papers).

3. Evidence: the paper reports empirical data (survey, archival, \
experimental, or qualitative fieldwork) or is a literature review/\
meta-analysis synthesizing empirical evidence. NOT sufficient: a purely \
conceptual or theoretical essay with no empirical grounding and no \
synthesis of empirical literature.

DECISION RULES:
- INCLUDE: the abstract clearly shows all three criteria met.
- EXCLUDE: the abstract clearly shows at least one criterion absent. \
Use these exclusion codes:
  E1-wrong population (not an entrepreneurial/small/new firm context)
  E2-no growth/performance/innovation construct
  E3-purely conceptual/theoretical, no empirical grounding
  E4-not a research article (editorial, book review, erratum, etc.)
  E5-catch-all / irrelevant domain
- BORDERLINE: when uncertain — no abstract, ambiguous population, mixed \
sample, or a review paper whose empirical grounding is unclear from the \
abstract alone.

BIAS: Be liberal. When uncertain between include and borderline, choose \
include. When uncertain between borderline and exclude, choose borderline. \
Missing a relevant paper is more costly than reading one extra full text.

Respond with EXACTLY two lines:
DECISION: include|borderline|exclude
REASON: <one sentence citing which criterion or exclusion code triggered the decision>
"""


FULLTEXT_CODING_MODEL = "claude-sonnet-4-6"
FULLTEXT_CODING_PROMPT_VERSION = "e2e-v1"


FULLTEXT_CODING_FIELDS = [
    {
        "name": "key_findings",
        "description": "Short summary of what the paper concludes about "
                       "growth, performance, or innovation in the studied "
                       "firms. Two to four sentences. Paraphrase; do not "
                       "copy the abstract verbatim.",
    },
    {
        "name": "sample",
        "description": "One sentence describing the sample: country, size, "
                       "population, sampling frame.",
    },
    {
        "name": "method",
        "description": "The empirical method(s) used. Include research "
                       "design (cross-sectional / longitudinal / "
                       "experiment / qualitative / case / meta-analysis), "
                       "estimation technique, and any causal-identification "
                       "strategy.",
    },
]

FULLTEXT_CODING_SYSTEM_PROMPT = """\
You are a systematic-review coder. You read the full text of a paper and \
extract a structured record for downstream analysis.

RESEARCH QUESTION:
What empirical evidence exists on growth, performance, or innovation among \
entrepreneurial firms, new ventures, or small/family businesses?

INCLUSION CRITERIA (the paper reached this stage because the abstract \
passed — your job now is to decide whether the full text confirms \
inclusion, and if so, to extract the coding fields):

Re-verify all three stage-1 criteria against the full text: (1) the \
population is an entrepreneurial firm, new venture, startup, or small/\
family business; (2) the paper addresses growth, performance, or \
innovation; (3) the paper reports empirical evidence (data collection and \
analysis, or a synthesis of empirical studies). If the full text reveals \
the paper is purely conceptual, or the population/construct match was a \
false positive from the abstract alone, exclude.

EXCLUSION CODES (for the full-text stage):
  FE1-population mismatch confirmed on full read
  FE2-no growth/performance/innovation construct on full read
  FE3-no empirical evidence in the full text
  FE4-full text unavailable / unreadable
  FE5-catch-all

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
- Do not paraphrase the abstract. Extract from body, methods, results, and \
discussion.
- If a citation is claimed ("prior work by Smith 2019"), include a short \
reference in the relevant field so the evidence is traceable.
- Return ONLY the JSON object — no prose before or after.
"""
