"""The skills' retrieval doctrine must match the code it describes.

The failure this guards against already happened once: an agent running
a real review tagged 119 items `fulltext-unavailable`, a tag that exists
nowhere in this repo. It was invented on the spot because the skill said
"surface the residual list" and stopped there — no tag, no procedure, no
cause vocabulary.

So both directions are pinned. Every `FailureCause` the code can emit is
named in the skill that has to act on it, and every retrieval tag the
skill names is one the codebase actually knows about.
"""

from __future__ import annotations

import re
from pathlib import Path

import pdf_fetch_log

ROOT = Path(__file__).resolve().parents[2]
SR_SKILL = ROOT / "skills" / "systematic-review" / "SKILL.md"
ZOT_SKILL = ROOT / "skills" / "zotero-operations" / "SKILL.md"


def _sr() -> str:
    return SR_SKILL.read_text(encoding="utf-8")


def _zot() -> str:
    return ZOT_SKILL.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Cause vocabulary
# ---------------------------------------------------------------------------


def test_every_failure_cause_is_documented_for_the_agent() -> None:
    """A cause the code emits but the skill never mentions is a cause the
    agent will not know how to act on."""
    text = _sr()
    missing = [c.value for c in pdf_fetch_log.FailureCause if c.value not in text]
    assert not missing, (
        "these causes are emitted by pdf_fetch_log but absent from "
        f"systematic-review/SKILL.md: {missing}"
    )


def test_the_escalation_ladder_covers_every_cause() -> None:
    text = _zot()
    missing = [c.value for c in pdf_fetch_log.FailureCause if c.value not in text]
    assert not missing, (
        "zotero-operations/SKILL.md's escalation ladder is missing rungs "
        f"for: {missing}"
    )


def test_recoverable_causes_are_marked_as_non_exclusions() -> None:
    """The whole point: these must not be adjudicated as exclusions.

    Matches the cause table's own rows (`| \\`CAUSE\\` | …`) rather than
    any line mentioning the name, so prose elsewhere can discuss a cause
    without satisfying the check.
    """
    text = _sr()
    for cause in pdf_fetch_log.RECOVERABLE_CAUSES:
        row = next(
            (
                ln for ln in text.splitlines()
                if ln.lstrip().startswith(f"| `{cause}` |")
            ),
            None,
        )
        assert row is not None, f"{cause} has no row in the cause table"
        assert "Not an exclusion" in row, (
            f"{cause} is recoverable but its row does not say so: {row}"
        )


def test_only_unavailable_justifies_the_unavailable_tag() -> None:
    text = _sr()
    assert (
        "may not be tagged `fulltext:unavailable` until\nits cause in the "
        "retrieval report is `UNAVAILABLE`" in text
    ), "the hard rule gating the unavailable tag has been weakened or moved"


# ---------------------------------------------------------------------------
# Tag vocabulary
# ---------------------------------------------------------------------------

_TAG = re.compile(r"`(fulltext:[a-z-]+|abstract:[a-z-]+|pdf:[a-z-]+)`")

#: Every retrieval/stage tag the plugin defines. A skill naming anything
#: outside this set is inventing vocabulary, which is exactly what
#: produced `fulltext-unavailable`.
KNOWN_TAGS = {
    "abstract:include", "abstract:exclude", "abstract:borderline",
    "fulltext:include", "fulltext:exclude", "fulltext:unavailable",
    "pdf:tdm-recovered",
}


def test_skills_invent_no_tags() -> None:
    for path in (SR_SKILL, ZOT_SKILL):
        found = set(_TAG.findall(path.read_text(encoding="utf-8")))
        unknown = found - KNOWN_TAGS
        assert not unknown, f"{path.name} names undefined tag(s): {unknown}"


def test_the_invented_tag_spelling_is_not_used_anywhere() -> None:
    """`fulltext-unavailable` (hyphen) was made up mid-run.

    The namespace separator in this plugin is a colon, and the tag is
    mutually exclusive with the other `fulltext:*` tags — which only
    works if it is spelled as one of them.
    """
    offenders = [
        f"{p.relative_to(ROOT)}:{i}"
        for p in ROOT.glob("skills/**/*.md")
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if "fulltext-unavailable" in line
    ]
    assert not offenders, (
        "use `fulltext:unavailable` (colon), the namespaced form: "
        + ", ".join(offenders)
    )


# ---------------------------------------------------------------------------
# The procedure itself
# ---------------------------------------------------------------------------


def test_diagnosis_is_stated_as_mandatory() -> None:
    text = _sr()
    assert "diagnose before you exclude" in text.lower()
    assert "mandatory" in text.lower()


def test_the_skill_names_the_command_that_produces_the_report() -> None:
    """A procedure the agent cannot run is not a procedure."""
    assert "audit_zotero_library.py" in _sr()
    assert "--sources browser --publisher" in _sr()


def test_the_retry_key_files_the_audit_writes_are_named() -> None:
    """The audit writes these; the skill must tell the agent to use them
    rather than assembling key lists by hand."""
    text = _sr()
    for label in ("retry.browser", "retry.ill", "true_negative"):
        assert label in text, f"{label} key file not mentioned in SKILL.md"
    assert "--filter-keys-file" in text
