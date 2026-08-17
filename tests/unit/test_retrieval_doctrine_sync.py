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
#:
#: The screening tags are hand-listed because they are defined in prose
#: doctrine, not in code. The `pdf:*` tags are code constants, and this
#: set drifted from them once — `pdf:repository-copy` shipped in
#: `fetchers/core.py` but was never added here, so the guard would have
#: rejected a skill for naming a tag the pipeline genuinely attaches.
#: `test_known_tags_covers_every_pdf_tag_in_code` now fails instead of
#: letting that recur.
KNOWN_TAGS = {
    "abstract:include", "abstract:exclude", "abstract:borderline",
    "fulltext:include", "fulltext:exclude", "fulltext:unavailable",
    "pdf:tdm-recovered", "pdf:preprint-version", "pdf:repository-copy",
}


def test_skills_invent_no_tags() -> None:
    for path in (SR_SKILL, ZOT_SKILL):
        found = set(_TAG.findall(path.read_text(encoding="utf-8")))
        unknown = found - KNOWN_TAGS
        assert not unknown, f"{path.name} names undefined tag(s): {unknown}"


def test_known_tags_covers_every_pdf_tag_in_code() -> None:
    """The `pdf:*` vocabulary is defined by code constants, so this set
    must be a superset of them. Guards the direction the hand-maintained
    list actually drifts: a new tag lands in a fetcher, no one updates
    the list, and the next skill to document the tag fails the build for
    the wrong reason."""
    from fetchers.core import REPOSITORY_COPY_TAG
    from fetchers.preprint import PREPRINT_VERSION_TAG
    from fetchers.sciencedirect import TDM_RECOVERED_TAG

    in_code = {REPOSITORY_COPY_TAG, PREPRINT_VERSION_TAG, TDM_RECOVERED_TAG}
    assert in_code <= KNOWN_TAGS, (
        f"code defines pdf tag(s) missing from KNOWN_TAGS: "
        f"{in_code - KNOWN_TAGS}"
    )


def test_the_preprint_tag_is_spelled_the_same_everywhere() -> None:
    """Four copies of this string exist, and three of them are deliberate:
    `audit_zotero_library.py` and `fulltext_code.py` both repeat it rather
    than import a fetcher they otherwise have no dependency on. A drift
    between any two means the tag is written by one stage and invisible
    to the next — which is the whole failure this tag exists to prevent.
    """
    import audit_zotero_library
    import fulltext_code
    from fetchers.preprint import PREPRINT_VERSION_TAG

    assert audit_zotero_library.PREPRINT_VERSION_TAG == PREPRINT_VERSION_TAG
    assert fulltext_code.PREPRINT_VERSION_TAG == PREPRINT_VERSION_TAG
    assert f"`{PREPRINT_VERSION_TAG}`" in _sr(), (
        "the tag catalogue in systematic-review/SKILL.md does not name it"
    )


def test_the_preprint_route_is_offered_before_declaring_unavailable() -> None:
    """A flag documented only in `--help` is a flag the agent never
    offers, which makes the opt-in indistinguishable from not shipping
    it."""
    text = _sr()
    assert "--allow-preprints" in text
    assert "peer review" in text, (
        "the skill names the flag without saying what makes it hazardous"
    )


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


# ---------------------------------------------------------------------------
# The agent-drivable browser pass
# ---------------------------------------------------------------------------
#
# The control-file handshake only helps if the skills tell the agent to
# use it. Before it existed the honest advice was "open your own
# terminal", and that sentence outliving the limitation would send users
# away for no reason.


def _enrich_parser():
    import enrich_pdfs

    return enrich_pdfs._build_parser()


def test_both_skills_tell_the_agent_to_drive_the_browser_pass() -> None:
    for name, text in (("systematic-review", _sr()), ("zotero-operations", _zot())):
        assert "--control-file" in text, (
            f"{name}/SKILL.md never mentions --control-file, so the agent "
            f"will still hand the user a command to paste"
        )
        assert "run_in_background" in text, (
            f"{name}/SKILL.md does not say to background the run, which is "
            f"what makes the handshake possible"
        )


def test_the_skills_explain_the_seq_echo() -> None:
    """Replying with the wrong seq is silently ignored. An agent that
    does not know why would read it as the script hanging."""
    for text in (_sr(), _zot()):
        assert "seq" in text


def test_every_browser_flag_the_skills_name_actually_exists() -> None:
    """A stale flag in a SKILL.md fails at the moment the user is already
    stuck — and silently for us."""
    import re

    parser = _enrich_parser()
    known = {
        opt for action in parser._actions for opt in action.option_strings
    }
    for name, text in (("systematic-review", _sr()), ("zotero-operations", _zot())):
        for flag in set(re.findall(r"(?<![\w-])--[a-z][a-z0-9-]+", text)):
            # Only check flags in enrich_pdfs' own namespace; the skills
            # also document other scripts.
            if flag in ("--control-file", "--auto-publishers", "--no-prompt",
                        "--filter-keys-file", "--sources", "--plan",
                        "--control-timeout", "--progress-json"):
                assert flag in known, f"{name}/SKILL.md names unknown {flag}"
