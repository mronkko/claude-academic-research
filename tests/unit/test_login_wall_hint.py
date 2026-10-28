"""`enrich_pdfs` says so when every queued route needs a sign-in.

A run whose only routes sit behind an SSO-walled EZproxy used to degrade
silently: the queue stalled at, or deferred on, a login page, and the
reason — no IP-authenticating library in `[library] openurl_base` — took
a whole session to find (BACKLOG, EBSCO chooser findings, item 5).
"""

from __future__ import annotations

from types import SimpleNamespace

import enrich_pdfs
from fetchers.resolvers.base import needs_interactive_login

WALLED = "https://research-ebsco-com.ezproxy.jyu.fi/linkprocessor/plink?x=1"
OPEN = "https://research-ebsco-com.libproxy.aalto.fi/linkprocessor/plink?x=1"


def _handler():
    return SimpleNamespace(
        name="ebsco", display_name="EBSCOhost",
        proxied_route_count=lambda items: sum(
            1 for it in items
            if needs_interactive_login(it.get("resolver_target_url", ""))
        ),
    )


def test_hint_when_every_route_needs_a_sign_in() -> None:
    items = [{"resolver_target_url": WALLED}] * 3
    hint = enrich_pdfs._all_routes_need_login_hint(_handler(), items)
    assert "every EBSCOhost route in this queue (3)" in hint
    assert "[library] openurl_base" in hint


def test_no_hint_when_one_route_needs_no_sign_in() -> None:
    items = [{"resolver_target_url": WALLED}, {"resolver_target_url": OPEN}]
    assert enrich_pdfs._all_routes_need_login_hint(_handler(), items) == ""


def test_no_hint_for_an_empty_queue_or_a_handler_that_cannot_tell() -> None:
    assert enrich_pdfs._all_routes_need_login_hint(_handler(), []) == ""
    plain = SimpleNamespace(name="sage", display_name="Sage")
    items = [{"resolver_target_url": WALLED}]
    assert enrich_pdfs._all_routes_need_login_hint(plain, items) == ""
