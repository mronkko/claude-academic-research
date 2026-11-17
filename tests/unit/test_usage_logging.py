"""Per-call usage columns, non-breaking CSV migration, prices and summary."""
import csv
from types import SimpleNamespace

import csv_io
import fulltext_code as fc
import log_schemas
import screening_common
from core import llm_provider, models, providers


def _openai(content="OK", effort=None):
    p = llm_provider.OpenAICompatProvider.__new__(llm_provider.OpenAICompatProvider)
    p.spec = providers.require("openai")
    p._quirks = {"m": {"completion_tokens": True, "temperature": False}}
    p.request_params = {"reasoning_effort": effort} if effort else {}

    def create(**kw):
        usage = SimpleNamespace(
            prompt_tokens=1000, completion_tokens=300,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=120))
        msg = SimpleNamespace(content=content)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg, finish_reason="stop")], usage=usage)
    p.client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    return p


def test_openai_records_usage_and_settings():
    p = _openai(effort="low")
    p.generate("m", "", "hi", max_tokens=4000)
    assert p.last_call == {
        "prompt_tokens": 1000, "completion_tokens": 300, "reasoning_tokens": 120,
        "finish_reason": "stop", "max_completion_tokens": 32000,
        "reasoning_effort": "low", "temperature": "omitted"}


def test_usage_recorded_even_when_answer_is_empty():
    p = _openai(content=None)
    try:
        p.generate("m", "", "hi")
    except llm_provider.EmptyResponseError:
        pass
    assert p.last_call["completion_tokens"] == 300


def test_code_one_puts_usage_on_row(monkeypatch):
    p = _openai(content='{"decision": "exclude", "exclusion_code": "FE1", "reason": "r"}')
    monkeypatch.setattr(fc, "build_coding_request",
                        lambda *a, **k: ({"item_key": "K"}, "msg"))
    row = fc._code_one({}, None, p, "m", "sys", [])
    assert row["decision"] == "exclude" and row["prompt_tokens"] == 1000
    assert row["reasoning_effort"] == "default"


def test_usage_columns_are_appended_after_legacy_columns():
    legacy = log_schemas.FULLTEXT_BASE_FIELDS + ["model", "prompt_version"]
    new = log_schemas.fulltext_screening_fields([])
    assert new[:len(legacy)] == legacy
    assert new[len(legacy):] == log_schemas.FULLTEXT_USAGE_FIELDS


def test_legacy_log_migrates_on_next_write(tmp_path):
    legacy = log_schemas.FULLTEXT_BASE_FIELDS + ["key_findings", "model", "prompt_version"]
    path = tmp_path / "log.csv"
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=legacy)
        w.writeheader()
        w.writerow({"item_key": "OLD", "decision": "include", "model": "m"})
    schema = log_schemas.fulltext_screening_fields(["key_findings"])
    csv_io.upsert_by_item_key(path, {"item_key": "NEW", "prompt_tokens": 5}, schema)
    rows = list(csv.DictReader(path.open()))
    assert [r["item_key"] for r in rows] == ["OLD", "NEW"]
    assert rows[0]["decision"] == "include" and rows[0]["prompt_tokens"] == ""
    assert rows[1]["prompt_tokens"] == "5"


def test_model_prices_lookup():
    mod = SimpleNamespace(MODEL_PRICES={"a": (1, 2)},
                          PRICE_PER_MTOK_INPUT=0.1, PRICE_PER_MTOK_OUTPUT=0.5)
    assert screening_common.model_prices(mod, "a") == (1.0, 2.0)
    assert screening_common.model_prices(mod, "b") == (0.1, 0.5)
    assert screening_common.model_prices(SimpleNamespace(), "b") is None


def test_usage_summary_computes_cost_only_with_prices():
    rows = [{"prompt_tokens": 1_000_000, "completion_tokens": 200_000,
             "reasoning_tokens": 50_000}, {"prompt_tokens": 1_000_000,
                                           "completion_tokens": 0}]
    priced = SimpleNamespace(PRICE_PER_MTOK_INPUT=0.10, PRICE_PER_MTOK_OUTPUT=0.50)
    out = fc.usage_summary("x", rows, "m", priced)
    assert "2,000,000 prompt, 200,000 completion" in out
    assert "Cost: $0.3000" in out
    assert "not computed" in fc.usage_summary("x", rows, "m", SimpleNamespace())
    assert fc.usage_summary("x", [{}], "m", priced) == ""


def test_dry_run_estimate_uses_configured_prices():
    line = models.cost_estimate_line(
        "m", stage="fulltext_coding", n_items=10, provider="openai",
        prices=(0.10, 0.50))
    assert "your configured $0.1/M input" in line
