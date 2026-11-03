"""Screening configuration for a BIOMEDICAL / DENTAL systematic review.

Pre-filled for: "Accuracy and acquisition efficiency of digital scanning
methods for auricular prosthesis fabrication". Copy to the project root as
`screening_config.py` (alongside `templates/medical/search_config.py`) and
edit the eligibility criteria to match your registered protocol.

Differences from the social-science template:

- Eligibility is written as P / I / O / S (population, index test,
  outcomes, study design) and the exclusion codes mirror what a PRISMA 2020
  flow diagram reports: one reason per excluded full text.
- Coding fields extract measurement data (scanner, reference standard,
  accuracy metric and value, acquisition time, sample size) and route each
  study to a risk-of-bias tool for the `risk-of-bias` skill.
- The coding prompt forbids inferring numbers. A value the paper does not
  report is coded "not reported", never estimated.

AI-ASSISTED SCREENING AND THE PRISMA / JOURNAL REPORT. Model decisions are
a second reviewer at most. Before relying on them: (1) have a human screen
a random sample (typically 10-20% of records, or every borderline) and
report the agreement (Cohen's kappa) and the sensitivity of the model
against the human; (2) have a human resolve every model "exclude" that a
human would have included in that sample; (3) report the model, the prompt
version below and the date in the methods. The `*_PROMPT_VERSION` strings
exist so the log records which prompt produced each decision.
"""

# =============================================================================
# Tag prefix — namespace for every Zotero tag this review writes
# =============================================================================

# 1-32 chars, lowercase letters, digits and interior hyphens. Changing it
# mid-review orphans every tag already written, so set it once.
TAG_PREFIX = "aur-scan-sr"


# =============================================================================
# PDF retrieval policy (optional overrides; see the base template)
# =============================================================================
# PDF_MIN_VERSION = "accepted"


# =============================================================================
# Abstract screening (stage 1) — title + abstract
# =============================================================================

ABSTRACT_SCREENING_MODEL = ""
ABSTRACT_SCREENING_PROMPT_VERSION = "v1-medical-2026-11-02"


ABSTRACT_SCREENING_SYSTEM_PROMPT = """\
You are a systematic review screener for a dental / maxillofacial \
prosthodontics review. Decide whether a record should go forward to \
full-text assessment.

REVIEW QUESTION:
**What is the accuracy and the acquisition efficiency of digital scanning \
methods used to capture the ear or an auricular defect for auricular \
prosthesis fabrication?**

A record is eligible if it meets ALL of P, I and O, and S:

P — POPULATION / SUBJECT: patients with an acquired or congenital \
auricular defect, OR healthy volunteers' ears, OR ear models, casts or \
phantoms used to evaluate scanning for auricular prostheses. In vitro and \
ex vivo studies on ear casts or phantoms ARE eligible.
   NOT eligible: nasal, orbital, ocular, intraoral (obturator, palatal) or \
   other non-ear prostheses studied alone; hearing-device or implant \
   studies with no scanning component.
   If a study covers several facial prosthesis types AND reports ear data \
   separately, mark BORDERLINE.

I — INDEX TEST: a digital 3D surface or volume acquisition method applied \
to the ear or defect for prosthesis design: intraoral scanner, structured \
light scanner, laser scanner, handheld 3D camera, stereophotogrammetry or \
photogrammetry, smartphone-based 3D scanning, or scanning of an impression \
or cast. CT / CBCT / MRI counts only if it is evaluated as an acquisition \
method for the ear or defect.
   NOT eligible: a study that only uses a scanner without evaluating it \
   AND reports no accuracy or acquisition outcome (handled by O).

O — OUTCOMES (at least one): (a) ACCURACY of the acquired 3D data: \
trueness, precision, deviation, RMS error, surface or landmark distance, \
or agreement with a reference; (b) ACQUISITION EFFICIENCY: scan time, \
processing time, number of scans or images, or workflow time. Clinical \
feasibility with measured time counts.
   If the abstract says accuracy or time was measured but gives no detail, \
   include.

S — STUDY DESIGN: primary quantitative studies — in vitro, in vivo \
comparative, randomised or non-randomised clinical studies, cross-sectional \
accuracy studies, and technical reports that present measured data.

DECISION RULES:
- INCLUDE: P, I, O and S clearly met.
- EXCLUDE: at least one element clearly not met. Use these codes:
  E1-Population: not an ear / auricular subject (nasal, orbital, ocular, \
intraoral or other prosthesis only)
  E2-Index test: no digital scanning / 3D acquisition method evaluated or \
used (e.g. only surgical guides, printing, materials, retention)
  E3-Outcome: no accuracy or acquisition-efficiency data (descriptive case \
report or workflow description with no measured accuracy or time)
  E4-Design: review, editorial, letter, protocol, conference abstract with \
no data, or animal study. For a systematic review or meta-analysis on this \
topic, write E4 and say "review - check reference list" in the reason.
  E5-Other: duplicate, retracted, wrong subject, not retrievable
- BORDERLINE: uncertain — no abstract, mixed facial prostheses, scanning \
used but outcomes unclear, accuracy named in the title only.

BIAS: Be liberal. When unsure between include and borderline, choose \
include. When unsure between borderline and exclude, choose borderline. \
A missed eligible study is worse than one extra full text.

Respond with EXACTLY two lines:
DECISION: include|borderline|exclude
REASON: <one sentence naming the element (P, I, O or S) or the exclusion code>
"""


# =============================================================================
# Full-text screening and coding (stage 2) — full PDF text
# =============================================================================

FULLTEXT_CODING_MODEL = ""
FULLTEXT_CODING_PROMPT_VERSION = "v1-medical-2026-11-02"


FULLTEXT_CODING_FIELDS = [
    {
        "name": "study_design",
        "description": "The study's design as it describes itself.",
        "values": [
            "in-vitro",
            "in-vivo-comparative",
            "randomised-trial",
            "non-randomised-clinical",
            "cross-sectional-accuracy",
            "technical-report-with-data",
            "other",
        ],
        "tag": True,
    },
    {
        "name": "rob_tool",
        "description": "Which risk-of-bias tool fits this design: QUIN for "
                       "in-vitro dental studies; QUADAS-2 for studies "
                       "assessing the accuracy of a test against a reference "
                       "in people; ROBINS-I for non-randomised comparative "
                       "clinical studies; RoB-2 for randomised trials; "
                       "none if no tool fits.",
        "values": ["QUIN", "QUADAS-2", "ROBINS-I", "RoB-2", "none"],
        "tag": True,
    },
    {
        "name": "subject_type",
        "description": "What was scanned.",
        "values": [
            "patients-with-defect",
            "healthy-volunteers",
            "ear-cast-or-impression",
            "phantom-or-model",
            "mixed",
        ],
        "tag": True,
    },
    {
        "name": "scanner_technology",
        "description": "The principal scanning technology evaluated. Use "
                       "'multiple' when several technologies are compared.",
        "values": [
            "intraoral-scanner",
            "structured-light",
            "laser",
            "handheld-3d-camera",
            "stereophotogrammetry",
            "smartphone",
            "ct-cbct",
            "multiple",
            "other",
        ],
        "tag": True,
    },
    {
        "name": "scanners_compared",
        "description": "Every scanner or app named, with manufacturer and "
                       "model as written. Example: 'Trios 3 (3Shape); "
                       "Artec Eva (Artec 3D)'. 'not reported' if absent.",
    },
    {
        "name": "reference_standard",
        "description": "What the scanned data were compared with: reference "
                       "scanner, conventional impression digitised, CT, "
                       "caliper or landmark measurement, or none. "
                       "'none' if there was no reference.",
    },
    {
        "name": "sample_size",
        "description": "Number of ears, patients, casts or scans analysed, "
                       "and the unit. Example: '12 ear casts, 10 scans each'.",
    },
    {
        "name": "accuracy_metrics",
        "description": "Accuracy outcome names and how they were computed "
                       "(trueness, precision, mean deviation, RMSE, "
                       "landmark distance), and the software used. "
                       "'not reported' if the study measured no accuracy.",
    },
    {
        "name": "accuracy_results",
        "description": "The numeric results with units and the page or table "
                       "where each appears. Copy values exactly as written; "
                       "never compute or estimate a value. 'not reported' if "
                       "absent.",
    },
    {
        "name": "acquisition_time_reported",
        "description": "Whether the study reports any acquisition-efficiency "
                       "measure (scan time, processing time, number of "
                       "images or scans, total workflow time).",
        "values": ["yes", "no"],
        "tag": True,
    },
    {
        "name": "acquisition_results",
        "description": "The numeric acquisition-time or efficiency results "
                       "with units and location in the paper. Copy exactly; "
                       "'not reported' if absent.",
    },
    {
        "name": "meta_analysable",
        "description": "Whether means and a measure of spread per scanner or "
                       "condition are reported, so the data could enter a "
                       "meta-analysis.",
        "values": ["yes", "no", "unclear"],
        "tag": True,
    },
    {
        "name": "funding_and_conflicts",
        "description": "Funding source and any declared conflict of interest, "
                       "especially scanner manufacturer involvement. "
                       "'not reported' if absent.",
    },
    {
        "name": "limitations_stated",
        "description": "Limitations the authors state. One or two sentences, "
                       "paraphrased.",
    },
]


FULLTEXT_CODING_SYSTEM_PROMPT = """\
You are a systematic-review data extractor for a dental / maxillofacial \
prosthodontics review. You read the full text of one paper and (1) confirm \
or reject eligibility, then (2) extract a structured record.

REVIEW QUESTION:
What is the accuracy and the acquisition efficiency of digital scanning \
methods used to capture the ear or an auricular defect for auricular \
prosthesis fabrication?

ELIGIBILITY (the abstract passed; confirm each element in the full text):
P — ear / auricular defect subjects, healthy ears, or ear casts, models or \
phantoms.
I — a digital 3D scanning or acquisition method evaluated or used for \
auricular prosthesis fabrication.
O — at least one measured accuracy outcome OR at least one measured \
acquisition-efficiency outcome, with data reported in the paper.
S — a primary quantitative study (in vitro, in vivo comparative, clinical, \
cross-sectional accuracy, or technical report with measured data).

EXCLUSION CODES (full-text stage; give exactly one, the first that applies):
  FE1-Population: not ear / auricular (nasal, orbital, intraoral, etc.), or \
ear data not separable from other prostheses
  FE2-Index test: no digital scanning evaluated or used for the ear
  FE3-Outcome: no measured accuracy or acquisition-efficiency data
  FE4-Design: review, editorial, letter, protocol, case report with no \
measurement, or animal study
  FE5-Duplicate: same data as another included report
  FE6-Not retrievable / not in a language we can read
  FE7-Other: state the reason

OUTPUT FORMAT — strict JSON, one object. Fields:

{{
  "decision": "include" | "exclude",
  "exclusion_code": "<code or empty if include>",
  "reason": "<one to three sentences justifying the decision>",
  {coding_fields_json_placeholder}
}}

RULES:
- Extract from the methods, results, tables and figure captions, not the \
abstract.
- Never infer, compute or estimate a number. If a value is not printed in \
the paper, write "not reported".
- For every numeric value, name where it appears (for example "Table 2").
- If the paper reports several scanners or conditions, keep them separate \
within the field, one per clause, in the order the paper lists them.
- For an excluded paper, give empty strings for every coding field.
- Return ONLY the JSON object, with no text before or after it.
"""
