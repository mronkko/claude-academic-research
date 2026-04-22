"""Library link-resolver (SFX / OpenURL) pre-flight access check.

Runs inside the browser-fetch flow only. The cascade handlers use our
own API keys and don't need institutional access; only the browser
flow benefits from knowing up-front whether the library can actually
reach the PDF.

An SFX / OpenURL response enumerates `<target>` elements describing
how the library is configured to reach a given DOI. A target with
`<service_type>getFullTxt</service_type>` means the library has a
licensed path to the full text. Zero such targets means the library
has no full-text route — the browser handler would certainly fail, so
we skip the item without opening Chromium.

Usage:
    from fetchers.library_resolver import has_fulltext_access,
        SfxCache, LibraryResolverConfig

    cfg = LibraryResolverConfig(
        openurl_base="https://sfx.finna.fi/nelli09",
        session=requests_session,
        cache=SfxCache(cache_dir),
    )
    if not has_fulltext_access("10.1111/j.1460-2466.1993.tb01304.x", cfg):
        # skip this item, log as skipped_no_library_coverage
        ...
"""

from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlencode, urlparse

if TYPE_CHECKING:
    import requests

logger = logging.getLogger(__name__)


# Default priority order for full-text platforms when SFX offers
# several routes for one DOI. Higher-ranked (earlier) entries win.
#
# Ranking rationale:
#   - EBSCOhost: cleanest PDFs in our testing.
#   - Publisher-direct (Elsevier/Wiley/Springer/Sage/T&F/OUP): also
#     clean. When offered alongside EBSCOhost, platform choice rarely
#     matters — prefer EBSCOhost for the UI the Zotero Connector
#     translator handles most consistently.
#   - JSTOR: adds a JSTOR-branded cover page.
#   - ProQuest: sometimes serves a scanned-image PDF where another
#     route has a digitally-typeset original. Last resort.
#
# Users can override via `[library] sfx_platform_priority` in config.
SFX_PLATFORM_PRIORITY: tuple[str, ...] = (
    "ebscohost.com",
    "ebsco.com",
    "sciencedirect.com",
    "onlinelibrary.wiley.com",
    "link.springer.com",
    "journals.sagepub.com",
    "tandfonline.com",
    "academic.oup.com",
    "jstor.org",
    "proquest.com",
)

# OpenURL 1.0 query parameters we send to every SFX request. The DOI
# goes in `rft_id=info:doi/<DOI>`. `sfx.response_type=multi_obj_xml`
# makes SFX emit the XML shape we parse below.
_OPENURL_STATIC_PARAMS: dict[str, str] = {
    "url_ver": "Z39.88-2004",
    "ctx_ver": "Z39.88-2004",
    "ctx_enc": "info:ofi/enc:UTF-8",
    "url_ctx_fmt": "info:ofi/fmt:kev:mtx:ctx",
    "svc_val_fmt": "info:ofi/fmt:kev:mtx:sch_svc",
    "sfx.response_type": "multi_obj_xml",
}

# SFX's service_type value for "this target serves the full text (PDF/HTML)".
# Other service types (getHolding, getAuthor, getDOI, getWebSearch, ...)
# don't imply access.
_FULLTEXT_SERVICE_TYPE = "getFullTxt"

# Timeout for a single SFX request. SFX is usually snappy (sub-second)
# but can stall on slow targets; cap so we don't block a whole batch.
_DEFAULT_TIMEOUT_S = 10


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class SfxCache:
    """On-disk DOI → {has_access: bool, targets: int} cache.

    The cache lives alongside the PDF cache directory so clearing the
    cache (delete the directory) also clears the SFX cache. Stale
    entries are unlikely to cause harm — if a library adds a new
    subscription, the worst case is that we keep skipping a DOI the
    user could now reach; a fresh run after deleting the cache picks
    up the change.
    """

    def __init__(self, cache_dir: str | Path) -> None:
        self.path = Path(cache_dir) / "sfx_cache.json"
        self._data: dict[str, dict] = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text())
            except Exception:
                # Corrupt cache — start fresh, don't fail the whole run.
                self._data = {}

    def get(self, doi: str) -> dict | None:
        return self._data.get(doi)

    def put(self, doi: str, value: dict) -> None:
        self._data[doi] = value
        # Best-effort write — don't crash the pipeline on a filesystem hiccup.
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, indent=1))
        except Exception as e:
            logger.debug("SfxCache write failed: %s", e)


# ---------------------------------------------------------------------------
# Config — passed to each has_fulltext_access call.
# ---------------------------------------------------------------------------


@dataclass
class LibraryResolverConfig:
    """Parameters the resolver needs to run.

    `openurl_base` is the library's SFX / OpenURL endpoint, configured
    under `[library] openurl_base` in config.toml. When unset, callers
    should skip the pre-flight entirely.
    """

    openurl_base: str
    session: requests.Session
    cache: SfxCache | None = None
    timeout_s: int = _DEFAULT_TIMEOUT_S
    # Source identifier included in the OpenURL request. Helps libraries
    # correlate resolver traffic to the plugin. Not required.
    sid: str = "academic-research"


# ---------------------------------------------------------------------------
# Core — query + parse
# ---------------------------------------------------------------------------


def _build_query_url(
    doi: str,
    cfg: LibraryResolverConfig,
    *,
    ignore_date_threshold: bool = False,
) -> str:
    """Build the OpenURL query URL for `doi`.

    When `ignore_date_threshold=True`, appends `sfx.ignore_date_threshold=1`
    so SFX returns every publisher it knows for the journal, not only
    those whose coverage includes this DOI's year. Used by the dual
    query that distinguishes "library has no Wiley at all" from
    "library has Wiley but not this year".
    """
    params = dict(_OPENURL_STATIC_PARAMS)
    params["rft_id"] = f"info:doi/{doi}"
    params["sfx.sid"] = cfg.sid
    if ignore_date_threshold:
        params["sfx.ignore_date_threshold"] = "1"
    return f"{cfg.openurl_base}?{urlencode(params)}"


def _local_name(el: ET.Element) -> str:
    """Element tag without XML namespace prefix."""
    tag = el.tag
    return tag.rpartition("}")[2] if "}" in tag else tag


def _fulltext_target_urls(xml_text: str) -> list[str] | None:
    """Every `<target_url>` that accompanies a `<service_type>getFullTxt</...>`
    in the SFX response.

    Returns a list (possibly empty) on success, None on parse failure —
    callers distinguish "no access" from "couldn't parse" by None.

    SFX nests `<target_url>` inside a `<target>` element that also
    contains `<service_type>`. We iterate any element that might be a
    target container and emit the pair when the service_type matches.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.debug("SFX XML parse failed: %s", e)
        return None

    urls: list[str] = []
    # Walk every element; treat any node that has BOTH a <service_type>
    # child saying "getFullTxt" AND a <target_url> child as a full-text
    # target. This is robust to variations in how deep `<target>` lives
    # inside the response (SFX wraps things in `<targets>` or `<target_set>`
    # depending on version).
    for el in root.iter():
        if _local_name(el) != "target":
            continue
        service = None
        target_url = None
        for child in el:
            name = _local_name(child)
            if name == "service_type" and (child.text or "").strip() == _FULLTEXT_SERVICE_TYPE:
                service = _FULLTEXT_SERVICE_TYPE
            elif name == "target_url":
                target_url = (child.text or "").strip()
        if service == _FULLTEXT_SERVICE_TYPE and target_url:
            urls.append(target_url)
    return urls


def _count_fulltext_targets(xml_text: str) -> int:
    """Back-compat wrapper: returns the count of full-text targets, or
    -1 when parsing failed. Kept because unit tests pin this shape."""
    urls = _fulltext_target_urls(xml_text)
    return -1 if urls is None else len(urls)


def _effective_host(target_url: str) -> str:
    """Hostname the target URL actually takes the user to, unwrapping
    EZproxy wrappers like `http://ezproxy.jyu.fi/login?url=<real>`.

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


def _target_matches_domains(target_url: str, domains: tuple[str, ...]) -> bool:
    """True when the target URL's effective host ends with any of the
    given domain suffixes.  Suffix-match so "wiley.com" matches
    "onlinelibrary.wiley.com"."""
    host = _effective_host(target_url)
    if not host:
        return False
    for d in domains:
        d = d.lower()
        if host == d or host.endswith("." + d):
            return True
    return False


def _query_target_urls(
    doi: str,
    cfg: LibraryResolverConfig,
    *,
    ignore_date_threshold: bool = False,
) -> list[str] | None:
    """Run one SFX query for `doi` and return the full-text target URL list.

    Returns the URL list on success (possibly empty), or None on
    transport / non-200 / parse failure. Callers distinguish the
    unknown case (None → fail-open) from the known-empty case ([]).

    Results are cached per `(doi, ignore_date_threshold)`. The cache
    value shape is `{"urls": [list of strings]}` — derived quantities
    (has_access bool, preferred target) are computed by the callers so
    the same cached payload can serve handlers with different
    direct-access domains.
    """
    if cfg.cache is not None:
        cached = cfg.cache.get(_cache_key(doi, ignore_date_threshold))
        if cached is not None and "urls" in cached:
            return list(cached["urls"])

    url = _build_query_url(
        doi, cfg, ignore_date_threshold=ignore_date_threshold,
    )
    try:
        resp = cfg.session.get(url, timeout=cfg.timeout_s)
    except Exception as e:
        logger.debug("SFX request failed for %s: %s", doi, e)
        return None

    if resp.status_code != 200:
        logger.debug("SFX returned HTTP %d for %s", resp.status_code, doi)
        return None

    urls = _fulltext_target_urls(resp.text)
    if urls is None:
        return None

    if cfg.cache is not None:
        cfg.cache.put(_cache_key(doi, ignore_date_threshold), {"urls": urls})
    return urls


def has_fulltext_access(
    doi: str,
    cfg: LibraryResolverConfig,
    *,
    required_domains: tuple[str, ...] = (),
) -> bool:
    """True if the library has at least one full-text route for this DOI.

    When `required_domains` is empty, any full-text target counts as
    access (legacy behaviour, useful when the caller doesn't care
    which platform hosts the PDF).

    When `required_domains` is non-empty, only full-text targets whose
    effective URL host matches one of the domains count. Callers that
    know their handler can only reach a specific publisher domain
    (e.g. `InformsHandler` only knows `pubsonline.informs.org`) pass
    their direct-access domains here so SFX-reported EBSCOhost/JSTOR
    targets don't create a false positive.

    Fail-open semantics: any transport error, parse error, or unset
    config returns True (i.e. "proceed, the handler may still work").
    The whole point of this pre-flight is to SKIP hopeless items; when
    the signal is ambiguous we lean toward letting the handler try.
    """
    if not cfg.openurl_base:
        return True

    urls = _query_target_urls(doi, cfg)
    if urls is None:
        # Query failed → unknown → fail-open.
        return True

    if required_domains:
        return any(
            _target_matches_domains(u, required_domains) for u in urls
        )
    return bool(urls)


# ---------------------------------------------------------------------------
# Dual query — the two SFX lookups that distinguish the three routing
# cases (library has no relationship | library has publisher but year
# out of range | library covers this DOI now). Callers diff `in_range`
# against `any_range` to classify.
# ---------------------------------------------------------------------------


@dataclass
class SfxDualResult:
    """Result of two SFX queries per DOI.

    - `in_range`: target URLs returned by the default (date-filtered)
      query — publishers whose coverage range actually includes this
      DOI. These are the routes the library can unlock right now.
    - `any_range`: target URLs returned with `sfx.ignore_date_threshold=1`
      — every publisher SFX knows has this journal, regardless of
      whether coverage reaches this DOI's year. Always a superset of
      `in_range`.
    - `query_ok`: False if either SFX call failed. Callers may still
      see partial data but should lean toward fail-open.
    """

    in_range: list[str]
    any_range: list[str]
    query_ok: bool = True


def sfx_lookup_dual(
    doi: str, cfg: LibraryResolverConfig,
) -> SfxDualResult:
    """Run both SFX queries (date-filtered + ignore-date) and return
    the target URL lists together.

    Each call is cached independently per `(doi, ignore_date_threshold)`
    — expected to be a cache hit on every run after the first per-DOI
    pair. On the first run, cost is ~2 × 1s per DOI.
    """
    if not cfg.openurl_base:
        return SfxDualResult(in_range=[], any_range=[], query_ok=False)

    in_range = _query_target_urls(doi, cfg, ignore_date_threshold=False)
    any_range = _query_target_urls(doi, cfg, ignore_date_threshold=True)
    query_ok = in_range is not None and any_range is not None
    return SfxDualResult(
        in_range=in_range or [],
        any_range=any_range or [],
        query_ok=query_ok,
    )


# ---------------------------------------------------------------------------
# Preferred target selection — when SFX offers several full-text
# routes, pick the one whose platform we've found most reliable for
# automated saves.
# ---------------------------------------------------------------------------


def _platform_rank(url: str, priority: tuple[str, ...]) -> int:
    """Rank for `url`: index into `priority` (lower = better). URLs whose
    effective host doesn't match any priority entry return len(priority)
    — they lose the tie-break to any ranked platform but still beat
    "no target at all"."""
    host = _effective_host(url)
    if not host:
        return len(priority)
    for i, dom in enumerate(priority):
        dom = dom.lower()
        if host == dom or host.endswith("." + dom):
            return i
    return len(priority)


def first_fulltext_target_preferred(
    doi: str,
    cfg: LibraryResolverConfig,
    *,
    priority: tuple[str, ...] = SFX_PLATFORM_PRIORITY,
    in_range_only: bool = True,
    required_domains: tuple[str, ...] = (),
) -> str | None:
    """Return one SFX full-text target URL for `doi`, picking the
    highest-priority platform.

    - `in_range_only=True` (default): use the date-filtered query —
      platforms that can actually unlock this DOI. Set False to use
      the ignore-date query (informs the Case-2 skip decision, rarely
      the right choice for handing a URL to a downloader).
    - `required_domains`: when non-empty, restrict candidates to
      targets whose effective host matches one of these domains.
      Empty means "any platform".

    Ranking uses `priority` (default `SFX_PLATFORM_PRIORITY`). Ties
    broken by SFX's response order (stable — first in list wins).
    Returns None when no target matches.
    """
    if not cfg.openurl_base:
        return None

    urls = _query_target_urls(
        doi, cfg, ignore_date_threshold=not in_range_only,
    )
    if not urls:
        return None

    if required_domains:
        urls = [u for u in urls if _target_matches_domains(u, required_domains)]
        if not urls:
            return None

    # Stable sort: same rank keeps SFX's response order.
    return min(urls, key=lambda u: _platform_rank(u, priority))


def _cache_key(doi: str, ignore_date_threshold: bool = False) -> str:
    """Cache key combining DOI and the ignore-date-threshold flag.

    When `ignore_date_threshold=False` (the default date-filtered
    query) the key is just `doi`, so existing cache entries written
    by v0.3.x keep the same key and earlier tests' `c.put("10.1/x", …)`
    calls still collide with the canonical Query-B key — the test
    shape doesn't change.
    """
    if ignore_date_threshold:
        return f"{doi}::any"
    return doi


# ---------------------------------------------------------------------------
# Config loader — turns `[library]` in config.toml into a concrete
# resolver config, or None when the user hasn't set it up.
# ---------------------------------------------------------------------------


def load_from_config(
    session: requests.Session,
    cache_dir: str | Path | None = None,
) -> LibraryResolverConfig | None:
    """Build a resolver config from `[library] openurl_base` in config.toml.

    Returns None when the config key is absent — callers MUST treat
    None as "no pre-flight, fall through to the handler directly".
    """
    from core.config_loader import get

    base = get("library", "openurl_base").strip()
    if not base:
        return None
    cache = SfxCache(cache_dir) if cache_dir else None
    return LibraryResolverConfig(
        openurl_base=base,
        session=session,
        cache=cache,
    )
