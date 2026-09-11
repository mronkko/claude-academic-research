"""Tests for the per-review tag namespace.

The prefix is the one thing standing between two reviews that share a Zotero
library, so the validation rules matter more than their size suggests: a
prefix containing `/` or `:` would make `family()` ambiguous and let one
review's `remove_prefixed` sweep reach into another's tags.
"""

from __future__ import annotations

import argparse
import types

import pytest
import tag_prefix as tp

# ---------------------------------------------------------------------------
# validate / namespace
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["agentic-ai", "slr2026", "a", "remote-work-meta-analysis", "x1-y2-z3"],
)
def test_well_formed_prefixes_are_accepted(raw):
    assert tp.validate(raw) == raw


@pytest.mark.parametrize(
    "raw",
    [
        "My Review",       # spaces and uppercase
        "UPPER",
        "a/b",             # would forge a second namespace level
        "x:y",             # would forge a second family level
        "-lead",
        "trail-",
        "double--hyphen",  # ambiguous when slugged back
        "",
        "   ",
        "a" * 33,
        "emoji-\U0001f600",
    ],
)
def test_malformed_prefixes_are_rejected(raw):
    with pytest.raises(ValueError):
        tp.validate(raw)


def test_non_strings_are_rejected_by_type_not_by_crashing():
    with pytest.raises(ValueError) as exc:
        tp.validate(123)
    assert "must be a string" in str(exc.value)


def test_surrounding_whitespace_is_stripped_rather_than_rejected():
    assert tp.validate("  agentic-ai  ") == "agentic-ai"


def test_every_rejection_states_the_rules():
    for raw in ("My Review", "a/b", "", "a" * 33):
        with pytest.raises(ValueError) as exc:
            tp.validate(raw)
        assert "lowercase letters" in str(exc.value)


def test_namespace_appends_the_separator():
    assert tp.namespace("agentic-ai") == "agentic-ai/"


def test_namespace_validates_too():
    with pytest.raises(ValueError):
        tp.namespace("Bad Prefix")


# ---------------------------------------------------------------------------
# family / slug / coding tags
# ---------------------------------------------------------------------------


def test_family_builds_a_removable_prefix():
    ns = tp.namespace("agentic-ai")
    assert tp.family(ns, "abstract") == "agentic-ai/abstract:"
    assert tp.family(ns, "fulltext") == "agentic-ai/fulltext:"


def test_family_output_is_what_stage_tag_op_expects():
    """The whole point of the design: `screening_common` needs no changes."""
    import screening_common as sc

    ns = tp.namespace("agentic-ai")
    op = sc.stage_tag_op(tp.family(ns, "abstract"), "include")
    assert op == {
        "add": ["agentic-ai/abstract:include"],
        "remove_prefixed": ["agentic-ai/abstract:"],
    }


def test_one_reviews_family_does_not_prefix_match_anothers():
    """`remove_prefixed` is a plain startswith, so this is the real guard."""
    mine = tp.family(tp.namespace("agentic-ai"), "abstract")
    theirs = tp.family(tp.namespace("remote-work"), "abstract")
    assert not f"{theirs}include".startswith(mine)
    assert not f"{mine}include".startswith(theirs)


def test_a_prefix_does_not_sweep_a_longer_prefix_sharing_its_stem():
    """`slr` must not clear `slr-2026`'s tags."""
    short = tp.family(tp.namespace("slr"), "abstract")
    longer = tp.family(tp.namespace("slr-2026"), "abstract")
    assert not f"{longer}include".startswith(short)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("research_design", "research-design"),
        ("Case study", "case-study"),
        ("panel", "panel"),
        ("RCT", "rct"),
        ("quasi--experiment", "quasi-experiment"),
        ("  padded  ", "padded"),
        ("with:colon", "with-colon"),
        ("with/slash", "with-slash"),
        ("punctuation!?", "punctuation"),
    ],
)
def test_slug_normalises_field_and_value_names(raw, expected):
    assert tp.slug(raw) == expected


def test_slug_neutralises_separators_so_a_value_cannot_forge_a_family():
    ns = tp.namespace("agentic-ai")
    tag = tp.coding_tag(ns, "design", "panel:include")
    assert tag == "agentic-ai/design:panel-include"
    assert tag.count(":") == 1
    assert tag.count("/") == 1


def test_coding_family_and_tag_compose():
    ns = tp.namespace("agentic-ai")
    assert tp.coding_family(ns, "research_design") == "agentic-ai/research-design:"
    assert tp.coding_tag(ns, "research_design", "panel") == "agentic-ai/research-design:panel"


# ---------------------------------------------------------------------------
# resolution order
# ---------------------------------------------------------------------------


def _cfg(**attrs) -> types.ModuleType:
    mod = types.ModuleType("screening_config")
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def test_config_supplies_the_prefix():
    assert tp.resolve("", _cfg(TAG_PREFIX="agentic-ai"), "cfg.py") == "agentic-ai/"


def test_override_beats_the_config():
    got = tp.resolve("cli-wins", _cfg(TAG_PREFIX="agentic-ai"), "cfg.py")
    assert got == "cli-wins/"


def test_override_works_without_any_config():
    assert tp.resolve("standalone", None, "cfg.py") == "standalone/"


def test_missing_constant_exits_with_the_fix():
    with pytest.raises(SystemExit) as exc:
        tp.resolve("", _cfg(), "screening_config.py")
    msg = str(exc.value)
    assert "TAG_PREFIX" in msg
    assert "set_tag_prefix.py" in msg


def test_invalid_constant_exits_naming_the_file():
    with pytest.raises(SystemExit) as exc:
        tp.resolve("", _cfg(TAG_PREFIX="Bad Prefix"), "screening_config.py")
    assert "screening_config.py" in str(exc.value)


def test_empty_constant_is_not_a_silent_opt_out():
    """`TAG_PREFIX = ""` used to be the obvious escape hatch; it is not one."""
    with pytest.raises(SystemExit) as exc:
        tp.resolve("", _cfg(TAG_PREFIX=""), "screening_config.py")
    assert "empty" in str(exc.value).lower()


def test_no_config_and_no_override_exits():
    with pytest.raises(SystemExit) as exc:
        tp.resolve("", None, "screening_config.py")
    assert "--tag-prefix" in str(exc.value)


def test_invalid_override_blames_the_flag_not_the_file():
    with pytest.raises(SystemExit) as exc:
        tp.resolve("Bad Prefix", None, "screening_config.py")
    assert "--tag-prefix" in str(exc.value)


def test_add_argument_registers_a_defaulted_flag():
    parser = argparse.ArgumentParser()
    tp.add_argument(parser)
    assert parser.parse_args([]).tag_prefix == ""
    assert parser.parse_args(["--tag-prefix", "x"]).tag_prefix == "x"
