"""Concurrent browser lanes: the ceiling rule and the shared state.

A "lane" is one Playwright page driving one handler instance. All lanes
for a publisher share a single persistent BrowserContext, because the
profile directory holds the Cloudflare clearance and the institutional
SSO session and Chromium locks that directory — a second browser would
need every one of those logins solved again.

That sharing is what these tests are about. Three facts were plain
locals in the serial loop and have to be shared once lanes exist: the
user's answer to the Option-4 prompt, the outage breaker's count, and
whether the prompt has already fired. Getting any of them wrong is
silent: the run keeps going and simply does the wrong thing to a few
hundred items.
"""

from __future__ import annotations

import asyncio

import pytest
from enrich_pdfs import LaneCoordinator, effective_lanes


class _Handler:
    def __init__(self, concurrency: int = 1, attaches_directly: bool = False):
        self.concurrency = concurrency
        self.attaches_directly = attaches_directly


# --- effective_lanes: two ceilings, the smaller wins -------------------


def test_the_flag_bounds_a_tolerant_publisher() -> None:
    assert effective_lanes(_Handler(concurrency=4), 2) == 2


def test_the_publisher_bounds_an_ambitious_flag() -> None:
    """`concurrency` records what a live run established about a
    platform. `--browser-workers` must not be able to overrule it, or
    the flag becomes a way to get quietly rate-limited."""
    assert effective_lanes(_Handler(concurrency=1), 10) == 1
    assert effective_lanes(_Handler(concurrency=4), 10) == 4


def test_the_connector_is_pinned_to_one_lane() -> None:
    """There is one Zotero desktop and one translator, and a human
    confirms each new host. No number from either side changes that."""
    connector = _Handler(concurrency=8, attaches_directly=True)

    assert effective_lanes(connector, 10) == 1


@pytest.mark.parametrize("requested", [0, -3, None])
def test_a_nonsense_request_still_yields_a_working_lane(requested) -> None:
    assert effective_lanes(_Handler(concurrency=4), requested) == 1


def test_a_handler_declaring_nothing_defaults_to_serial() -> None:
    class Bare:
        pass

    assert effective_lanes(Bare(), 10) == 1


def test_every_shipped_handler_declares_a_sane_ceiling() -> None:
    """Guards the seam the other direction: a new handler that forgets
    `concurrency`, or sets it to 0, would otherwise be discovered by a
    live run rather than by CI."""
    playwright = pytest.importorskip(
        "playwright", reason="handler registry imports playwright types",
    )
    del playwright
    from fetchers.browser import all_handlers

    for handler in all_handlers():
        lanes = effective_lanes(handler, 10)
        assert lanes >= 1, f"{handler.name} resolved to {lanes} lanes"


# --- LaneCoordinator: the outage breaker ------------------------------


def test_the_breaker_trips_on_a_run_of_transport_failures() -> None:
    coord = LaneCoordinator(outage_threshold=3)

    assert coord.note_transport_failure() is False
    assert coord.note_transport_failure() is False
    assert coord.note_transport_failure() is True


def test_any_other_outcome_clears_the_breaker() -> None:
    """A flaky link that drops one request in ten must never trip it —
    only a genuine run of them, which is what "the machine is offline"
    looks like from here."""
    coord = LaneCoordinator(outage_threshold=3)
    coord.note_transport_failure()
    coord.note_transport_failure()

    coord.note_other_outcome()

    assert coord.note_transport_failure() is False


def test_the_count_is_shared_across_lanes() -> None:
    """Deliberate: "consecutive" stops being literal under concurrency,
    but the fact it detects was never per-lane. N lanes reaching the
    threshold N times sooner is the direction you want when the
    alternative is shredding the queue at a second an item."""
    coord = LaneCoordinator(outage_threshold=3)

    # Three different lanes, one failure each.
    assert [coord.note_transport_failure() for _ in range(3)] == [
        False, False, True,
    ]


# --- LaneCoordinator: the prompt --------------------------------------


def test_only_one_lane_may_claim_the_prompt() -> None:
    """N simultaneous failures must ask the human once, not N times."""
    coord = LaneCoordinator()

    assert [coord.claim_prompt() for _ in range(4)] == [
        True, False, False, False,
    ]


def test_lanes_park_at_the_gate_while_a_prompt_is_open() -> None:
    """The piece with no serial counterpart.

    Without the gate, an answer of "skip the rest" arrives after the
    other lanes have already opened pages against a publisher the user
    just declined — the exact waste the Option-4 prompt exists to
    prevent, reintroduced by concurrency.
    """
    async def scenario() -> list[str]:
        coord = LaneCoordinator()
        order: list[str] = []

        async def waiter() -> None:
            await coord.wait_until_open()
            order.append("lane resumed")

        async def prompter() -> None:
            async with coord.prompting():
                await asyncio.sleep(0.02)      # the human, thinking
                order.append("answer given")

        await asyncio.gather(prompter(), waiter())
        return order

    assert asyncio.run(scenario()) == ["answer given", "lane resumed"]


def test_the_gate_reopens_even_when_the_prompt_raises() -> None:
    """A prompt that blows up must not strand every other lane."""
    async def scenario() -> bool:
        coord = LaneCoordinator()
        with pytest.raises(RuntimeError):
            async with coord.prompting():
                raise RuntimeError("channel died")
        # Would hang here if the gate had stayed shut.
        await asyncio.wait_for(coord.wait_until_open(), timeout=0.5)
        return True

    assert asyncio.run(scenario()) is True


def test_the_gate_is_open_before_anyone_prompts() -> None:
    async def scenario() -> bool:
        coord = LaneCoordinator()
        await asyncio.wait_for(coord.wait_until_open(), timeout=0.5)
        return True

    assert asyncio.run(scenario()) is True


def test_a_run_starts_with_no_outage_and_no_skip() -> None:
    coord = LaneCoordinator()

    assert coord.outage is None
    assert coord.skip_remaining is False
    assert coord.prompt_fired is False
