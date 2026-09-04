"""The `gateway` provider, and what happens before it is configured.

An institutional gateway is the one provider the plugin ships no address
for. Every other entry in the registry has a `default_base_url` that is
right for almost everybody; this one cannot, because "your university's
LLM endpoint" has no guessable value and inventing one would send a
user's abstracts to a host they did not choose.

That makes "configured provider, empty base URL" a *normal* state rather
than an exotic one — it is where every gateway user starts. These tests
pin what the plugin says in that state. Before `byo_endpoint` existed,
the answer was `"/v1/models"` handed to `urllib`, which raises
`ValueError("unknown url type")`, which `_get_json` catches and retries
three times over roughly eight seconds before reporting a message
naming neither the cause nor the fix.

The other half of the file pins the cost story. A gateway usually costs
the researcher nothing, but the plugin does not know that — an
institution may recharge internally — so it must say *unknown* and not
*free*. "Free" is the one wrong answer that cannot be walked back after
someone has run 5,000 abstracts.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from core import llm_provider, model_discovery, model_health, models, providers

GATEWAY = providers.require("gateway")
SCRIPTS_ROOT = Path(__file__).resolve().parents[2] / "scripts"


def _load_setup_script(name: str):
    """Import a `scripts/setup/` script by path, as the suite does for
    the wizard. They are stdlib-only and run under a bare python3, so
    they are not importable as a package."""
    spec = importlib.util.spec_from_file_location(
        name, SCRIPTS_ROOT / "setup" / f"{name}.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# The spec itself
# ---------------------------------------------------------------------------


def test_the_gateway_ships_no_hostname() -> None:
    """The whole point of the provider: the address is the user's to give."""
    assert GATEWAY.default_base_url == ""
    assert GATEWAY.byo_endpoint is True
    assert GATEWAY.local is False
    # And no environment variable either. Every other provider's is an
    # ecosystem convention its own SDK reads; a gateway has none, so an
    # invented name would just collide with the one the user already
    # exports (UNI_LLM_TOKEN, MY_LLM_KEY, …).
    assert GATEWAY.api_key_env == ""
    assert GATEWAY.base_url_env == ""


def test_the_gateway_reuses_the_openai_transport() -> None:
    """No new transport: a gateway is OpenAI on the wire, and adding a
    fourth client class for an identical protocol would be a lie in the
    data that every consumer then has to special-case."""
    assert GATEWAY.transport == "openai_compat"


@pytest.mark.parametrize(
    ("model_id", "tier"),
    [
        # Open-weight IDs carry a parameter count where a vendor ID
        # carries a tier word.
        ("meta-llama/Llama-3.1-8B-Instruct", "fast"),
        ("Qwen/Qwen3-32B", "balanced"),
        ("meta-llama/Llama-3.3-70B-Instruct", "deep"),
    ],
)
def test_tier_hints_read_parameter_counts(model_id, tier) -> None:
    assert providers.tier_of(GATEWAY, model_id) == tier


#: The `/v1/models` payload from a real institutional vLLM gateway,
#: recorded 2026-08-16. Two things about it broke the first pass: it is a
#: **bare array** with no `{"data": …}` envelope, and six of its eight IDs
#: were unclassifiable by the tier hints as first written.
LIVE_GATEWAY_LISTING = [
    {"id": "Qwen/Qwen3-30B-A3B-Instruct-2507-FP8", "object": "model"},
    {"id": "Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8", "object": "model"},
    {"id": "Qwen/Qwen3-VL-30B-A3B-Instruct-FP8", "object": "model"},
    {"id": "Qwen/Qwen3-VL-30B-A3B-Thinking-FP8", "object": "model"},
    {"id": "RedHatAI/gemma-4-31B-it-FP8-Dynamic", "object": "model"},
    {"id": "google/codegemma-7b-it", "object": "model"},
    {"id": "google/gemma-4-E4B-it", "object": "model"},
    {"id": "openai/gpt-oss-120b", "object": "model"},
]


def test_a_bare_array_listing_is_accepted() -> None:
    """Not every OpenAI-compatible server wraps its listing.

    `payload.get("data")` raised `AttributeError: 'list' object has no
    attribute 'get'` against a real gateway — a crash, so discovery
    failed outright rather than degrading. Both shapes must work.
    """
    models = model_discovery._normalise(GATEWAY, LIVE_GATEWAY_LISTING)
    assert [m.id for m in models] == [m["id"] for m in LIVE_GATEWAY_LISTING]


def test_a_malformed_listing_entry_is_skipped_not_fatal() -> None:
    models = model_discovery._normalise(
        GATEWAY, ["not-a-dict", {"id": "real/model"}, {}],
    )
    assert [m.id for m in models] == ["real/model"]


def test_every_model_a_real_gateway_serves_gets_a_tier() -> None:
    """`?` is honest but useless in bulk.

    The hints are advisory and nothing selects from them, so an
    unplaceable ID is not a failure — but six of eight unplaceable makes
    the listing's `tier?` column worthless, which is the one thing it is
    for. This is the regression test on that.
    """
    unplaced = [
        m["id"] for m in LIVE_GATEWAY_LISTING
        if not providers.tier_of(GATEWAY, m["id"])
    ]
    assert not unplaced, f"no tier inferred for: {unplaced}"


def test_the_biggest_model_a_real_gateway_serves_is_the_deep_one() -> None:
    """Ordering sanity: 120B must outrank the 30Bs, and a 7B must not
    land in the same tier as either."""
    assert providers.tier_of(GATEWAY, "openai/gpt-oss-120b") == "deep"
    assert providers.tier_of(
        GATEWAY, "Qwen/Qwen3-30B-A3B-Instruct-2507-FP8",
    ) == "balanced"
    assert providers.tier_of(GATEWAY, "google/codegemma-7b-it") == "fast"


def test_a_leading_hyphen_stops_8b_matching_128b() -> None:
    """`lmstudio` declares a bare "8b", so "gemma-128b" classifies as
    fast. The gateway's hints are hyphen-anchored to avoid inheriting
    that bug — this is the case that catches a careless edit."""
    assert providers.tier_of(GATEWAY, "some-model-128b") != "fast"


# ---------------------------------------------------------------------------
# Before an endpoint is configured
# ---------------------------------------------------------------------------


def test_listing_models_names_where_the_endpoint_belongs() -> None:
    with pytest.raises(model_discovery.DiscoveryError) as excinfo:
        model_discovery.list_models(GATEWAY, api_key="k", base_url="")
    assert "[gateway].base_url" in str(excinfo.value)


def test_listing_models_does_not_retry_a_missing_endpoint() -> None:
    """It must fail on the first attempt. A missing URL is not transient,
    and the retry ladder turned a typo into an eight-second wait."""
    calls: list[str] = []

    original = model_discovery._get_json

    def counting(url, headers):  # pragma: no cover - must not be reached
        calls.append(url)
        return original(url, headers)

    model_discovery._get_json = counting
    try:
        with pytest.raises(model_discovery.DiscoveryError):
            model_discovery.list_models(GATEWAY, api_key="k", base_url="")
    finally:
        model_discovery._get_json = original
    assert calls == [], "no HTTP should be attempted without an endpoint"


def test_the_health_probe_reports_unreachable_and_says_why() -> None:
    result = model_health.check_connection(GATEWAY, "Qwen/Qwen3-32B", "k", "")
    assert result.status is model_health.ConnectionStatus.UNREACHABLE
    assert not result.ok
    assert "[gateway].base_url" in result.detail


def test_the_missing_endpoint_outranks_the_missing_pin() -> None:
    """With neither configured, name the endpoint: you cannot list models
    to pin one until there is somewhere to list them from."""
    result = model_health.check_connection(GATEWAY, "", "k", "")
    assert result.status is model_health.ConnectionStatus.UNREACHABLE


@pytest.mark.allow_network
def test_a_configured_endpoint_still_probes_normally() -> None:
    """The guard must not swallow the real path — an unroutable host
    should reach the network layer and come back UNREACHABLE from there,
    with the host in the detail rather than the env var name.

    Opts out of the unit-suite network block (see tests/unit/conftest.py):
    reaching the socket layer is the point of this test. Port 9 is the
    discard port, so it fails immediately and depends on nothing.
    """
    result = model_health.check_connection(
        GATEWAY, "m", "k", "http://127.0.0.1:9",
    )
    assert result.status is model_health.ConnectionStatus.UNREACHABLE
    assert "[gateway].base_url" not in result.detail


def test_the_client_refuses_to_build_without_an_endpoint(monkeypatch) -> None:
    """Otherwise the SDK is handed base_url="/v1" and the failure
    surfaces per item, mid-run, as an opaque connection error."""
    monkeypatch.setattr(llm_provider, "base_url_for", lambda _spec: "")
    monkeypatch.setattr(llm_provider, "_api_key_for", lambda *_a, **_kw: "k")
    with pytest.raises(RuntimeError) as excinfo:
        llm_provider.OpenAICompatProvider(GATEWAY)
    assert "[gateway].base_url" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


def test_the_cost_estimate_says_unknown_not_free() -> None:
    line = models.cost_estimate_line(
        "Qwen/Qwen3-32B", stage="fulltext_coding", n_items=500,
        provider="gateway",
    )
    assert "unknown" in line
    assert "$0" not in line
    assert "own machine" not in line, (
        "that is the `local` wording; a gateway runs on someone else's "
        "hardware and may well be recharged internally"
    )


def test_the_catalogue_prices_nothing_for_a_gateway() -> None:
    assert model_discovery.catalog_prices("gateway", "balanced") == (0.0, 0.0)
    assert model_discovery.catalog_model("gateway", "balanced") == ""


# ---------------------------------------------------------------------------
# What the status surfaces say
#
# These caught two real bugs that every other test missed, both from the
# same wrong assumption — that `api_key_env == ""` means "needs no
# credential". It does for a local server; for a gateway it means "the
# plugin cannot name the variable". The status block cheerfully reported
# `credential: not required (local provider)` for a remote authenticated
# endpoint, and the provider menu rendered "needs  and ".
# ---------------------------------------------------------------------------


def test_the_status_block_does_not_call_a_gateway_credential_optional(
    monkeypatch,
) -> None:
    check = _load_setup_script("check_llm_provider")
    monkeypatch.setattr(llm_provider, "get", lambda *_a, **_kw: "")
    lines = "\n".join(check.status_lines(GATEWAY, selected=True))
    assert "not required" not in lines, (
        "a gateway is remote and authenticated; 'not required' is the "
        "local-provider wording and is simply false here"
    )
    assert "local: no" in lines
    assert "[gateway].api_key" in lines
    assert "[gateway].base_url" in lines


def test_the_provider_menu_names_something_for_every_provider() -> None:
    """No entry may render an empty requirement.

    The menu used to interpolate `spec.api_key_env` unconditionally,
    which for a config-only provider printed "needs  and ".
    """
    setter = _load_setup_script("set_llm_provider")
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        setter._print_choices()
    out = buf.getvalue()
    assert "needs  " not in out and "needs \n" not in out
    for spec in providers.PROVIDERS:
        assert spec.name in out
    assert "config.toml [gateway]" in out


# ---------------------------------------------------------------------------
# check_model_connection.py — which complaint comes first
# ---------------------------------------------------------------------------


def test_the_connection_check_names_the_endpoint_before_the_pin(
    monkeypatch, capsys, tmp_path,
) -> None:
    """With neither an endpoint nor a pinned model, report the endpoint.

    The missing-pin branch advises running `resolve_models.py --list`,
    which cannot work without somewhere to list from — so reporting it
    first sends the user down a path that dead-ends.
    """
    check = _load_setup_script("check_model_connection")
    monkeypatch.setenv("ACADEMIC_RESEARCH_PROVIDER", "gateway")
    monkeypatch.setattr(
        sys, "argv",
        ["check_model_connection", "--config", str(tmp_path / "absent.py")],
    )

    assert check.main() == 2
    err = capsys.readouterr().err
    assert "[gateway].base_url" in err
    assert "no pinned model" not in err
