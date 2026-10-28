"""screening_config.py for the `demo-slr` skill.

The skill fills every `<...>` placeholder with the presenter's approved
topic and criteria, sets TAG_PREFIX to `demo-<timestamp>`, and installs
this file as `screening_config.py` in the demo project. The model pins are
left empty on purpose: the run uses `--model fast` / `--model balanced`.
"""

TAG_PREFIX = ""   # <demo-YYYYMMDD-HHMM>

ABSTRACT_SCREENING_MODEL = ""
ABSTRACT_SCREENING_PROMPT_VERSION = "demo-v1"

ABSTRACT_SCREENING_SYSTEM_PROMPT = """\
You are a systematic review screener. Decide whether a paper is relevant to \
a literature review on this research question:

**<RESEARCH QUESTION>**

A paper is relevant if it meets both criteria:

1. <CRITERION 1>. NOT relevant: <what fails it>.

2. <CRITERION 2>. NOT relevant: <what fails it>.

DECISION RULES:
- INCLUDE: the abstract clearly shows both criteria met.
- EXCLUDE: at least one criterion is clearly absent. Exclusion codes:
  E1-<first reason>
  E2-<second reason>
  E3-not a research article (editorial, erratum, review of a book)
  E4-<fourth reason>
  E5-catch-all / irrelevant domain
- BORDERLINE: when uncertain.

BIAS: Be liberal. When uncertain between include and borderline, choose \
include. When uncertain between borderline and exclude, choose borderline.

Respond with EXACTLY two lines:
DECISION: include|borderline|exclude
REASON: <one sentence citing which criterion or exclusion code triggered the decision>
"""

FULLTEXT_CODING_MODEL = ""
FULLTEXT_CODING_PROMPT_VERSION = "demo-v1"

FULLTEXT_CODING_FIELDS = [
    {
        "name": "key_findings",
        "description": "What the paper concludes about the question. Two "
                       "sentences, paraphrased.",
    },
    {
        "name": "sample",
        "description": "One sentence: country, size and population of the "
                       "sample or data.",
    },
    {
        "name": "method",
        "description": "The empirical method in one sentence.",
    },
    {
        "name": "research_design",
        "description": "The paper's primary empirical design.",
        "values": ["experiment", "survey", "panel", "case-study",
                   "simulation", "meta-analysis", "conceptual"],
        "tag": True,
    },
    {
        "name": "evidence_quotes",
        "description": "Between 3 and 6 verbatim quotes from the paper that "
                       "support the values you gave above, as a JSON list of "
                       "{\"field\": <one of key_findings, sample, method>, "
                       "\"quote\": <exact words copied from the text, 10 to "
                       "30 words, from one page only, no ellipses>}. Copy "
                       "character for character; never paraphrase.",
    },
]

FULLTEXT_CODING_SYSTEM_PROMPT = """\
You are a systematic-review coder. You read the full text of a paper and \
extract a structured record.

RESEARCH QUESTION:
<RESEARCH QUESTION>

INCLUSION CRITERIA (confirm them against the full text):
<CRITERIA, as in abstract screening>

EXCLUSION CODES (full-text stage):
  FE1-<reason>
  FE2-<reason>
  FE3-<reason>
  FE4-<reason>
  FE5-catch-all

OUTPUT FORMAT — strict JSON, one object. Fields:

{{
  "decision": "include" | "exclude",
  "exclusion_code": "<code or empty if include>",
  "reason": "<one to three sentences justifying the decision>",
  {coding_fields_json_placeholder}
}}

Additional rules:
- For every coding field, give SUBSTANTIVE content if include, or an empty \
string if exclude.
- Quotes in evidence_quotes must be exact copies of the paper's own words.
- Return ONLY the JSON object — no prose before or after.
"""
