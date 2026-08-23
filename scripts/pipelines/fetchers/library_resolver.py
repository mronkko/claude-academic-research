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
import os
import time
from dataclasses import dataclass, replace
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
    from collections.abc import Iterable

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

    Lives alongside the PDF cache directory by default, so deleting that
    directory also clears this. `enrich_pdfs.py --resolver-cache-dir`
    separates the two, because their economics are opposite: the PDF
    cache is gigabytes and cheap to rebuild in parallel, while this is a
    few megabytes rebuilt one serial query at a time against an
    institutional endpoint. A caller keeping one cache directory per pass
    — the natural thing to do when `--log-csv` is also per-pass — silently
    fragments the expensive one. Stale entries are low-risk: if a library
    adds a subscription, the worst case is that a route we already knew
    about stays cached, and a fresh run after deleting the cache picks up
    the change.

    The file is `resolver_cache.json`. Earlier versions wrote
    `sfx_cache.json` with a bare URL list (`{"urls": [...]}`) and, before
    that, `{"has_access": bool, "targets": int}`. Neither carried the
    provider names that ranking now depends on, so rather than migrate a
    shape that cannot answer the current question, this uses a new
    filename and lets the old file be ignored. One resolver round-trip
    per DOI on the first run after upgrading; cached from then on.
    """

    #: Seconds a *negative* answer stays believable. Positives never
    #: expire — a library that holds a title today held it yesterday, and
    #: a route that disappears costs one failed fetch, not a wrong
    #: verdict. Misses are the opposite: the resolver keys on DOI, so a
    #: journal reached through an aggregator answers empty for reasons
    #: that have nothing to do with entitlement.
    #:
    #: This used to be "never cache a miss at all", after a live run
    #: cached empties with no expiry and permanently skipped 15 *Journal
    #: of Business Ethics* articles the user demonstrably had access to.
    #: That fix worked but overshot: every miss was then re-queried on
    #: every run forever, and with the browser pass now driven one
    #: publisher at a time, the same fruitless lookups repeated once per
    #: block — the single slowest thing in the pre-flight, on a queue
    #: where misses outnumber hits.
    #:
    #: A week is the compromise. Long enough that a ten-block browser
    #: session, or a day of re-runs, asks once; short enough that a miss
    #: cannot outlive a subscription change by much.
    #:
    #: The recovery path the old rule protected — gain access, re-run,
    #: get it — is preserved by `enrich_pdfs.py --refresh-resolver-cache`,
    #: which sets this to 0 for one run and re-asks every miss while
    #: keeping known routes. That flag exists because the original
    #: incident report named the real problem precisely: clearing the
    #: cache meant deleting a directory that also holds the PDF cache and
    #: both Chromium profiles. A TTL without an escape hatch would have
    #: reintroduced that, slower.
    miss_ttl_s: float = 7 * 24 * 3600

    #: Writes go through on every `put`, and deliberately so. Batching
    #: them was tried and reverted: serialising the whole dict costs
    #: ~5-10 ms against ~400 ms of network per item, so the saving is
    #: ~2% of pre-flight wall time — not worth a cache that loses its
    #: tail when a run is interrupted, which is exactly when the next
    #: run most needs it. The write is atomic (tmp + replace) so a
    #: process killed mid-write leaves the previous file intact rather
    #: than a truncated one.

    def __init__(self, cache_dir: str | Path,
                 miss_ttl_s: float | None = None) -> None:
        self.path = Path(cache_dir) / "resolver_cache.json"
        self._data: dict[str, dict] = {}
        if miss_ttl_s is not None:
            self.miss_ttl_s = miss_ttl_s
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text())
            except Exception:
                # Corrupt cache — start fresh, don't fail the whole run.
                self._data = {}

    def get(self, key: str) -> list[FulltextTarget] | None:
        """Cached answer, or None when this DOI must be asked about.

        Three outcomes, and the caller depends on telling them apart:
        a list of targets (known route), an empty list (asked recently,
        no route — do not ask again yet), and None (never asked, or the
        miss has aged out).
        """
        entry = self._data.get(key)
        if not entry or "targets" not in entry:
            return None
        miss_at = entry.get("miss_at")
        if miss_at is not None:
            if time.time() - float(miss_at) > self.miss_ttl_s:
                return None
            return []
        out = []
        for raw in entry["targets"]:
            target = FulltextTarget.from_cache_dict(raw)
            if target is not None:
                out.append(target)
        return out or None

    def put(self, key: str, targets: list[FulltextTarget]) -> None:
        self._data[key] = {"targets": [t.as_cache_dict() for t in targets]}
        self._write(key)

    def put_miss(self, key: str) -> None:
        """Record that the resolver answered, with nothing.

        Timestamped, unlike a positive, because that is the whole basis
        on which it is allowed to be forgotten.
        """
        self._data[key] = {"targets": [], "miss_at": time.time()}
        self._write(key)

    def _write(self, fresh_key: str | None = None) -> None:
        # Best-effort write — don't crash the pipeline on a filesystem
        # hiccup. Losing a cache entry costs one repeated query.
        #
        # Re-read and merge first. tmp+replace is atomic, so this file was
        # never at risk of corruption, but atomic is not the same as
        # correct when two processes share it: each holds the whole dict
        # from its own startup, and the later writer replaces the file
        # with a snapshot that never saw the earlier one's entries. That
        # did not matter while the cache was pinned to the PDF cache
        # directory, since every run had its own. `--resolver-cache-dir`
        # exists precisely so several passes can share one, which makes
        # last-writer-wins a way to lose exactly the answers the sharing
        # was meant to preserve.
        #
        # **Disk wins every key except the one that triggered this
        # write.** Our copy of any other key was either loaded at startup
        # or written by us earlier, so whatever is on disk now is at
        # least as new; `fresh_key` is the single fact this process knows
        # more recently than the file does. The opposite rule — keeping
        # our own — reads plausibly and is wrong: a long-running pass
        # that cached a miss early would re-publish that miss over
        # another pass's later positive answer, on every subsequent
        # write. Timestamps cannot arbitrate instead, because positives
        # deliberately carry none.
        #
        # Cost is one small read per put, against ~400 ms of network per
        # item.
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                try:
                    on_disk = json.loads(self.path.read_text())
                except Exception:
                    on_disk = {}
                if isinstance(on_disk, dict):
                    for key, entry in on_disk.items():
                        if key != fresh_key:
                            self._data[key] = entry
            # Per-process scratch name. A shared directory means two
            # runs would otherwise write the same `.tmp` file, and one
            # `replace()` would publish the other's half-written bytes.
            tmp = self.path.with_suffix(f".json.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(self._data, indent=1))
            tmp.replace(self.path)
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
    #: Further institutions, queried after `resolver` and merged into one
    #: route list. A researcher with two affiliations has two sets of
    #: entitlements and no single resolver knows both: one live case had
    #: an Alma tenant reporting *no* route for a journal that the second
    #: institution's SFX served through Ovid and ProQuest.
    #:
    #: `resolver` stays the primary rather than this being a plain list,
    #: so every existing construction site and cache key is untouched and
    #: adding a second library cannot invalidate the first one's cache.
    additional_resolvers: tuple[LibraryResolver, ...] = ()

    @property
    def resolvers(self) -> tuple[LibraryResolver, ...]:
        """Every configured resolver, primary first. Empty when unconfigured."""
        return tuple(
            r for r in (self.resolver, *self.additional_resolvers) if r is not None
        )

    @property
    def openurl_base(self) -> str:
        """The primary endpoint, or '' when unconfigured. A property so
        callers can log where they are querying without reaching through
        to the resolver."""
        return self.resolver.openurl_base if self.resolver else ""

    def describe(self) -> str:
        """One line naming every endpoint, for the run's opening banner."""
        bases = [r.openurl_base for r in self.resolvers]
        if not bases:
            return "(no link resolver configured)"
        if len(bases) == 1:
            return bases[0]
        return f"{bases[0]} (+{len(bases) - 1} more)"


def load_from_config(
    session: requests.Session,
    cache_dir: str | Path | None = None,
    *,
    miss_ttl_s: float | None = None,
) -> LibraryResolverConfig | None:
    """Build a config from `[library]` in config.toml.

    Returns None when `openurl_base` is absent — callers MUST treat None
    as "no pre-flight, fall through to the handler directly", never as
    "the library has no access".

    `openurl_base` takes a string or a **list** of endpoints. A list is
    for a reader with more than one affiliation: each is queried and the
    routes are merged, because no single institution's resolver knows
    another's entitlements. The first entry is the primary — it keeps the
    existing cache keys and breaks ranking ties — so put the library you
    are normally authenticated to first.

    `[library] resolver` (auto/sfx/alma) applies to a single endpoint. It
    is deliberately ignored for a list: each entry is autodetected from
    its own URL shape, since forcing one dialect onto endpoints of two
    different products is never right.
    """
    from core.config_loader import get, load_config

    raw_base = load_config().get("library", {}).get("openurl_base", "")
    if isinstance(raw_base, list):
        bases = [str(b).strip() for b in raw_base if str(b).strip()]
        override = ""
    else:
        env_or_str = get(
            "library", "openurl_base", env="LIBRARY_OPENURL_BASE",
        ).strip()
        bases = [env_or_str] if env_or_str else []
        override = get("library", "resolver", env="LIBRARY_RESOLVER").strip()
    if not bases:
        return None

    built = [r for r in (resolver_for(b, override) for b in bases) if r is not None]
    if not built:
        return None
    resolver, additional = built[0], tuple(built[1:])

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
        additional_resolvers=additional,
        session=session,
        cache=ResolverCache(cache_dir, miss_ttl_s) if cache_dir else None,
        priority=priority,
    )


# ---------------------------------------------------------------------------
# Query + cache
# ---------------------------------------------------------------------------


def _cache_key(
    doi: str, ignore_date_threshold: bool = False, resolver_id: str = "",
) -> str:
    """Cache key combining DOI, the ignore-date-threshold flag and — for
    a non-primary resolver — which library answered.

    The primary resolver keeps the bare-DOI key it has always used, so
    adding a second institution does not invalidate a warm cache built
    against the first. Entries are per-resolver rather than per-merge for
    the same reason: removing one library must not discard the other's
    answers.
    """
    base = f"{doi}::any" if ignore_date_threshold else doi
    return f"{base}@@{resolver_id}" if resolver_id else base


def _fetch_and_parse(
    url: str, cfg: LibraryResolverConfig, doi: str,
    resolver: LibraryResolver | None = None,
) -> list[FulltextTarget] | None:
    """GET `url` and parse it with `resolver` (default: the primary).

    None on transport / non-200 / parse failure; `doi` is only for
    logging. The dialect must be the one that produced `url` — parsing an
    SFX response with Alma's parser yields nothing, silently.
    """
    resolver = resolver if resolver is not None else cfg.resolver
    if resolver is None:
        return None
    try:
        resp = cfg.session.get(url, timeout=cfg.timeout_s)
    except Exception as e:
        logger.debug("resolver request failed for %s: %s", doi, e)
        return None
    if resp.status_code != 200:
        logger.debug("resolver returned HTTP %d for %s", resp.status_code, doi)
        return None
    return resolver.parse(resp.text)


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
    resolvers = cfg.resolvers
    if not resolvers:
        return None

    req = ResolverRequest(
        doi=doi, ignore_date_threshold=ignore_date_threshold,
        issn=issn, pub_date=pub_date, volume=volume, sid=cfg.sid,
    )

    merged: list[FulltextTarget] = []
    answered = False
    for index, resolver in enumerate(resolvers):
        one = _query_one(
            resolver, doi, cfg, req,
            ignore_date_threshold=ignore_date_threshold,
            resolver_id="" if index == 0 else resolver.openurl_base,
        )
        if one is None:
            continue          # this library could not be asked
        answered = True
        merged.extend(one)

    # None only when *no* library answered. One institution being down
    # must not read as "nobody has this": with several configured, a
    # partial answer is still an answer, and the alternative is a
    # transport blip at one library gating access at the other.
    if not answered:
        return None
    return _dedupe_targets(merged)


def _query_one(
    resolver: LibraryResolver,
    doi: str,
    cfg: LibraryResolverConfig,
    req: ResolverRequest,
    *,
    ignore_date_threshold: bool,
    resolver_id: str,
) -> list[FulltextTarget] | None:
    """One library's answer for `doi`, cached per library. None when it
    could not be asked."""
    key = _cache_key(doi, ignore_date_threshold, resolver_id)
    if cfg.cache is not None:
        cached = cfg.cache.get(key)
        if cached is not None:
            return cached

    label = _resolver_label(resolver)
    targets: list[FulltextTarget] | None = None
    for url in resolver.query_urls(req):
        result = _fetch_and_parse(url, cfg, doi, resolver)
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
    # Stamp the origin only when it can be ambiguous — a single-resolver
    # setup keeps writing exactly the cache entries it always has.
    if resolver_id and targets:
        targets = [replace(t, resolver_name=label) for t in targets]
    if cfg.cache is not None:
        if targets:
            cfg.cache.put(key, targets)
        else:
            # An answer, and answers are worth remembering — for a while.
            # See `ResolverCache.miss_ttl_s` for why this is time-boxed
            # rather than permanent, and why it is no longer discarded.
            cfg.cache.put_miss(key)
    return targets


def _resolver_label(resolver: LibraryResolver) -> str:
    """Short human name for a resolver, derived from its endpoint.

    Both products put the institution in the path, differently: Alma as a
    tenant code (`.../358AALTO_INST/openurl`), SFX as an instance segment
    (`https://sfx.example.fi/jyu`). The host is the last resort and
    usually the worst option — an SFX host commonly starts with the
    literal "sfx", which names the product rather than the library.

    Only ever used in diagnostics and in `FulltextTarget.resolver_name`,
    so an imperfect label costs nothing.
    """
    from urllib.parse import urlparse

    parsed = urlparse(resolver.openurl_base)
    segments = [p for p in parsed.path.split("/") if p]
    for part in segments:
        if part.upper().endswith("_INST"):
            # "358AALTO_INST" -> "Aalto": strip the numeric prefix Alma
            # prepends to the tenant code.
            return part.split("_")[0].lstrip("0123456789").title() or part
    for part in reversed(segments):
        if part.lower() not in ("openurl", "resolve", "sfx", "view", "uresolver"):
            return part.title()
    return (parsed.netloc or resolver.openurl_base).split(".")[0].title()


def _dedupe_targets(targets: list[FulltextTarget]) -> list[FulltextTarget]:
    """Drop exact duplicate URLs, preserving order.

    Two libraries can name the same open-access or free route. Routes
    that merely share a *platform* are deliberately kept: they are
    different entitlements behind different logins, and which one works
    depends on who the reader is.
    """
    seen: set[str] = set()
    out: list[FulltextTarget] = []
    for target in targets:
        if target.url in seen:
            continue
        seen.add(target.url)
        out.append(target)
    return out


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


def dual_cache_keys(doi: str, cfg: LibraryResolverConfig) -> list[str]:
    """Every cache key `lookup_dual(doi, cfg)` would consult.

    Kept beside `lookup_dual` rather than derived by callers, because the
    two must not drift: the key set depends on the primary dialect's
    `supports_date_threshold` and on how many libraries are configured,
    and a caller reasoning about "is this DOI warm" from the bare DOI
    would be right only in the single-SFX-library case.
    """
    variants = [False]
    if cfg.resolver is not None and cfg.resolver.supports_date_threshold:
        variants.append(True)
    keys: list[str] = []
    for index, resolver in enumerate(cfg.resolvers):
        resolver_id = "" if index == 0 else resolver.openurl_base
        keys.extend(_cache_key(doi, ignore, resolver_id) for ignore in variants)
    return keys


def cached_answer_count(
    dois: Iterable[str], cfg: LibraryResolverConfig,
) -> int:
    """How many of `dois` `lookup_dual` could answer from disk alone.

    Read-only and network-free. It exists so a caller can price a
    pre-flight sweep *before* running it: a sweep is serial and roughly
    two seconds per uncached item, so a queue of a few thousand is an
    hour, and the user should be told that up front rather than discover
    it forty minutes in.

    A DOI counts only when **every** key the dual lookup would consult is
    already present. Counting a partially-warm DOI would make the
    estimate optimistic in precisely the case that matters — the first
    run after adding a second institution, where the primary's answers
    are all warm and every item still costs a round-trip.
    """
    cache = cfg.cache
    if cache is None or not cfg.resolvers:
        return 0
    return sum(
        1 for doi in dois
        if all(cache.get(key) is not None for key in dual_cache_keys(doi, cfg))
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
    "cached_answer_count",
    "dual_cache_keys",
    "effective_host",
    "first_fulltext_target_preferred",
    "has_fulltext_access",
    "host_matches_domains",
    "load_from_config",
    "lookup_dual",
    "lookup_fulltext_target",
    "targets_match_domains",
]
