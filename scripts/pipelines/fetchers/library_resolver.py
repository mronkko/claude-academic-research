"""Library link-resolver pre-flight access check.

Answers "does this institution have a licensed full-text route to this
DOI, and via which platform?" before the browser flow opens Chromium for
an item it could never reach. The API cascade uses our own keys and does
not need this; only the browser and Connector passes benefit.

This module owns *policy* — fetching, caching, fail-open semantics and
platform ranking. It owns no dialect knowledge: SFX and Alma
`uresolver` (what Primo VE institutions have) are peer implementations
of `LibraryResolver` in `fetchers/resolvers/`, selected by
`[library] resolver` or autodetected. See that package's `base.py` for
why the two are peers rather than one being a variant of the other.

Configuration (`~/.config/academic-research/config.toml`)::

    [library]
    openurl_base = "https://eu03.alma.exlibrisgroup.com/view/uresolver/<inst>/openurl"
    resolver = "auto"                     # or "sfx" / "alma"
    platform_priority = "ebscohost,jstor" # optional reordering

Finding your own institution's `openurl_base`:
    - SFX: your library or its existing OpenURL/citation-manager
      documentation usually has this already (often the base URL handed
      to EndNote/RefWorks/Zotero's "institutional proxy").
    - Alma: open a Primo VE "Get it"/"View it" link for any item and read
      the outbound request in your browser's Network tab — it is the
      `.../view/uresolver/<inst_code>/openurl` URL up to the `?`. This
      URL is routinely shared for third-party integrations (LibKey
      Nomad, Lean Library, browser extensions), so your library's
      systems staff can usually just hand it to you.

Usage::

    from fetchers.library_resolver import load_from_config, lookup_fulltext_target

    cfg = load_from_config(session, cache_dir)
    if cfg is not None:
        target, query_ok = lookup_fulltext_target(doi, cfg)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from fetchers.resolvers import (
    PLATFORM_PRIORITY,
    FulltextTarget,
    LibraryResolver,
    Platform,
    ResolverRequest,
    effective_host,
    host_matches_domains,
    platform_priority_from_keys,
    resolver_for,
)

if TYPE_CHECKING:
    import requests

logger = logging.getLogger(__name__)

# Timeout for a single resolver request. Usually snappy (sub-second) but
# can stall on slow targets; cap so one DOI cannot block a whole batch.
_DEFAULT_TIMEOUT_S = 10


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class ResolverCache:
    """On-disk DOI → `{"targets": [...]}` cache.

    Lives alongside the PDF cache directory, so deleting that directory
    also clears this. Stale entries are low-risk: if a library adds a
    subscription, the worst case is that a route we already knew about
    stays cached, and a fresh run after deleting the cache picks up the
    change.

    The file is `resolver_cache.json`. Earlier versions wrote
    `sfx_cache.json` with a bare URL list (`{"urls": [...]}`) and, before
    that, `{"has_access": bool, "targets": int}`. Neither carried the
    provider names that ranking now depends on, so rather than migrate a
    shape that cannot answer the current question, this uses a new
    filename and lets the old file be ignored. One resolver round-trip
    per DOI on the first run after upgrading; cached from then on.
    """

    def __init__(self, cache_dir: str | Path) -> None:
        self.path = Path(cache_dir) / "resolver_cache.json"
        self._data: dict[str, dict] = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text())
            except Exception:
                # Corrupt cache — start fresh, don't fail the whole run.
                self._data = {}

    def get(self, key: str) -> list[FulltextTarget] | None:
        entry = self._data.get(key)
        if not entry or "targets" not in entry:
            return None
        out = []
        for raw in entry["targets"]:
            target = FulltextTarget.from_cache_dict(raw)
            if target is not None:
                out.append(target)
        return out or None

    def put(self, key: str, targets: list[FulltextTarget]) -> None:
        self._data[key] = {"targets": [t.as_cache_dict() for t in targets]}
        # Best-effort write — don't crash the pipeline on a filesystem hiccup.
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, indent=1))
        except Exception as e:
            logger.debug("ResolverCache write failed: %s", e)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class LibraryResolverConfig:
    """Everything a lookup needs.

    `resolver` is the dialect implementation; when it is None the caller
    has no configured endpoint and must skip the pre-flight entirely
    rather than treat the absence as "no access".
    """

    resolver: LibraryResolver | None
    session: requests.Session
    cache: ResolverCache | None = None
    timeout_s: int = _DEFAULT_TIMEOUT_S
    priority: tuple[Platform, ...] = PLATFORM_PRIORITY
    #: Source identifier included in the OpenURL request. Helps libraries
    #: correlate resolver traffic to the plugin. Not required.
    sid: str = "academic-research"

    @property
    def openurl_base(self) -> str:
        """The configured endpoint, or '' when unconfigured. A property so
        callers can log where they are querying without reaching through
        to the resolver."""
        return self.resolver.openurl_base if self.resolver else ""


def load_from_config(
    session: requests.Session,
    cache_dir: str | Path | None = None,
) -> LibraryResolverConfig | None:
    """Build a config from `[library]` in config.toml.

    Returns None when `openurl_base` is absent — callers MUST treat None
    as "no pre-flight, fall through to the handler directly", never as
    "the library has no access".
    """
    from core.config_loader import get, load_config

    base = get("library", "openurl_base", env="LIBRARY_OPENURL_BASE").strip()
    if not base:
        return None
    override = get("library", "resolver", env="LIBRARY_RESOLVER").strip()
    resolver = resolver_for(base, override)
    if resolver is None:
        return None

    raw_priority = load_config().get("library", {}).get("platform_priority", "")
    if isinstance(raw_priority, str):
        keys = tuple(k.strip() for k in raw_priority.split(",") if k.strip())
    elif isinstance(raw_priority, list):
        keys = tuple(str(k).strip() for k in raw_priority if str(k).strip())
    else:
        keys = ()
    priority = platform_priority_from_keys(keys) if keys else PLATFORM_PRIORITY

    return LibraryResolverConfig(
        resolver=resolver,
        session=session,
        cache=ResolverCache(cache_dir) if cache_dir else None,
        priority=priority,
    )


# ---------------------------------------------------------------------------
# Query + cache
# ---------------------------------------------------------------------------


def _cache_key(doi: str, ignore_date_threshold: bool = False) -> str:
    """Cache key combining DOI and the ignore-date-threshold flag."""
    return f"{doi}::any" if ignore_date_threshold else doi


def _fetch_and_parse(
    url: str, cfg: LibraryResolverConfig, doi: str,
) -> list[FulltextTarget] | None:
    """GET `url` and parse it. None on transport / non-200 / parse
    failure; `doi` is only for logging."""
    if cfg.resolver is None:
        return None
    try:
        resp = cfg.session.get(url, timeout=cfg.timeout_s)
    except Exception as e:
        logger.debug("resolver request failed for %s: %s", doi, e)
        return None
    if resp.status_code != 200:
        logger.debug("resolver returned HTTP %d for %s", resp.status_code, doi)
        return None
    return cfg.resolver.parse(resp.text)


def _query_targets(
    doi: str,
    cfg: LibraryResolverConfig,
    *,
    ignore_date_threshold: bool = False,
    issn: str | None = None,
    pub_date: str | None = None,
    volume: str | None = None,
) -> list[FulltextTarget] | None:
    """Full-text targets for `doi`, or None when the resolver could not
    answer.

    Tries each URL the dialect offers in order, stopping at the first that
    yields targets — Alma's second, ISSN-keyed URL exists because a
    DOI-keyed query can come back empty for a journal the library does
    license.

    Results are cached per `(doi, ignore_date_threshold)` whichever query
    produced them: the cache answers "does the library have this DOI", not
    "which query strategy worked".

    **Positive results only.** An empty result is a claim about holdings
    *as the resolver could see them for this DOI*, and that claim is wrong
    often enough to matter — the resolver keys on DOI, so a journal
    reached through an aggregator can come back empty. Persisting that
    turned a soft miss into a permanent one: a live run skipped 15
    *Journal of Business Ethics* articles the user demonstrably had access
    to, and re-running could never re-check because the empty answer was
    cached with no expiry.
    """
    if cfg.resolver is None:
        return None

    key = _cache_key(doi, ignore_date_threshold)
    if cfg.cache is not None:
        cached = cfg.cache.get(key)
        if cached is not None:
            return cached

    req = ResolverRequest(
        doi=doi, ignore_date_threshold=ignore_date_threshold,
        issn=issn, pub_date=pub_date, volume=volume, sid=cfg.sid,
    )
    targets: list[FulltextTarget] | None = None
    for url in cfg.resolver.query_urls(req):
        result = _fetch_and_parse(url, cfg, doi)
        if result:
            targets = result
            break
        if result is not None and targets is None:
            # Reached the resolver, which said nothing. Keep the empty
            # list so "no route" stays distinct from "could not ask",
            # but keep trying any remaining query shapes.
            targets = result

    if targets is None:
        return None
    if cfg.cache is not None and targets:
        cfg.cache.put(key, targets)
    return targets


# ---------------------------------------------------------------------------
# Public lookups
# ---------------------------------------------------------------------------


class TargetLookup(NamedTuple):
    """Outcome of a resolver query.

    `query_ok=False` means the resolver could not answer at all — unset
    config, transport error, non-200, unparseable XML. That is **not**
    evidence of missing access, and callers must not treat it as such.
    `url=None` with `query_ok=True` is the real "library has no licensed
    route" verdict.

    `target` is the winning `FulltextTarget`, or None when there was no
    winner. Callers need it to know *which platform* they were sent to:
    on Alma every URL is the same redirector host, so the platform is
    only knowable from the target's `interface_name` / `package_name`.
    That is what lets a platform-specific handler (EBSCO) be chosen over
    the generic Connector.
    """

    url: str | None
    query_ok: bool
    target: FulltextTarget | None = None


def lookup_fulltext_target(
    doi: str,
    cfg: LibraryResolverConfig,
    *,
    priority: tuple[Platform, ...] | None = None,
    in_range_only: bool = True,
    required_domains: tuple[str, ...] = (),
    issn: str | None = None,
    pub_date: str | None = None,
    volume: str | None = None,
) -> TargetLookup:
    """One full-text target URL for `doi`, highest-priority platform first.

    Reports query health alongside the URL because this gates a browser
    open: failing the gate closed on an ambiguous signal makes a transport
    blip indistinguishable from a real entitlement gap.

    - `in_range_only=True` (default) uses the date-filtered query, i.e.
      platforms that can unlock this DOI now. On a dialect without date
      filtering (Alma) both settings ask the same question.
    - `required_domains` restricts candidates to platforms the caller can
      actually reach. Matching is host-or-name, so this works on Alma,
      whose URLs never expose a publisher host.
    """
    if cfg.resolver is None:
        return TargetLookup(None, False)

    targets = _query_targets(
        doi, cfg, ignore_date_threshold=not in_range_only,
        issn=issn, pub_date=pub_date, volume=volume,
    )
    if targets is None:
        return TargetLookup(None, False)
    if not targets:
        return TargetLookup(None, True)

    if required_domains:
        targets = [
            t for t in targets
            if cfg.resolver.matches_domains(t, required_domains)
        ]
        if not targets:
            return TargetLookup(None, True)

    ranking = priority if priority is not None else cfg.priority
    resolver = cfg.resolver
    # Stable: equal keys keep the resolver's response order. `pub_date`
    # makes coverage outrank platform preference, so an embargoed
    # first-choice platform loses to one that actually holds this year.
    best = min(
        targets,
        key=lambda t: resolver.sort_key(t, ranking, pub_date=pub_date),
    )
    return TargetLookup(best.url, True, best)


def first_fulltext_target_preferred(
    doi: str,
    cfg: LibraryResolverConfig,
    *,
    priority: tuple[Platform, ...] | None = None,
    in_range_only: bool = True,
    required_domains: tuple[str, ...] = (),
    issn: str | None = None,
    pub_date: str | None = None,
    volume: str | None = None,
) -> str | None:
    """`lookup_fulltext_target`, discarding query health.

    Collapses "no coverage" and "couldn't ask" into the same None. Use
    `lookup_fulltext_target` when that difference matters — for gating
    decisions it always does.
    """
    return lookup_fulltext_target(
        doi, cfg, priority=priority, in_range_only=in_range_only,
        required_domains=required_domains, issn=issn, pub_date=pub_date,
        volume=volume,
    ).url


def has_fulltext_access(
    doi: str,
    cfg: LibraryResolverConfig,
    *,
    required_domains: tuple[str, ...] = (),
    issn: str | None = None,
    pub_date: str | None = None,
    volume: str | None = None,
) -> bool:
    """True if the library has at least one full-text route for this DOI.

    Fail-open: any transport error, parse error, or unset config returns
    True ("proceed, the handler may still work"). The point of the
    pre-flight is to skip *hopeless* items; when the signal is ambiguous,
    lean toward letting the handler try.
    """
    if cfg.resolver is None:
        return True
    result = lookup_fulltext_target(
        doi, cfg, required_domains=required_domains,
        issn=issn, pub_date=pub_date, volume=volume,
    )
    if not result.query_ok:
        return True
    return result.url is not None


# ---------------------------------------------------------------------------
# Dual query — distinguishes "no relationship with the publisher" from
# "has the publisher but this year is out of coverage".
# ---------------------------------------------------------------------------


@dataclass
class DualResult:
    """Result of the coverage-range comparison.

    - `in_range`: targets from the date-filtered query — routes the
      library can unlock right now.
    - `any_range`: targets ignoring coverage dates — every platform the
      resolver knows for the journal. A superset of `in_range`.
    - `query_ok`: False if either call failed.
    - `date_filtering_available`: False when the dialect cannot filter on
      coverage dates at all (Alma). Both lists are then the *same* query,
      so a caller must not read `in_range == any_range` as evidence that
      this DOI is inside coverage — it is evidence of nothing.
    """

    in_range: list[FulltextTarget]
    any_range: list[FulltextTarget]
    query_ok: bool = True
    date_filtering_available: bool = True


def lookup_dual(
    doi: str, cfg: LibraryResolverConfig,
    *,
    issn: str | None = None,
    pub_date: str | None = None,
    volume: str | None = None,
) -> DualResult:
    """Both coverage queries, or one when the dialect cannot filter dates.

    On SFX this is two calls (`sfx.ignore_date_threshold` off then on),
    each cached independently — a cache hit on every run after the first.
    On Alma it is **one** call reused for both fields, because live
    testing found Alma returns identical results for correct, wrong and
    absent date/volume values; a second request would double traffic for
    the same answer.
    """
    if cfg.resolver is None:
        return DualResult([], [], query_ok=False)

    in_range = _query_targets(
        doi, cfg, ignore_date_threshold=False,
        issn=issn, pub_date=pub_date, volume=volume,
    )
    if not cfg.resolver.supports_date_threshold:
        return DualResult(
            in_range=in_range or [],
            any_range=in_range or [],
            query_ok=in_range is not None,
            date_filtering_available=False,
        )

    any_range = _query_targets(
        doi, cfg, ignore_date_threshold=True,
        issn=issn, pub_date=pub_date, volume=volume,
    )
    return DualResult(
        in_range=in_range or [],
        any_range=any_range or [],
        query_ok=in_range is not None and any_range is not None,
    )


def targets_match_domains(
    targets: list[FulltextTarget],
    domains: tuple[str, ...],
    cfg: LibraryResolverConfig,
    *,
    pub_date: int | str | None = None,
) -> bool:
    """True when any target is served by one of `domains`.

    Exists so callers comparing `DualResult` lists against a handler's
    reachable domains go through the dialect's host-or-name matching
    rather than re-implementing a hostname test that is blind on Alma.

    `pub_date` additionally requires that the matching target's coverage
    include that year. **This is what restores Case 2 detection on a
    dialect that cannot filter by date in the query.** Asked with a year,
    the answer is "the library can reach this platform *for this
    article*"; asked without one, merely "the library has this
    platform". Diffing the two is the Case 2 test, reached by per-target
    coverage instead of SFX's `sfx.ignore_date_threshold`.

    A target whose coverage is absent or unparseable counts as matching —
    unknown is not evidence of exclusion, and on SFX every target is
    unknown, so passing `pub_date` there changes nothing.
    """
    if cfg.resolver is None:
        return False
    for t in targets:
        if not cfg.resolver.matches_domains(t, domains):
            continue
        if pub_date is not None and t.covers_year(pub_date) is False:
            continue
        return True
    return False


__all__ = [
    "DualResult",
    "FulltextTarget",
    "LibraryResolverConfig",
    "PLATFORM_PRIORITY",
    "Platform",
    "ResolverCache",
    "TargetLookup",
    "effective_host",
    "first_fulltext_target_preferred",
    "has_fulltext_access",
    "host_matches_domains",
    "load_from_config",
    "lookup_dual",
    "lookup_fulltext_target",
    "targets_match_domains",
]
