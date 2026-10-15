"""Which site an automated download comes from, and the per-run gate that
asks (or does not ask) whether the user consents to automating it.

The stored answers and the "a missing answer never means yes" rule live
in `core/publisher_consent.py`; this module is the browser-pass side:

- **Site keys.** A browser handler's site is its `name` (`sage`, `ebsco`).
  A Zotero Connector save is keyed by the platform the page belongs to
  (`site_for_target`): the resolver route's platform where it is one we
  know (`resolvers.base.PLATFORM_PRIORITY` — the same keys as the
  handlers, plus `jstor`, `proquest`, `sciencedirect`; EBSCOhost is
  `ebsco` either way), else the registrable domain of the host the page
  is on, with EZproxy unwrapped in both its forms. A doi.org fail-open
  target is keyed by the landing host Crossref reported, when cached;
  with nothing known it is `doi.org` — one answer for "whatever site a
  DOI link lands on", which a user may reasonably refuse to give.
- **Terms links.** Each handler carries `terms_url`; platforms only the
  Connector reaches carry theirs in `CONNECTOR_ONLY_SITES` below. Never a
  summary of the terms — the link, or the site's home page when no link
  has been verified.
- **`ConsentGate`.** One per run. Asks at most once per site — the
  question is about the site, never per item — and remembers the run's
  answer, so the Connector pass reuses what the handler pass was told.

Stdlib only, like the rest of this package's import surface: the planner
and `pdf_consent.py` import it without Playwright.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from core import publisher_consent as pc

MODE_BROWSER = "browser handler"
MODE_CONNECTOR = "Zotero Connector"

#: Run-time decisions (`ConsentGate.decide`).
ALLOW = "allow"
DENY = "deny"
NEEDED = "needed"

#: Platforms the Connector saves from that have no handler of their own:
#: site key → (display name, terms URL or "" when none is verified).
CONNECTOR_ONLY_SITES: dict[str, tuple[str, str]] = {
    "jstor": ("JSTOR", "https://about.jstor.org/terms/"),
    # TODO(consent terms): no verified URL (candidate:
    # https://about.proquest.com/en/about/terms-and-conditions/).
    "proquest": ("ProQuest", ""),
    # Elsevier's site terms for ScienceDirect pages (the Connector path,
    # not the TDM API). Checked 2026-10-15: HTTP 200.
    "sciencedirect": (
        "ScienceDirect (Elsevier)",
        "https://www.elsevier.com/legal/elsevier-website-terms-and-conditions",
    ),
}

#: `PLATFORM_PRIORITY` keys whose consent site is spelled differently.
_PLATFORM_SITE = {"ebscohost": "ebsco"}

_HOMEPAGES = {
    "jstor": "https://www.jstor.org/",
    "proquest": "https://www.proquest.com/",
    "sciencedirect": "https://www.sciencedirect.com/",
}

#: Second-level labels under which a registrable domain has three labels
#: (`ox.ac.uk`, `example.co.jp`).
_SECOND_LEVEL = frozenset({"ac", "co", "com", "edu", "gov", "net", "org"})

_DOI_HOSTS = frozenset({"doi.org", "dx.doi.org", "www.doi.org"})


@dataclass(frozen=True)
class SiteInfo:
    site: str
    display: str
    terms_url: str = ""
    homepage: str = ""


def _handlers() -> list:
    from . import EbscoHandler, all_handlers
    return [*all_handlers(), EbscoHandler()]


def site_info(site: str) -> SiteInfo:
    """Display name, terms URL and home page for a consent site key."""
    for h in _handlers():
        if h.name == site:
            domains = tuple(getattr(h, "direct_access_domains", ()) or ())
            home = (f"https://{domains[0]}/" if domains
                    else "https://www.ebsco.com/" if site == "ebsco" else "")
            return SiteInfo(site, h.display_name or site,
                            getattr(h, "terms_url", "") or "", home)
    if site in CONNECTOR_ONLY_SITES:
        display, terms = CONNECTOR_ONLY_SITES[site]
        return SiteInfo(site, display, terms, _HOMEPAGES.get(site, ""))
    home = f"https://{site}/" if "." in site else ""
    return SiteInfo(site, site, "", home)


def known_sites() -> list[SiteInfo]:
    """Every site with a name of its own: the handlers, then the
    Connector-only platforms."""
    seen: dict[str, SiteInfo] = {}
    for h in _handlers():
        seen.setdefault(h.name, site_info(h.name))
    for site in CONNECTOR_ONLY_SITES:
        seen.setdefault(site, site_info(site))
    return list(seen.values())


# ---------------------------------------------------------------------------
# Site keys for Connector targets
# ---------------------------------------------------------------------------

def _registrable(host: str) -> str:
    labels = [x for x in host.lower().strip(".").split(".") if x]
    if len(labels) <= 2:
        return ".".join(labels)
    if labels[-2] in _SECOND_LEVEL and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def unproxied_host(url: str) -> str:
    """The host a URL really reaches, with EZproxy unwrapped: the
    `login?url=<real>` wrapper (via `effective_host`) and the
    hostname-rewriting form `www-jstor-org.ezproxy.example.fi`, where the
    real host sits, hyphenated, in front of the proxy's label."""
    from fetchers.resolvers.base import effective_host

    host = effective_host(url)
    labels = host.split(".")
    for i, label in enumerate(labels):
        if i and "proxy" in label:
            return ".".join(labels[:i]).replace("--", "\0").replace(
                "-", ".").replace("\0", "-")
    return host


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60].strip("-")


def _platform_site(host: str, target) -> str:
    from fetchers.resolvers.base import PLATFORM_PRIORITY

    text = (f"{getattr(target, 'package_name', '')} "
            f"{getattr(target, 'interface_name', '')}").lower() if target else ""
    for plat in PLATFORM_PRIORITY:
        if host and any(host == d or host.endswith("." + d) for d in plat.domains):
            return _PLATFORM_SITE.get(plat.key, plat.key)
    if text.strip():
        for plat in PLATFORM_PRIORITY:
            if any(n.lower() in text for n in plat.names if n):
                return _PLATFORM_SITE.get(plat.key, plat.key)
    return ""


def site_for_target(target, url: str, *, landing_host: str = "") -> str:
    """The consent site for a Connector save of `url` (the resolver route
    `target`, or None for a fail-open doi.org link)."""
    host = unproxied_host(url or "")
    if host in _DOI_HOSTS:
        if not landing_host:
            return "doi.org"
        host = landing_host.lower()
    site = _platform_site(host, target)
    if site:
        return site
    if target is not None and "exlibrisgroup" in host:
        # An Alma redirector: the host says nothing, the package does.
        name = (getattr(target, "interface_name", "")
                or getattr(target, "package_name", ""))
        if _slug(name):
            return _slug(name)
    return _registrable(host) or "doi.org"


# ---------------------------------------------------------------------------
# The prompt and the per-run gate
# ---------------------------------------------------------------------------

def prompt_text(info: SiteInfo, *, mode: str, items: int) -> str:
    if info.terms_url:
        terms = f"  Terms:  {info.terms_url}"
    elif info.homepage:
        terms = (f"  Terms:  no link recorded for this site — see the terms of "
                 f"use on {info.homepage}")
    else:
        terms = "  Terms:  no link recorded for this site"
    plural = "" if items == 1 else "s"
    return (
        f"\nAutomated download from {info.display} [{info.site}]\n"
        f"  Mode:   {mode} — the plugin, not you, opens the pages and "
        f"downloads\n"
        f"  Items:  {items} item{plural} in this run\n"
        f"{terms}\n"
        f"  Whether this is permitted depends on your institution's licence "
        f"and the site's terms.\n"
        f"  [a] Allow — record 'allow' (dated) for {info.site}; not asked again\n"
        f"  [o] Once — download these now; ask again next time\n"
        f"  [d] Deny — record 'deny'; these go to the manual-download queue\n"
        f"  [Enter] Not now — skip {info.site} this run (CONSENT_NEEDED)\n> "
    )


def parse_answer(text: str) -> str:
    """`allow`, `once`, `deny`, or `skip` (anything else — Enter included:
    a non-answer is never a yes)."""
    t = (text or "").strip().lower()
    if t in ("a", "allow"):
        return "allow"
    if t in ("o", "once"):
        return "once"
    if t in ("d", "deny"):
        return "deny"
    return "skip"


class ConsentGate:
    """Decides, once per site per run, whether automated download goes ahead.

    `can_ask` is the run's to decide: True only with a person at a
    terminal (no `--no-prompt`, no `--control-file`, not unattended).
    `ask(prompt) -> answer` is the channel; `record` stores an answer.
    """

    def __init__(
        self,
        *,
        can_ask: bool,
        ask: Callable[[str], str] | None = None,
        lookup: Callable[[str], pc.Consent] = pc.lookup,
        record: Callable[..., object] = pc.record,
        out: Callable[[str], None] = lambda s: print(s, flush=True),
    ) -> None:
        self.can_ask = bool(can_ask and ask is not None)
        self._ask = ask
        self._lookup = lookup
        self._record = record
        self._out = out
        self.decisions: dict[str, str] = {}

    def status(self, site: str) -> pc.Consent:
        try:
            return self._lookup(site)
        except pc.ConsentError:
            return pc.Consent(site=site)

    def decide(self, site: str, *, mode: str, items: int) -> str:
        """`ALLOW`, `DENY` or `NEEDED` for `site`; asks at most once."""
        if site in self.decisions:
            return self.decisions[site]
        consent = self.status(site)
        verdict = pc.resolve(consent, can_ask=self.can_ask)
        if verdict == pc.PROCEED:
            decision = ALLOW
        elif verdict == pc.DENIED:
            decision = DENY
        elif verdict == pc.NEEDED:
            decision = NEEDED
        else:
            decision = self._ask_user(site, mode=mode, items=items)
        self.decisions[site] = decision
        return decision

    def _ask_user(self, site: str, *, mode: str, items: int) -> str:
        assert self._ask is not None
        answer = parse_answer(self._ask(prompt_text(site_info(site), mode=mode,
                                                    items=items)))
        if answer in ("allow", "deny"):
            try:
                self._record(site, answer)
                self._out(f"  Recorded: {site} automated = {answer!r} "
                          f"(config.toml [publisher_consent.{site}]).")
            except Exception as e:  # noqa: BLE001 — the run's answer stands
                self._out(f"  Could not record the answer for {site} ({e}); "
                          f"it applies to this run only.")
            return ALLOW if answer == "allow" else DENY
        if answer == "once":
            return ALLOW
        return NEEDED
