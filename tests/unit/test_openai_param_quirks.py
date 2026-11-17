"""OpenAI provider adapts to models that reject max_tokens / temperature."""
from types import SimpleNamespace

from core import llm_provider, providers


class _Err(Exception):
    status_code = 400


def _provider(name, behaviour):
    p = llm_provider.OpenAICompatProvider.__new__(llm_provider.OpenAICompatProvider)
    p.spec = providers.require(name)
    p._quirks = {}
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
