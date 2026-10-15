"""Which version of the article a PDF is — and the `pdf:version:*` tag.

Every PDF the pipeline attaches gets exactly one of

    pdf:version:published   the publisher's version of record
    pdf:version:accepted    the author's accepted manuscript (post peer review)
    pdf:version:submitted   a preprint / working paper (before peer review)

Global, like every `pdf:*` tag: it states a fact about the attachment,
not a review's judgement, so it sits outside `TAG_PREFIX`. It is
additive — `pdf:repository-copy`, `pdf:preprint-version` and
`pdf:tdm-recovered` stay as they were, because skills and existing
libraries read them.

**The rule is "never over-claim".** Where the evidence is weak the tag
goes *down* (accepted, not published), because the planned
`[pdf] min_version` policy will gate on it, and a manuscript wrongly
tagged `published` would pass a "version of record only" review
unnoticed while one wrongly tagged `accepted` only costs a re-fetch.

How the version is decided, per source:

- **Publisher APIs and platforms → published.** ScienceDirect, Springer,
  Wiley TDM, the paid OpenAlex Content API, every browser handler (each
  drives a publisher platform, or EBSCOhost, whose PDFs are the
  publisher's typeset file or a scan of the printed article).
  ScienceDirect's XML recovery is `published` too: the text is the
  version of record's, re-typeset — `pdf:tdm-recovered` already carries
  the "not the publisher's layout" warning, and `[pdf] reconstructed` is
  a separate axis in the plan.
- **Crossref TDM → its link's `content-version`**: `am` → accepted,
  anything else (`vor`, `unspecified`) → published. The link is the
  publisher's own deposit for its own content.
- **PMC → split on the NCBI ID converter's `mid`.** PMC also hosts NIH /
  Europe PMC *author manuscripts*; the converter (with `versions=yes`)
  gives those a manuscript id (`NIHMS…`) on the current version and
  omits it for publisher deposits. Verified 2026-10-15 on two NIHMS
  records and one publisher deposit.
- **arXiv and the `preprint` source → submitted.**
- **CORE / OpenAIRE / BASE → accepted** (they already tag
  `pdf:repository-copy`, and the suffix in their cache name decides).
- **Unpaywall / OpenAlex OA → `from_oa_location`**, the measured rule
  below. **Semantic Scholar** reports no version at all: a preprint-server
  URL → submitted, anything else → accepted.
- **Zotero Connector → `for_connector_target`**: published when the
  resolver route is a known publisher/aggregator platform or the DOI
  landing page, accepted for anything else (free lists, repositories,
  routes the plugin cannot identify).

**The measurement behind `from_oa_location`** (2026-10-15, 68 located
PDFs whose true version was read off the file; full write-up in
`~/.claude/plans/pdf-retrieval.md`, "Measurement: version-field
reliability"). Unpaywall and OpenAlex agreed on all 68 — Unpaywall is
served from OpenAlex data, so they are one witness, not two.

    publishedVersion on a publisher/journal host   17/17 published
    publishedVersion on a repository                0/1  (an arXiv copy)
    acceptedVersion                                 4/10 (5 were preprints
                                                         on Research Square
                                                         and bioRxiv)
    submittedVersion                               11/40 (18 were the
                                                         typeset publisher
                                                         PDF in a repository)

So only `publishedVersion` + publisher/journal host is trusted. A
preprint-server URL is `submitted` whatever the label says; everything
else is `accepted`. On the sample that rule over-claimed once in 67 (an
IZA working paper on EconStor called accepted), never as `published`,
and under-claimed 21 times — mostly publisher PDFs sitting in
repositories, which only a content check could recover.

**Surviving the cache.** Most API sources share `<doi>.pdf`, so the
filename cannot carry the version the way `-repository-copy` carries
provenance. A sidecar `<pdf>.version.json` records it instead, with the
file's size and MD5: a sidecar describing different bytes (the file was
overwritten by another source) is ignored, never trusted. A cached file
with no valid sidecar — anything downloaded before this module existed —
is `accepted` (provenance unknown), except an arXiv DOI (`submitted`).

Stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import urllib.parse
from pathlib import Path

logger = logging.getLogger(__name__)

PUBLISHED = "published"
ACCEPTED = "accepted"
SUBMITTED = "submitted"

#: Highest first. The order is the one `[pdf] min_version` will compare on.
VERSIONS: tuple[str, ...] = (PUBLISHED, ACCEPTED, SUBMITTED)

TAG_FAMILY = "pdf:version:"


def tag_for(version: str) -> str:
    """`pdf:version:<version>`. Raises on an unknown version: a typo here
    would mint a tag no reader looks for."""
    if version not in VERSIONS:
        raise ValueError(f"unknown PDF version {version!r}")
    return f"{TAG_FAMILY}{version}"


VERSION_TAGS: tuple[str, ...] = tuple(tag_for(v) for v in VERSIONS)

#: Sources whose version is fixed by what they are. Sources missing here
#: either decide per file (`API_FIELD_SOURCES`) or are unknown, which
#: falls to `ACCEPTED`.
STATIC_SOURCE_VERSION: dict[str, str] = {
    "sciencedirect": PUBLISHED,
    "springer": PUBLISHED,
    "wiley": PUBLISHED,
    "openalex_content": PUBLISHED,
    # Stage 4 in the cascade's own registry (`BrowserSource`).
    "browser": PUBLISHED,
    "arxiv": SUBMITTED,
    "preprint": SUBMITTED,
    "core": ACCEPTED,
    "openaire": ACCEPTED,
    "base": ACCEPTED,
}

#: Sources that read the version off their API's answer and `record` it
#: themselves at download time.
API_FIELD_SOURCES: frozenset[str] = frozenset({
    "crossref", "pubmed_central", "unpaywall", "openalex", "semantic_scholar",
})

#: Every publisher handler in `fetchers/browser/` — and EBSCOhost — drives
#: the platform's own PDF. Passed as `static` by the browser pass.
BROWSER_HANDLER_VERSION = PUBLISHED

#: Hosts that serve preprints / working papers. Wider than
#: `preprint.PREPRINT_HOSTS`, which lists only what that source fetches
#: from: this one has to recognise a preprint whoever found it. Research
#: Square and bioRxiv are here because the measurement caught the OA
#: metadata calling their copies `acceptedVersion`.
PREPRINT_HOSTS: tuple[str, ...] = (
    "arxiv.org", "biorxiv.org", "medrxiv.org", "researchsquare.com",
    "ssrn.com", "osf.io", "psyarxiv.com", "preprints.org", "chemrxiv.org",
    "repec.org", "socarxiv.org", "edarxiv.org", "authorea.com",
)

#: DOI prefixes registered by preprint servers. `10.1101` (bioRxiv) is
#: deliberately absent: Cold Spring Harbor's peer-reviewed journals share
#: it.
PREPRINT_DOI_PREFIXES: tuple[str, ...] = (
    "10.48550/",   # arXiv
    "10.2139/",    # SSRN
    "10.21203/",   # Research Square
    "10.31234/",   # PsyArXiv
    "10.31235/",   # SocArXiv
    "10.31219/",   # OSF Preprints
    "10.20944/",   # Preprints.org
    "10.26434/",   # ChemRxiv
)


def _host(url: str) -> str:
    try:
        return (urllib.parse.urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""


def is_preprint_url(url: str) -> bool:
    """True when `url` is on a preprint server (suffix match on the host,
    so `export.arxiv.org` counts and `arxiv.org.example.com` does not)."""
    host = _host(url)
    return bool(host) and any(
        host == d or host.endswith("." + d) for d in PREPRINT_HOSTS
    )


def is_preprint_doi(doi: str) -> bool:
    """True when `doi` was registered by a preprint server."""
    low = (doi or "").strip().lower()
    return any(low.startswith(p) for p in PREPRINT_DOI_PREFIXES)


# ---------------------------------------------------------------------------
# Per-source decisions from API fields
# ---------------------------------------------------------------------------

#: Host types under which `publishedVersion` measured reliable. Unpaywall
#: says `publisher`, OpenAlex says `journal` (its source type).
_PUBLISHER_HOST_TYPES = frozenset({"publisher", "journal"})


def from_oa_location(*, version: str | None, host_type: str | None, url: str) -> str:
    """The version of one Unpaywall / OpenAlex OA location. See the module
    docstring for the measurement this rule rests on."""
    if is_preprint_url(url):
        return SUBMITTED
    if version == "publishedVersion" and (host_type or "") in _PUBLISHER_HOST_TYPES:
        return PUBLISHED
    return ACCEPTED


def from_unpaywall_location(loc: dict | None, url: str) -> str:
    loc = loc or {}
    return from_oa_location(
        version=loc.get("version"), host_type=loc.get("host_type"), url=url,
    )


def from_openalex_location(loc: dict | None, url: str) -> str:
    loc = loc or {}
    return from_oa_location(
        version=loc.get("version"),
        host_type=(loc.get("source") or {}).get("type"),
        url=url,
    )


def from_semantic_scholar(url: str) -> str:
    """S2's `openAccessPdf` carries no version. Not measured, so nothing
    beyond the URL is trusted."""
    return SUBMITTED if is_preprint_url(url) else ACCEPTED


def from_crossref_link(content_version: str | None) -> str:
    """A Crossref TDM link's `content-version`: `am` is the accepted
    manuscript; `vor` and `unspecified` are the publisher's own file."""
    return ACCEPTED if (content_version or "").strip().lower() == "am" else PUBLISHED


def from_pmc_record(record: dict | None) -> str:
    """Accepted when the NCBI ID converter's current version carries a
    manuscript id (`mid`, e.g. `NIHMS1865295`), published otherwise.

    Needs the converter queried with `versions=yes`. Without the
    `versions` list at all the answer is unknown, and unknown is
    `ACCEPTED`.
    """
    record = record or {}
    if record.get("mid"):
        return ACCEPTED
    versions = record.get("versions")
    if not isinstance(versions, list) or not versions:
        return ACCEPTED
    current = [v for v in versions if isinstance(v, dict) and v.get("current")]
    chosen = current[-1] if current else versions[-1]
    if not isinstance(chosen, dict):
        return ACCEPTED
    return ACCEPTED if chosen.get("mid") else PUBLISHED


def _platform_hosts(url: str) -> list[str]:
    """Hosts a resolver URL may name: its effective host, plus the one an
    EZproxy hostname-rewriting proxy hides (`research-ebsco-com.ezproxy.x`
    → `research.ebsco.com`)."""
    from fetchers.resolvers.base import effective_host

    hosts = []
    host = effective_host(url or "")
    if host:
        hosts.append(host)
        labels = host.split(".")
        if "ezproxy" in labels[1:]:
            hosts.append(labels[0].replace("-", "."))
    return hosts


def for_connector_target(target, url: str, doi: str) -> str:
    """The version the Zotero Connector will save for this route.

    `target` is the resolver's `FulltextTarget` (None when the run fell
    open to the DOI landing page), `url` the URL actually handed over.

    Published only when the route is recognisably a publisher or
    aggregator platform (`PLATFORM_PRIORITY`) or the DOI landing page,
    which is the publisher's. Anything else — a free-list target, a
    repository, an Alma route whose platform name matches nothing — is
    accepted: the Connector saves whatever that page offers.
    """
    if is_preprint_doi(doi) or is_preprint_url(url):
        return SUBMITTED
    from fetchers.resolvers.base import PLATFORM_PRIORITY

    host = _host(url)
    if target is None:
        return PUBLISHED if host in ("doi.org", "dx.doi.org") else ACCEPTED
    names = (
        f"{getattr(target, 'package_name', '')} "
        f"{getattr(target, 'interface_name', '')}"
    ).lower()
    hosts = _platform_hosts(getattr(target, "url", "") or url)
    for plat in PLATFORM_PRIORITY:
        if any(n in names for n in plat.names if n):
            return PUBLISHED
        for h in hosts:
            if any(h == d or h.endswith("." + d) for d in plat.domains):
                return PUBLISHED
    if host in ("doi.org", "dx.doi.org"):
        # A journal-level target replaced by the DOI landing page.
        return PUBLISHED
    return ACCEPTED


# ---------------------------------------------------------------------------
# The sidecar: the version survives the cache
# ---------------------------------------------------------------------------

SIDECAR_SUFFIX = ".version.json"


def sidecar_path(pdf_path: str | Path) -> Path:
    p = Path(pdf_path)
    return p.with_name(p.name + SIDECAR_SUFFIX)


def _fingerprint(pdf_path: Path) -> tuple[int, str] | None:
    try:
        data = pdf_path.read_bytes()
    except OSError:
        return None
    return len(data), hashlib.md5(data).hexdigest()


def record(pdf_path: str | Path, version: str, *, source: str = "") -> None:
    """Write the sidecar for `pdf_path`. Best-effort: a failure costs the
    next run a conservative guess, never the download."""
    if version not in VERSIONS:
        raise ValueError(f"unknown PDF version {version!r}")
    path = Path(pdf_path)
    fp = _fingerprint(path)
    if fp is None:
        return
    try:
        sidecar_path(path).write_text(json.dumps({
            "version": version, "source": source,
            "size": fp[0], "md5": fp[1],
        }), encoding="utf-8")
    except OSError as e:
        logger.debug("could not record PDF version for %s: %s", path, e)


def recorded(pdf_path: str | Path) -> str | None:
    """The sidecar's version, or None when there is no sidecar or it
    describes other bytes than the file now holds."""
    path = Path(pdf_path)
    try:
        meta = json.loads(sidecar_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or meta.get("version") not in VERSIONS:
        return None
    fp = _fingerprint(path)
    if fp is None or (meta.get("size"), meta.get("md5")) != fp:
        return None
    return meta["version"]


def from_path_suffix(pdf_path: str | Path) -> str | None:
    """The version a provenance suffix fixes, or None for a plain name.
    Those suffixes are written by one source each, so they outrank the
    sidecar."""
    from fetchers.core import is_repository_copy_path
    from fetchers.preprint import is_preprint_path
    from fetchers.sciencedirect import is_tdm_recovered_path

    if is_preprint_path(pdf_path):
        return SUBMITTED
    if is_repository_copy_path(pdf_path):
        return ACCEPTED
    if is_tdm_recovered_path(pdf_path):
        return PUBLISHED
    return None


def version_of(pdf_path: str | Path, *, doi: str = "") -> str:
    """The version of a file about to be attached: suffix, then sidecar,
    then the conservative default for a file of unknown provenance."""
    return (
        from_path_suffix(pdf_path)
        or recorded(pdf_path)
        or (SUBMITTED if is_preprint_doi(doi) else ACCEPTED)
    )


def settle(
    pdf_path: str | Path,
    *,
    source: str,
    source_url: str = "",
    doi: str = "",
    static: str | None = None,
) -> str:
    """Decide the version of a file a fetcher just returned, and record it.

    Called by the orchestrator right after a successful fetch, while it
    still knows which source answered and whether it was a cache hit
    (`source_url` starting `cache://`). A fresh download from a source
    with a fixed version (`STATIC_SOURCE_VERSION`, or `static` for the
    browser handlers) overwrites any older sidecar; an API-field source
    has already recorded its own; a cache hit keeps what the sidecar says.
    """
    fixed = from_path_suffix(pdf_path)
    if fixed:
        return fixed
    fresh = not (source_url or "").startswith("cache://")
    fixed = static or STATIC_SOURCE_VERSION.get(source)
    if fresh and fixed:
        record(pdf_path, fixed, source=source)
        return fixed
    return version_of(pdf_path, doi=doi)
