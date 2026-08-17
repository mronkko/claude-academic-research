"""Library link-resolver interface, shared by every resolver flavour.

A link resolver answers one question: *which licensed routes does this
institution have to this DOI?* Two products answer it in different XML
dialects — Ex Libris SFX and Ex Libris Alma's `uresolver` (what
Primo VE institutions get) — and this package treats them as peers
rather than treating Alma as a variant of SFX.

Why the split exists at all
---------------------------
Everything used to live in one module written around SFX's response
shape, with Alma bolted on as a second parse branch. Parsing worked;
everything built on top of it silently did not. Measured live against
an Alma tenant, for a DOI with 15 `getFullTxt` routes including
EBSCOhost, JSTOR and ProQuest:

    _effective_host(target)        -> <tenant>.alma.exlibrisgroup.com
    _platform_rank(target, ...)    -> len(priority)   i.e. unranked
    required_domains=ebscohost.com -> no route found

Alma's `resolution_url` always points at the Alma redirector, never at
the publisher, so any decision keyed on the URL's hostname is blind on
Alma. Platform preference — the reasoned choice of EBSCOhost over
JSTOR over ProQuest — was therefore dead there, and a domain filter
reported "no licensed route" for an article with fifteen.

The fix is not an Alma special case. It is to carry the provider
*names* Alma already sends (`package_public_name`, `interface_name`)
alongside the URL, and to rank on **host or name**. One code path in
this base class serves both dialects: SFX keeps matching by domain,
Alma starts matching by name, and a future third flavour gets both for
free.

Adding a flavour
----------------
Subclass `LibraryResolver`, implement `matches`, `query_urls` and
`parse`, and register the class in `resolvers/__init__.py`. Do not
override `rank_key` or `matches_domains` — they are deliberately shared,
and a flavour that needs its own ranking is a sign the target model is
missing a field instead.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

# SFX's service_type value for "this target serves the full text".
# Other service types (getHolding, getAuthor, getDOI, getWebSearch, ...)
# do not imply access. Alma's uresolver reuses the same value, as an
# attribute of <context_service> rather than a <target>'s child element.
FULLTEXT_SERVICE_TYPE = "getFullTxt"

# OpenURL 1.0 context parameters every dialect accepts. Flavour-specific
# additions (SFX's `sfx.*`, Alma's `svc_dat`) are added by the subclass,
# so neither dialect receives the other's vendor namespace.
OPENURL_CONTEXT_PARAMS: dict[str, str] = {
    "url_ver": "Z39.88-2004",
    "ctx_ver": "Z39.88-2004",
    "ctx_enc": "info:ofi/enc:UTF-8",
    "url_ctx_fmt": "info:ofi/fmt:kev:mtx:ctx",
    "svc_val_fmt": "info:ofi/fmt:kev:mtx:sch_svc",
}


# ---------------------------------------------------------------------------
# Target model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FulltextTarget:
    """One licensed full-text route the resolver reported.

    Replaces the bare URL string this package used to pass around. The
    URL alone is not enough to decide anything on Alma, where every
    route shares the same redirector host — the platform identity lives
    in `package_name` / `interface_name`.

    `coverage` and `is_free` are carried because Alma sends them and
    they are cheap to keep; nothing ranks on them yet.
    """

    url: str
    package_name: str = ""
    interface_name: str = ""
    coverage: str = ""
    is_free: bool = False

    def as_cache_dict(self) -> dict:
        """Serialise for `ResolverCache`. Omits empty fields so a cache
        file written against an SFX endpoint stays readable."""
        out: dict = {"url": self.url}
        if self.package_name:
            out["package_name"] = self.package_name
        if self.interface_name:
            out["interface_name"] = self.interface_name
        if self.coverage:
            out["coverage"] = self.coverage
        if self.is_free:
            out["is_free"] = True
        return out

    @classmethod
    def from_cache_dict(cls, d: dict) -> FulltextTarget | None:
        """Inverse of `as_cache_dict`; None when the entry is unusable."""
        url = (d.get("url") or "").strip()
        if not url:
            return None
        return cls(
            url=url,
            package_name=d.get("package_name", "") or "",
            interface_name=d.get("interface_name", "") or "",
            coverage=d.get("coverage", "") or "",
            is_free=bool(d.get("is_free", False)),
        )


# ---------------------------------------------------------------------------
# Platform priority
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Platform:
    """A full-text platform, identified by host suffix *and* by the name
    a resolver prints for it.

    Two identities because the two dialects expose different things: SFX
    emits a publisher or EZproxy URL whose host is recognisable, while
    Alma emits its own redirector URL and names the platform in a
    sibling field. Carrying both means one priority list drives ranking
    on either.
    """

    key: str
    domains: tuple[str, ...] = ()
    names: tuple[str, ...] = ()


# Priority order for full-text platforms when a resolver offers several
# routes for one DOI. Higher-ranked (earlier) entries win.
#
# Ranking rationale (unchanged from the original SFX-only list):
#   - EBSCOhost: cleanest PDFs in our testing.
#   - Publisher-direct (Elsevier/Wiley/Springer/Sage/T&F/OUP): also
#     clean. When offered alongside EBSCOhost, platform choice rarely
#     matters — prefer EBSCOhost for the UI the Zotero Connector
#     translator handles most consistently.
#   - JSTOR: adds a JSTOR-branded cover page.
#   - ProQuest: sometimes serves a scanned-image PDF where another
#     route has a digitally-typeset original. Last resort.
#
# `names` are matched case-insensitively as substrings, because Alma
# spells one platform several ways across packages ("EBSCOhost Business
# Source Ultimate", "EBSCOhost Academic Search Premier").
#
# Users can override via `[library] platform_priority` in config.toml,
# which takes a comma-separated list of these `key` values.
PLATFORM_PRIORITY: tuple[Platform, ...] = (
    Platform("ebscohost", ("ebscohost.com", "ebsco.com"), ("ebscohost", "ebsco")),
    Platform("sciencedirect", ("sciencedirect.com",), ("sciencedirect", "elsevier")),
    Platform("wiley", ("onlinelibrary.wiley.com", "wiley.com"), ("wiley",)),
    Platform("springer", ("link.springer.com", "springer.com"), ("springer",)),
    Platform("sage", ("journals.sagepub.com", "sagepub.com"), ("sage",)),
    Platform("tandf", ("tandfonline.com",), ("taylor & francis", "taylor and francis")),
    Platform("oup", ("academic.oup.com", "oup.com"), ("oxford university press",)),
    Platform("jstor", ("jstor.org",), ("jstor",)),
    Platform("proquest", ("proquest.com",), ("proquest",)),
)


def platform_priority_from_keys(
    keys: tuple[str, ...] | list[str],
    catalogue: tuple[Platform, ...] = PLATFORM_PRIORITY,
) -> tuple[Platform, ...]:
    """Reorder `catalogue` to follow `keys`.

    Unknown keys are ignored rather than raising: this comes from
    user config, and a typo should not stop a run — it should just not
    reorder anything. Platforms the user did not mention keep their
    relative order after the ones they did, so naming a single
    preference does not silently demote everything else to unranked.
    """
    by_key = {p.key: p for p in catalogue}
    chosen = [by_key[k] for k in keys if k in by_key]
    rest = [p for p in catalogue if p not in chosen]
    return tuple(chosen + rest)


# ---------------------------------------------------------------------------
# URL / name matching helpers
# ---------------------------------------------------------------------------


def local_name(el) -> str:
    """Element tag without its XML namespace prefix.

    Both dialects are served with and without namespace declarations
    depending on deployment, so every parse walks on local names.
    """
    tag = el.tag
    return tag.rpartition("}")[2] if "}" in tag else tag


def effective_host(target_url: str) -> str:
    """Hostname the target URL actually takes the user to, unwrapping
    EZproxy wrappers like `http://ezproxy.example.edu/login?url=<real>`.

    Returns '' when no hostname can be extracted (malformed URL).
    """
    if not target_url:
        return ""
    parsed = urlparse(target_url)
    host = (parsed.hostname or "").lower()
    # EZproxy/ebscohost/etc. patterns: real URL is in a `url=` query arg.
    if parsed.query:
        q = parse_qs(parsed.query)
        inner = q.get("url", [""])[0]
        if inner:
            inner_host = (urlparse(inner).hostname or "").lower()
            if inner_host:
                return inner_host
    return host


def host_matches_domains(target_url: str, domains: tuple[str, ...]) -> bool:
    """True when the URL's effective host ends with any of the given
    domain suffixes. Suffix-match, so "wiley.com" matches
    "onlinelibrary.wiley.com"."""
    host = effective_host(target_url)
    if not host:
        return False
    for d in domains:
        d = d.lower()
        if host == d or host.endswith("." + d):
            return True
    return False


def _target_text(target: FulltextTarget) -> str:
    """Lower-cased provider naming for substring matching."""
    return f"{target.package_name} {target.interface_name}".lower()


def _names_match(target: FulltextTarget, names: tuple[str, ...]) -> bool:
    text = _target_text(target)
    if not text.strip():
        return False
    return any(n.lower() in text for n in names if n)


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------


@dataclass
class ResolverRequest:
    """Everything a flavour needs to build its query URLs.

    A dataclass rather than a long kwargs list because Alma's ISSN
    fallback needs journal-level identity (`issn`/`pub_date`/`volume`)
    that SFX ignores, and threading four optionals through every call
    site was how the previous version grew unreadable.
    """

    doi: str
    ignore_date_threshold: bool = False
    issn: str | None = None
    pub_date: str | None = None
    volume: str | None = None
    sid: str = "academic-research"
    extra: dict = field(default_factory=dict)


class LibraryResolver(ABC):
    """One link-resolver dialect.

    Implementations are stateless value objects: they build query URLs
    and parse responses. Fetching, caching and fail-open policy all live
    in `library_resolver.py`, so a flavour cannot accidentally invent its
    own error semantics.
    """

    #: Short identifier, also the accepted value of `[library] resolver`.
    flavour: str = ""

    #: Whether this dialect can answer "is this DOI inside the licensed
    #: coverage range?" separately from "does the library know this
    #: journal at all?". SFX can, via `sfx.ignore_date_threshold`. Alma
    #: cannot: live testing found it ignores `rft.date`/`rft.volume`
    #: entirely, so asking twice returns the same answer twice. Callers
    #: use this to skip a pointless second round-trip and to avoid
    #: claiming a coverage verdict they cannot support.
    supports_date_threshold: bool = False

    def __init__(self, openurl_base: str) -> None:
        self.openurl_base = openurl_base

    @classmethod
    @abstractmethod
    def matches(cls, openurl_base: str) -> bool:
        """True when `openurl_base` is an endpoint of this dialect."""

    @abstractmethod
    def query_urls(self, req: ResolverRequest) -> list[str]:
        """Query URLs to try, in order, until one yields targets.

        Usually one. Alma returns a second, ISSN-keyed URL because some
        deployments link holdings only at journal level and answer a
        DOI-keyed query with nothing even for licensed journals.
        """

    @abstractmethod
    def parse(self, xml_text: str) -> list[FulltextTarget] | None:
        """Full-text targets in one response.

        Returns a list (possibly empty) on success, and None when the
        payload could not be parsed. Callers depend on that distinction:
        empty means "the library has no route", None means "we could not
        ask", and collapsing them turns a transient blip into a
        permanent false negative.
        """

    # -- shared decisions; do not override -------------------------------

    def rank_key(
        self, target: FulltextTarget,
        priority: tuple[Platform, ...] = PLATFORM_PRIORITY,
    ) -> int:
        """Index into `priority` (lower is better).

        Matches by host first, then by provider name, so one list ranks
        both dialects. Targets matching nothing return `len(priority)`:
        they lose to any ranked platform but still beat having no target
        at all.
        """
        for i, plat in enumerate(priority):
            if plat.domains and host_matches_domains(target.url, plat.domains):
                return i
            if plat.names and _names_match(target, plat.names):
                return i
        return len(priority)

    def matches_domains(
        self, target: FulltextTarget, domains: tuple[str, ...],
    ) -> bool:
        """True when this target is served by one of `domains`.

        Callers pass the domains their downloader can actually reach.
        The host check is authoritative when the URL exposes a real
        publisher host; when it does not — every Alma target — fall back
        to the platform naming, mapping each requested domain to its
        `Platform` entry so `("ebscohost.com",)` still matches a target
        named "EBSCOhost Business Source Ultimate".
        """
        if host_matches_domains(target.url, domains):
            return True
        names: list[str] = []
        for plat in PLATFORM_PRIORITY:
            if any(
                d.lower() == pd or d.lower().endswith("." + pd)
                or pd.endswith("." + d.lower())
                for d in domains for pd in (x.lower() for x in plat.domains)
            ):
                names.extend(plat.names)
        return _names_match(target, tuple(names)) if names else False
