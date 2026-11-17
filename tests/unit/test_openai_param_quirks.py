"""OpenAI provider adapts to models that reject max_tokens / temperature."""
import argparse
from types import SimpleNamespace

import pytest
from core import llm_provider, providers


class _Err(Exception):
    status_code = 400


def _provider(name, behaviour):
    p = llm_provider.OpenAICompatProvider.__new__(llm_provider.OpenAICompatProvider)
    p.spec = providers.require(name)
    p._quirks = {}
    p.request_params = {}
    calls = []

    def create(**kw):
        calls.append(kw)
        err = behaviour(kw)
        if err:
            raise _Err(err)
        msg = SimpleNamespace(content="OK")
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    p.client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    return p, calls


def test_openai_uses_completion_tokens_and_drops_temperature():
    def rule(kw):
        if "temperature" in kw:
            return "Unsupported value: 'temperature' does not support 0"
        return None
    p, calls = _provider("openai", rule)
    assert p.generate("m", "", "hi") == "OK"
    assert "max_completion_tokens" in calls[-1] and "temperature" not in calls[-1]
    assert p._quirks["m"]["temperature"] is False


def test_compatible_endpoint_retries_with_new_name_on_400():
    def rule(kw):
        if "max_tokens" in kw:
            return "Use 'max_completion_tokens' instead of 'max_tokens'"
        return None
    p, calls = _provider("ollama", rule)
    assert p.generate("m", "", "hi") == "OK"
    assert "max_tokens" in calls[0] and "max_completion_tokens" in calls[-1]


def test_provider_override_sets_env_and_rejects_unknown(monkeypatch):
    monkeypatch.setenv(providers.PROVIDER_ENV, "x")
    monkeypatch.delenv(providers.PROVIDER_ENV)  # restored after the test
    parser = argparse.ArgumentParser()
    llm_provider.add_provider_argument(parser)
    llm_provider.apply_provider_override(parser.parse_args([]).provider)
    assert providers.PROVIDER_ENV not in __import__("os").environ
    llm_provider.apply_provider_override(parser.parse_args(["--provider", "openai"]).provider)
    assert llm_provider.resolve_provider("anything").name == "openai"
    with pytest.raises(ValueError):
        llm_provider.apply_provider_override("nope")


def test_parallel_worker_that_sent_temperature_still_retries():
    # Another worker learns the quirk while this call is in flight, so by the
    # time this one's 400 arrives the shared state already says "omit".
    state = {}

    def rule(kw):
        if "temperature" in kw:
            state["p"]._quirks["m"]["temperature"] = False  # learned elsewhere
            return "temperature does not support 0.0"
        return None
    p, calls = _provider("openai", rule)
    state["p"] = p
    assert p.generate("m", "", "hi") == "OK"
    assert "temperature" not in calls[-1]


def test_empty_answer_is_a_per_item_failure_with_diagnostics():
    def create(**kw):
        msg = SimpleNamespace(content=None)
        usage = SimpleNamespace(
            completion_tokens=4000,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=4000))
        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg, finish_reason="length")],
            usage=usage)
    p, _ = _provider("openai", lambda kw: None)
    p.client.chat.completions.create = create
    with pytest.raises(llm_provider.EmptyResponseError) as ei:
        p.generate("m", "", "hi", max_tokens=4000)
    assert "finish_reason=length" in str(ei.value) and "reasoning_tokens=4000" in str(ei.value)
    verdict = llm_provider.classify_failure(ei.value)
    assert verdict.retryable


def test_openai_gets_roomy_cap_and_reasoning_effort():
    p, calls = _provider("openai", lambda kw: None)
    p.request_params = {"reasoning_effort": "low"}
    p.generate("m", "", "hi", max_tokens=4000)
    assert calls[-1]["max_completion_tokens"] == llm_provider.OPENAI_MIN_COMPLETION_CAP
    assert calls[-1]["reasoning_effort"] == "low"
