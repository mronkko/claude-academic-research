"""Unit tests for fulltext_code._merge_fields_into_payload."""
from fulltext_code import _items_for_update_mode, _merge_fields_into_payload


def _make_payload(fields: dict, decision: str = "include") -> dict:
    return {
        "decision": decision,
        "exclusion_code": "",
        "reason": "original reason",
        "model": "claude-sonnet-4-6",
        "prompt_version": "v1-2026-01-01",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "fields": fields,
    }


def test_adds_new_field():
    existing = _make_payload({"key_findings": "old finding", "sample": "old sample"})
    new_row = {"key_findings": "new finding", "sample": "new sample",
               "method": "new method"}
    result = _merge_fields_into_payload(existing, new_row, {"method"})
    assert result["fields"]["key_findings"] == "old finding"
    assert result["fields"]["sample"] == "old sample"
    assert result["fields"]["method"] == "new method"


def test_updates_only_named_fields():
    existing = _make_payload({"key_findings": "original", "method": "old method"})
    new_row = {"key_findings": "llm says something else", "method": "updated method"}
    result = _merge_fields_into_payload(existing, new_row, {"method"})
    assert result["fields"]["key_findings"] == "original"
    assert result["fields"]["method"] == "updated method"


def test_preserves_decision_and_provenance():
    existing = _make_payload({"key_findings": "x"})
    new_row = {"key_findings": "y", "decision": "exclude",
               "reason": "new reason", "model": "new-model"}
    result = _merge_fields_into_payload(existing, new_row, {"key_findings"})
    # Only fields dict is merged; decision/reason/model are NOT touched
    assert result["decision"] == "include"
    assert result["reason"] == "original reason"
    assert result["model"] == "claude-sonnet-4-6"
    assert result["fields"]["key_findings"] == "y"


def test_missing_field_in_new_row_skipped():
    existing = _make_payload({"key_findings": "keep this"})
    new_row = {}  # LLM returned nothing for the target field
    result = _merge_fields_into_payload(existing, new_row, {"key_findings"})
    assert result["fields"]["key_findings"] == "keep this"


def test_does_not_mutate_existing():
    existing = _make_payload({"key_findings": "original"})
    original_fields = dict(existing["fields"])
    _merge_fields_into_payload(existing, {"key_findings": "new"}, {"key_findings"})
    assert existing["fields"] == original_fields


def _make_item(key: str, tags: list) -> dict:
    return {
        "key": key,
        "data": {
            "key": key,
            "tags": [{"tag": t} for t in tags],
        },
    }


def test_update_mode_selects_fulltext_include():
    items = [
        _make_item("A", ["fulltext:include"]),
        _make_item("B", ["fulltext:exclude"]),
        _make_item("C", []),
        _make_item("D", ["fulltext:include", "abstract:include"]),
    ]
    result = _items_for_update_mode(items, only_keys=None)
    assert {it["key"] for it in result} == {"A", "D"}


def test_update_mode_respects_only_keys():
    items = [
        _make_item("A", ["fulltext:include"]),
        _make_item("B", ["fulltext:include"]),
    ]
    result = _items_for_update_mode(items, only_keys={"A"})
    assert [it["key"] for it in result] == ["A"]


def test_reason_prefix_regex_cleaning():
    import re
    def clean(reason):
        return re.sub(r'^(?:\[UPDATE-FIELDS:[^\]]*\]\s*)+', '', reason)

    assert clean("original reason") == "original reason"
    assert clean("[UPDATE-FIELDS:ai_role] original reason") == "original reason"
    assert clean("[UPDATE-FIELDS:ai_role] [UPDATE-FIELDS:another] original reason") == "original reason"
    assert clean("[UPDATE-FIELDS:ai_role]\n[UPDATE-FIELDS:another] original reason") == "original reason"
    assert clean("something else [UPDATE-FIELDS:ai_role]") == "something else [UPDATE-FIELDS:ai_role]"

