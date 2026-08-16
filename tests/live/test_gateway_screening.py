"""Live checks against an institutional OpenAI-compatible gateway.

Opt in with `pytest -m live`. Everything is read from config or the
environment, so this file names no institution, no hostname, and no
model — point it at whichever gateway you have.

    [gateway]
    base_url = "https://llm.example.edu/api"
    api_key_env = "MY_LLM_KEY"          # or api_key = "..."
    test_model  = "org/model-id"        # which model these tests may call

**`test_model` must be named explicitly; there is no fallback to "the
first model listed".** A gateway that loads weights on demand answers
`503 Model not available yet, try again in a few minutes` for anything
not already resident, and picking a model by list position is a coin
flip on whether the suite spends a minute cold-starting one — or goes
red for a reason that has nothing to do with this plugin. Naming the
model is the only way the caller can guarantee a warm one.

What this covers that the unit suite cannot: the listing's real
envelope shape, whether the credential is actually accepted, and
whether the OpenAI SDK path returns a response the screening parser can
read. Each of those has already been wrong once against a real gateway.
"""

from __future__ import annotations

import pytest
from core import llm_provider, model_discovery, model_health, providers

from tests.live.conftest import require_config

pytestmark = pytest.mark.live

GATEWAY = providers.require("gateway")


def _endpoint() -> tuple[str, str]:
    """`(api_key, base_url)`, skipping when the gateway is not set up."""
    base = require_config("gateway", "base_url")
    return llm_provider._api_key_for(GATEWAY, required=False), base


def _test_model() -> str:
    model = require_config("gateway", "test_model")
    if not model:  # pragma: no cover - require_config skips first
        pytest.skip("set [gateway] test_model to a model your gateway keeps warm")
    return model


def test_the_gateway_lists_its_models() -> None:
    """Whatever envelope it uses, `_normalise` must flatten it.

    A real gateway returned a bare JSON array rather than OpenAI's
    `{"data": [...]}`, and `payload.get("data")` raised AttributeError —
    discovery crashed instead of degrading. That is the regression this
    guards, and it can only be caught against a live endpoint.
    """
    api_key, base = _endpoint()
    try:
        models = model_discovery.list_models(GATEWAY, api_key, base)
    except model_discovery.DiscoveryError as e:
        pytest.skip(f"gateway not reachable ({e}); VPN down?")
    assert models, "the gateway listed no models"
    assert all(m.id for m in models), "a listed model has no id"


def test_every_listed_model_gets_a_tier() -> None:
    """`tier_of` is advisory, so an unplaceable ID is not a failure — but
    a listing where most rows read `?` makes the column worthless. Six of
    eight were unplaceable on the first real gateway this met.

    Reported rather than asserted: a gateway may legitimately serve
    something with no size in its name, and this suite must not go red
    because an institution added a model with an unusual ID.
    """
    api_key, base = _endpoint()
    try:
        models = model_discovery.list_models(GATEWAY, api_key, base)
    except model_discovery.DiscoveryError as e:
        pytest.skip(f"gateway not reachable ({e})")
    unplaced = [m.id for m in models if not providers.tier_of(GATEWAY, m.id)]
    if unplaced:
        pytest.skip(
            f"{len(unplaced)} of {len(models)} models have no inferred tier: "
            f"{unplaced}. Not a failure — consider widening the gateway's "
            f"tier_hints in scripts/core/providers.py."
        )


def test_the_named_test_model_answers() -> None:
    api_key, base = _endpoint()
    model = _test_model()
    result = model_health.check_connection(GATEWAY, model, api_key, base)
    if not result.ok and result.http_status == 503:
        pytest.fail(
            f"{model} is not resident on the gateway — it answered 503 "
            f"'{result.detail}'. Point [gateway] test_model at a model "
            f"your gateway keeps warm; do not let the suite cold-start one."
        )
    assert result.ok, result.format()


def test_a_screening_prompt_comes_back_parseable() -> None:
    """The whole path the pipeline uses: SDK client, real credential,
    real model, and a response `abstract_screen.py` can parse.

    Asserts the *shape* only. Whether an open-weight model reaches the
    same verdict as Claude on a given abstract is a research question,
    not a test.
    """
    _endpoint()
    model = _test_model()
    client = llm_provider.get_provider(model)
    text = client.generate(
        model=model,
        max_tokens=200,
        temperature=0.0,
        system=(
            "You are a systematic review screener. Respond with EXACTLY "
            "two lines:\nDECISION: include|borderline|exclude\n"
            "REASON: <one sentence>"
        ),
        prompt=(
            "TITLE: Entrepreneurial self-efficacy and venture growth\n\n"
            "ABSTRACT: We survey 1,243 nascent entrepreneurs and estimate "
            "the effect of self-efficacy on three-year revenue growth.\n\n"
            "JOURNAL: Journal of Business Venturing"
        ),
    )
    assert text.strip(), "the gateway returned an empty completion"

    decision = ""
    for line in text.splitlines():
        if line.upper().startswith("DECISION:"):
            decision = line.split(":", 1)[1].strip().lower()
    assert decision in ("include", "borderline", "exclude"), (
        f"no parseable DECISION line in the response:\n{text[:400]}"
    )
