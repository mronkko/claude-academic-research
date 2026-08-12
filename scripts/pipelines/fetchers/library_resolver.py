"""Library link-resolver (SFX / OpenURL, and Ex Libris Alma `uresolver`)
pre-flight access check.

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

Alma/Primo institutions (the majority of academic libraries today)
don't expose an SFX-shaped OpenURL endpoint — their public Primo
openurl path redirects to the HTML discovery UI. Alma's `uresolver`
endpoint (`https://<host>.alma.exlibrisgroup.com/view/uresolver/
<inst_code>/openurl`) answers the same `getFullTxt` question in XML,
just with a different element shape: `<context_service
service_type="getFullTxt">` (an attribute, not a child element) with
the resolvable link in a sibling `<resolution_url>` rather than SFX's
`<target_url>`. Both shapes are recognized by `_fulltext_target_urls`
below without needing to know in advance which one a response uses;
only the query builder (`_build_query_url`) needs to know, since Alma
requires an extra `svc_dat=CTO` param that SFX doesn't use.

Finding your own institution's `openurl_base`:
    - SFX: your library or its existing OpenURL/citation-manager
      documentation usually has this already (often the base URL
      handed to EndNote/RefWorks/Zotero's "institutional proxy").
    - Alma: open a Primo VE "Get it"/"View it" link for any item and
      read the outbound request in your browser's Network tab — it's
      the `.../view/uresolver/<inst_code>/openurl` URL up to the `?`.
      This URL is routinely shared for third-party integrations
      (LibKey Nomad, Lean Library, browser extensions), so your
      library's systems/electronic-resources staff can usually just
      hand it to you.

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
# don't imply access. Alma's uresolver reuses the same value, as the
# service_type of a <context_service> element instead of a <target>'s
# <service_type> child.
_FULLTEXT_SERVICE_TYPE = "getFullTxt"

# Alma's uresolver requires this to return the getFullTxt service
# category as XML; without it, Alma serves its HTML discovery skin
# instead (HTTP 200, but not parseable — see _build_query_url). SFX
# ignores the param harmlessly, so it's only added when the configured
# base looks like an Alma uresolver URL.
_ALMA_SVC_DAT = "CTO"

# Timeout for a single SFX/Alma request. Usually snappy (sub-second)
# but can stall on slow targets; cap so we don't block a whole batch.
_DEFAULT_TIMEOUT_S = 10


def _is_alma_uresolver(openurl_base: str) -> bool:
    """True when `openurl_base` looks like an Ex Libris Alma `uresolver`
    endpoint rather than an SFX one.

    `/view/uresolver/` is a fixed Alma product path, not something an
    institution configures differently, so this is reliable without an
    extra network round-trip to probe the response shape.
    """
    return "/view/uresolver/" in openurl_base


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
    if _is_alma_uresolver(cfg.openurl_base):
        params["svc_dat"] = _ALMA_SVC_DAT
    return f"{cfg.openurl_base}?{urlencode(params)}"


def _build_issn_query_url(
    cfg: LibraryResolverConfig,
    *,
    issn: str,
    pub_date: str | None = None,
    volume: str | None = None,
    ignore_date_threshold: bool = False,
) -> str:
    """Alma-only fallback query keyed by journal identity instead of a
    DOI (see BACKLOG.md P11 / `_query_target_urls`'s fallback logic).

    Omits `rft_id` entirely — some Alma deployments only link holdings
    at journal level and return nothing for a DOI-keyed query even
    when they license the journal, so re-asking by ISSN (+ date/volume
    when known) is a genuinely different query, not a variant of the
    DOI one.
    """
    params = dict(_OPENURL_STATIC_PARAMS)
    params["rft.issn"] = issn
    if pub_date:
        params["rft.date"] = pub_date
    if volume:
        params["rft.volume"] = volume
    params["sfx.sid"] = cfg.sid
    if ignore_date_threshold:
        params["sfx.ignore_date_threshold"] = "1"
    params["svc_dat"] = _ALMA_SVC_DAT
    return f"{cfg.openurl_base}?{urlencode(params)}"


def _local_name(el: ET.Element) -> str:
    """Element tag without XML namespace prefix."""
    tag = el.tag
    return tag.rpartition("}")[2] if "}" in tag else tag


def _fulltext_target_urls(xml_text: str) -> list[str] | None:
    """Every full-text target URL in the response.

    Returns a list (possibly empty) on success, None on parse failure —
    callers distinguish "no access" from "couldn't parse" by None.

    Recognizes two response shapes in the same walk, since the caller
    doesn't know in advance which one a given `openurl_base` returns:

    - SFX nests `<target_url>` inside a `<target>` element that also
      contains `<service_type>`. We iterate any element that might be
      a target container and emit the pair when the service_type
      matches. This is robust to variations in how deep `<target>`
      lives inside the response (SFX wraps things in `<targets>` or
      `<target_set>` depending on version).
    - Alma's `uresolver` marks `<context_service service_type=
      "getFullTxt">` as an XML attribute (not a child element) and
      carries the resolvable link in a sibling `<resolution_url>`
      rather than a `<target_url>` child.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.debug("SFX/Alma XML parse failed: %s", e)
        return None

    urls: list[str] = []
    for el in root.iter():
        name = _local_name(el)
        if name == "target":
            service = None
            target_url = None
            for child in el:
                child_name = _local_name(child)
                if child_name == "service_type" and (child.text or "").strip() == _FULLTEXT_SERVICE_TYPE:
                    service = _FULLTEXT_SERVICE_TYPE
                elif child_name == "target_url":
                    target_url = (child.text or "").strip()
            if service == _FULLTEXT_SERVICE_TYPE and target_url:
                urls.append(target_url)
        elif name == "context_service" and el.get("service_type") == _FULLTEXT_SERVICE_TYPE:
            for child in el:
                if _local_name(child) == "resolution_url":
                    resolution_url = (child.text or "").strip()
                    if resolution_url:
                        urls.append(resolution_url)
                    break
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


def _fetch_and_parse(url: str, cfg: LibraryResolverConfig, doi: str) -> list[str] | None:
    """GET `url` and parse it as an SFX/Alma response. None on
    transport / non-200 / parse failure; `doi` is only for logging."""
    try:
        resp = cfg.session.get(url, timeout=cfg.timeout_s)
    except Exception as e:
        logger.debug("SFX/Alma request failed for %s: %s", doi, e)
        return None

    if resp.status_code != 200:
        logger.debug("SFX/Alma returned HTTP %d for %s", resp.status_code, doi)
        return None

    return _fulltext_target_urls(resp.text)


def _query_target_urls(
    doi: str,
    cfg: LibraryResolverConfig,
    *,
    ignore_date_threshold: bool = False,
    issn: str | None = None,
    pub_date: str | None = None,
    volume: str | None = None,
) -> list[str] | None:
    """Run one SFX/Alma query for `doi` and return the full-text target
    URL list.

    Returns the URL list on success (possibly empty), or None on
    transport / non-200 / parse failure. Callers distinguish the
    unknown case (None → fail-open) from the known-empty case ([]).

    Alma fallback (BACKLOG.md P11): when the DOI-keyed query comes
    back empty and `issn` is given, retries once against an Alma
    endpoint using `rft.issn`/`rft.date`/`rft.volume` instead of the
    DOI. Some Alma deployments only link holdings to a journal record,
    not to individual DOIs, so a DOI-only query can under-report even
    when the library licenses the journal. Not attempted for SFX
    (`issn` is simply ignored there) or when the primary query already
    found something.

    Results are cached per `(doi, ignore_date_threshold)` regardless
    of whether the primary or the fallback query produced them — the
    cache answers "does the library have this DOI", not "which query
    strategy worked". The cache value shape is `{"urls": [list of
    strings]}` — derived quantities (has_access bool, preferred
    target) are computed by the callers so the same cached payload can
    serve handlers with different direct-access domains.
    """
    cache_key = _cache_key(doi, ignore_date_threshold)
    if cfg.cache is not None:
        cached = cfg.cache.get(cache_key)
        if cached is not None and "urls" in cached:
            return list(cached["urls"])

    url = _build_query_url(doi, cfg, ignore_date_threshold=ignore_date_threshold)
    urls = _fetch_and_parse(url, cfg, doi)

    if urls == [] and issn and _is_alma_uresolver(cfg.openurl_base):
        fallback_url = _build_issn_query_url(
            cfg, issn=issn, pub_date=pub_date, volume=volume,
            ignore_date_threshold=ignore_date_threshold,
        )
        fallback_urls = _fetch_and_parse(fallback_url, cfg, doi)
        if fallback_urls:
            urls = fallback_urls

    if urls is None:
        return None

    if cfg.cache is not None:
        cfg.cache.put(cache_key, {"urls": urls})
    return urls


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

    When `required_domains` is empty, any full-text target counts as
    access (legacy behaviour, useful when the caller doesn't care
    which platform hosts the PDF).

    When `required_domains` is non-empty, only full-text targets whose
    effective URL host matches one of the domains count. Callers that
    know their handler can only reach a specific publisher domain
    (e.g. `InformsHandler` only knows `pubsonline.informs.org`) pass
    their direct-access domains here so SFX-reported EBSCOhost/JSTOR
    targets don't create a false positive.

    `issn`/`pub_date`/`volume` are optional and only matter for Alma
    endpoints — see `_query_target_urls`'s fallback logic (BACKLOG.md
    P11).

    Fail-open semantics: any transport error, parse error, or unset
    config returns True (i.e. "proceed, the handler may still work").
    The whole point of this pre-flight is to SKIP hopeless items; when
    the signal is ambiguous we lean toward letting the handler try.
    """
    if not cfg.openurl_base:
        return True

    urls = _query_target_urls(
        doi, cfg, issn=issn, pub_date=pub_date, volume=volume,
    )
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
    *,
    issn: str | None = None,
    pub_date: str | None = None,
    volume: str | None = None,
) -> SfxDualResult:
    """Run both SFX queries (date-filtered + ignore-date) and return
    the target URL lists together.

    Each call is cached independently per `(doi, ignore_date_threshold)`
    — expected to be a cache hit on every run after the first per-DOI
    pair. On the first run, cost is ~2 × 1s per DOI.

    `issn`/`pub_date`/`volume` are optional and only matter for Alma
    endpoints — see `_query_target_urls`'s fallback logic (BACKLOG.md
    P11).
    """
    if not cfg.openurl_base:
        return SfxDualResult(in_range=[], any_range=[], query_ok=False)

    in_range = _query_target_urls(
        doi, cfg, ignore_date_threshold=False,
        issn=issn, pub_date=pub_date, volume=volume,
    )
    any_range = _query_target_urls(
        doi, cfg, ignore_date_threshold=True,
        issn=issn, pub_date=pub_date, volume=volume,
    )
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
    issn: str | None = None,
    pub_date: str | None = None,
    volume: str | None = None,
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
    - `issn`/`pub_date`/`volume`: optional, only matter for Alma
      endpoints — see `_query_target_urls`'s fallback logic
      (BACKLOG.md P11).

    Ranking uses `priority` (default `SFX_PLATFORM_PRIORITY`). Ties
    broken by SFX's response order (stable — first in list wins).
    Returns None when no target matches.
    """
    if not cfg.openurl_base:
        return None

    urls = _query_target_urls(
        doi, cfg, ignore_date_threshold=not in_range_only,
        issn=issn, pub_date=pub_date, volume=volume,
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

    base = get("library", "openurl_base", env="LIBRARY_OPENURL_BASE").strip()
    if not base:
        return None
    cache = SfxCache(cache_dir) if cache_dir else None
    return LibraryResolverConfig(
        openurl_base=base,
        session=session,
        cache=cache,
    )
