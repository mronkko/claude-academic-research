"""Which queues need a human to sign in before the lanes open.

`EbscoHandler.needs_interactive_solve = False` was measured against a
library whose EBSCO route authenticates on institutional IP. `4cead93`
then merged a second library's routes into the same target list, and
those arrive EZproxy-wrapped and land on a SAML IdP. Because `a8b3d8f`
had just made the `False` genuinely skip `setup()`, no login ever
happened for them.

Live consequence with `--browser-workers 4`: all four lanes opened cold
and hit the IdP simultaneously *on the same SAML execution token*, which
no human can clear — each tab invalidates the others'. 8 of 14 items
died at `login.jyu.fi`; serial hit the same wall one item at a time.

Both directions are load-bearing. A needless prompt stalls an unattended
run until the control-file timeout, which is the regression `a8b3d8f`
fixed and must not come back for IP-authenticated routes.
"""

from __future__ import annotations

from fetchers.browser.ebsco import EbscoHandler
from fetchers.resolvers.base import needs_interactive_login

# Real shapes seen live this session.
JYU_REWRITTEN = "https://research-ebsco-com.ezproxy.jyu.fi/c/x3kxfd/search/details/o735"
JYU_WRAPPER = "https://ezproxy.jyu.fi/login?url=https://research.ebsco.com/c/abc"
AALTO_OCLC = "https://login.aalto-libproxy.idm.oclc.org/login?qurl=https://research.ebsco.com/x"
ALMA_DIRECT = "https://eu03.alma.exlibrisgroup.com/view/uresolver/358AALTO_INST/openurl?x=1"
EBSCO_PLAIN = "https://openurl.ebsco.com/linksvc/linking.aspx?sid=Pri"


def _item(url: str) -> dict:
    return {"item_key": "K1", "doi": "10.1/x", "resolver_target_url": url}


class TestProxyDetection:
    def test_hostname_rewriting_form_is_detected(self) -> None:
        assert needs_interactive_login(JYU_REWRITTEN) is True

    def test_wrapper_form_is_detected(self) -> None:
        """`effective_host` unwraps `?url=` and would hide the proxy."""
        assert needs_interactive_login(JYU_WRAPPER) is True

    def test_ip_authenticated_oclc_proxy_is_not_flagged(self) -> None:
        """The regression guard in the other direction.

        Aalto's route is also an EZproxy deployment, but authenticates
        silently on the institutional IP range. Prompting for it would
        re-break unattended runs.
        """
        assert needs_interactive_login(AALTO_OCLC) is False

    def test_plain_routes_are_not_flagged(self) -> None:
        assert needs_interactive_login(ALMA_DIRECT) is False
        assert needs_interactive_login(EBSCO_PLAIN) is False

    def test_matching_is_label_exact_not_substring(self) -> None:
        """A substring test would drag in unrelated hosts."""
        assert needs_interactive_login("https://notezproxying.example.com/x") is False
        assert needs_interactive_login("https://ezproxyfoo.example.com/x") is False

    def test_missing_or_malformed_urls_are_not_flagged(self) -> None:
        assert needs_interactive_login("") is False
        assert needs_interactive_login("not a url") is False


class TestEbscoQueueDecision:
    def test_a_proxied_route_anywhere_in_the_queue_needs_a_solve(self) -> None:
        items = [_item(EBSCO_PLAIN), _item(ALMA_DIRECT), _item(JYU_REWRITTEN)]
        assert EbscoHandler().needs_solve_for(items) is True

    def test_an_ip_authenticated_queue_stays_unattended(self) -> None:
        items = [_item(EBSCO_PLAIN), _item(ALMA_DIRECT), _item(AALTO_OCLC)]
        assert EbscoHandler().needs_solve_for(items) is False

    def test_an_empty_queue_needs_nothing(self) -> None:
        assert EbscoHandler().needs_solve_for([]) is False

    def test_the_static_flag_alone_would_have_said_no(self) -> None:
        """Documents why the hook exists at all."""
        assert EbscoHandler().needs_interactive_solve is False
        assert EbscoHandler().needs_solve_for([_item(JYU_REWRITTEN)]) is True


class TestSolveTarget:
    def test_solve_opens_the_proxied_route_not_the_first_item(self) -> None:
        """doi.org lands on the publisher, where there is no login."""
        items = [_item(EBSCO_PLAIN), _item(JYU_REWRITTEN)]
        assert EbscoHandler().solve_url_for(items) == JYU_REWRITTEN

    def test_setup_url_uses_the_stashed_route(self) -> None:
        h = EbscoHandler()
        h.pending_solve_url = JYU_REWRITTEN
        assert h.setup_url_for("10.1/x") == JYU_REWRITTEN

    def test_setup_url_falls_back_to_the_doi(self) -> None:
        assert EbscoHandler().setup_url_for("10.1/x") == "https://doi.org/10.1/x"

    def test_solve_hosts_name_the_institution_to_sign_in_to(self) -> None:
        """With two libraries configured, "sign in" is ambiguous."""
        items = [_item(EBSCO_PLAIN), _item(JYU_REWRITTEN), _item(JYU_WRAPPER)]
        assert EbscoHandler().solve_hosts_for(items) == [
            "ezproxy.jyu.fi", "research-ebsco-com.ezproxy.jyu.fi",
        ]

    def test_no_hosts_when_nothing_is_proxied(self) -> None:
        assert EbscoHandler().solve_hosts_for([_item(ALMA_DIRECT)]) == []


def test_base_handlers_keep_their_declared_answer() -> None:
    """The hook must not change any handler whose route is fixed."""
    from fetchers.browser import all_handlers

    for h in all_handlers():
        assert h.needs_solve_for([]) is h.needs_interactive_solve
