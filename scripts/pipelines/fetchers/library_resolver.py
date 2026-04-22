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


def _build_query_url(doi: str, cfg: LibraryResolverConfig) -> str:
    params = dict(_OPENURL_STATIC_PARAMS)
    params["rft_id"] = f"info:doi/{doi}"
    params["sfx.sid"] = cfg.sid
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

    cache_key = _cache_key(doi, required_domains)
    if cfg.cache is not None:
        cached = cfg.cache.get(cache_key)
        if cached is not None:
            return bool(cached.get("has_access", True))

    url = _build_query_url(doi, cfg)
    try:
        resp = cfg.session.get(url, timeout=cfg.timeout_s)
    except Exception as e:
        logger.debug("SFX request failed for %s: %s", doi, e)
        return True

    if resp.status_code != 200:
        logger.debug("SFX returned HTTP %d for %s", resp.status_code, doi)
        return True

    target_urls = _fulltext_target_urls(resp.text)
    if target_urls is None:
        # Parse failure → unknown → fail-open.
        return True

    if required_domains:
        matching = [u for u in target_urls
                    if _target_matches_domains(u, required_domains)]
        has_access = bool(matching)
        target_count = len(matching)
    else:
        has_access = bool(target_urls)
        target_count = len(target_urls)

    if cfg.cache is not None:
        cfg.cache.put(cache_key, {
            "has_access": has_access,
            "targets": target_count,
        })
    return has_access


def _cache_key(doi: str, required_domains: tuple[str, ...]) -> str:
    """Cache key combining DOI and the domain filter, so the same DOI
    can have separate cached answers when queried with different
    required_domains (one per handler)."""
    if not required_domains:
        return doi
    domains = "|".join(sorted(d.lower() for d in required_domains))
    return f"{doi}::{domains}"


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
