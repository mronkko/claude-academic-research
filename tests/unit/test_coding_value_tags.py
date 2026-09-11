"""Coded categorical values as Zotero tags.

Full-text coding writes its fields into a child note and a CSV. A field
that declares a closed vocabulary can additionally opt in to becoming a
tag, so the corpus can be browsed by design / method / whatever the review
codes for, straight from Zotero's tag selector.

The rules that matter: only a closed vocabulary can be tagged, a value
outside it is recorded but never tagged, and a re-code replaces rather
than accumulates.
"""

from __future__ import annotations

import fulltext_code as fc
import pytest
import tag_prefix

NS = tag_prefix.namespace("test-review")

DESIGN = {
    "name": "research_design",
    "description": "The design.",
    "values": ["experiment", "survey", "panel", "case-study"],
    "tag": True,
}
STRENGTH = {
    "name": "causal_strength",
    "description": "How strong.",
    "values": ["strong", "weak"],
    "tag": True,
}
#: Declares a vocabulary but did not opt in to tagging.
UNTAGGED_CATEGORICAL = {
    "name": "region",
    "description": "Where.",
    "values": ["europe", "asia"],
}
FREE_TEXT = {"name": "key_findings", "description": "What it found."}


# ---------------------------------------------------------------------------
# Which fields are taggable
# ---------------------------------------------------------------------------


def test_only_opted_in_categorical_fields_are_taggable():
    fields = [FREE_TEXT, UNTAGGED_CATEGORICAL, DESIGN]
    assert [f["name"] for f in fc.taggable_fields(fields)] == ["research_design"]


def test_no_taggable_fields_is_not_an_error():
    assert fc.taggable_fields([FREE_TEXT]) == []


# ---------------------------------------------------------------------------
# Building the tag ops
# ---------------------------------------------------------------------------


def test_a_coded_value_becomes_one_namespaced_tag():
    add, remove, rejected = fc.coding_value_tag_ops(
        {"research_design": "panel"}, [DESIGN], NS,
    )
    assert add == [f"{NS}research-design:panel"]
    assert remove == [f"{NS}research-design:"]
    assert rejected == []


def test_the_family_is_cleared_so_a_recode_replaces_rather_than_accumulates():
    """Without `remove_prefixed` an item re-coded from panel to survey would
    end up carrying both, and nothing downstream could say which is current."""
    _, remove, _ = fc.coding_value_tag_ops(
        {"research_design": "survey"}, [DESIGN], NS,
    )
    assert remove == [f"{NS}research-design:"]
    assert f"{NS}research-design:panel".startswith(remove[0])


def test_several_taggable_fields_each_contribute_one_tag():
    add, remove, _ = fc.coding_value_tag_ops(
        {"research_design": "experiment", "causal_strength": "strong"},
        [DESIGN, STRENGTH],
        NS,
    )
    assert add == [
        f"{NS}research-design:experiment",
        f"{NS}causal-strength:strong",
    ]
    assert remove == [f"{NS}research-design:", f"{NS}causal-strength:"]


def test_field_and_value_names_are_slugged_into_tag_shape():
    add, _, _ = fc.coding_value_tag_ops(
        {"research_design": "Case Study"}, [DESIGN], NS,
    )
    assert add == [f"{NS}research-design:case-study"]


def test_an_untagged_categorical_field_writes_nothing():
    add, remove, rejected = fc.coding_value_tag_ops(
        {"region": "europe"}, [UNTAGGED_CATEGORICAL], NS,
    )
    assert (add, remove, rejected) == ([], [], [])


def test_a_free_text_field_writes_nothing():
    add, remove, _ = fc.coding_value_tag_ops(
        {"key_findings": "Growth follows funding."}, [FREE_TEXT], NS,
    )
    assert (add, remove) == ([], [])


# ---------------------------------------------------------------------------
# Values outside the vocabulary
# ---------------------------------------------------------------------------


def test_an_out_of_vocabulary_value_is_reported_not_tagged():
    """The model invented a category. It stays in the note and the CSV —
    but a tag would read as authoritative as a real one."""
    add, remove, rejected = fc.coding_value_tag_ops(
        {"research_design": "ethnography"}, [DESIGN], NS,
    )
    assert add == []
    assert rejected == ["research_design='ethnography'"]
    # The family is still cleared, so a bad re-code cannot leave the
    # previous run's tag standing beside a contradicting note.
    assert remove == [f"{NS}research-design:"]


def test_one_bad_value_does_not_suppress_a_good_one():
    add, _, rejected = fc.coding_value_tag_ops(
        {"research_design": "ethnography", "causal_strength": "weak"},
        [DESIGN, STRENGTH],
        NS,
    )
    assert add == [f"{NS}causal-strength:weak"]
    assert rejected == ["research_design='ethnography'"]


def test_an_empty_value_is_skipped_silently():
    """An exclude leaves coding fields blank; that is not a bad value."""
    add, remove, rejected = fc.coding_value_tag_ops(
        {"research_design": ""}, [DESIGN], NS,
    )
    assert add == []
    assert rejected == []
    assert remove == [f"{NS}research-design:"]


def test_a_missing_field_is_skipped_silently():
    add, _, rejected = fc.coding_value_tag_ops({}, [DESIGN], NS)
    assert add == []
    assert rejected == []


def test_whitespace_and_case_differences_still_match_the_vocabulary():
    add, _, rejected = fc.coding_value_tag_ops(
        {"research_design": "  PANEL  "}, [DESIGN], NS,
    )
    assert add == [f"{NS}research-design:panel"]
    assert rejected == []


def test_a_value_cannot_forge_a_second_family_level():
    add, _, _ = fc.coding_value_tag_ops(
        {"research_design": "panel"}, [DESIGN], NS,
    )
    assert add[0].count(":") == 1
    assert add[0].count("/") == 1


# ---------------------------------------------------------------------------
# Config validation — caught at load, before the run is paid for
# ---------------------------------------------------------------------------


def test_tag_without_values_is_rejected():
    with pytest.raises(SystemExit) as exc:
        fc._validate_coding_field({"name": "notes", "tag": True})
    assert "closed vocabulary" in str(exc.value)


def test_empty_values_list_is_rejected():
    with pytest.raises(SystemExit) as exc:
        fc._validate_coding_field({"name": "d", "values": [], "tag": True})
    assert "non-empty list" in str(exc.value)


def test_non_string_values_are_rejected():
    with pytest.raises(SystemExit):
        fc._validate_coding_field({"name": "d", "values": [1, 2]})


def test_values_that_collide_once_slugged_are_rejected():
    """`Case Study` and `case-study` would share one tag, so an item coded
    either way would be indistinguishable from one coded the other."""
    with pytest.raises(SystemExit) as exc:
        fc._validate_coding_field(
            {"name": "d", "values": ["Case Study", "case-study"], "tag": True},
        )
    assert "collide" in str(exc.value)


def test_a_value_with_no_letters_or_digits_is_rejected():
    with pytest.raises(SystemExit) as exc:
        fc._validate_coding_field({"name": "d", "values": ["???"], "tag": True})
    assert "cannot become a tag" in str(exc.value)


def test_a_plain_free_text_field_validates():
    fc._validate_coding_field(FREE_TEXT)


def test_a_categorical_field_without_tag_validates():
    fc._validate_coding_field(UNTAGGED_CATEGORICAL)


# ---------------------------------------------------------------------------
# The prompt shows the vocabulary
# ---------------------------------------------------------------------------


def test_the_json_schema_lists_the_permitted_values():
    rendered = fc._render_prompt(
        "before\n{coding_fields_json_placeholder}\nafter", [DESIGN],
    )
    assert '"research_design": "experiment" | "survey"' in rendered


def test_free_text_fields_keep_their_open_slot():
    rendered = fc._render_prompt(
        "{coding_fields_json_placeholder}", [FREE_TEXT],
    )
    assert '"key_findings": "<...>"' in rendered


def test_the_field_guide_spells_out_the_closed_vocabulary():
    rendered = fc._render_prompt("{coding_fields_json_placeholder}", [DESIGN])
    assert "Answer with exactly one of:" in rendered
    assert "experiment, survey, panel, case-study" in rendered
