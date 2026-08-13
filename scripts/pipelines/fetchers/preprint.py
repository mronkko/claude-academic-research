"""Preprint servers (arXiv / SSRN / RePEc) — opt-in, and tagged.

This source is off by default, and that is the point rather than an
oversight. Every other fetcher here returns *the article*: the version
of record, or an accepted manuscript whose content passed the same peer
review. A preprint is neither. It is the manuscript before the referees
saw it, and between it and the published paper sit revised hypotheses,
dropped analyses, added controls, and sometimes a reversed finding.

Coding a working paper as though it were the published article is a
methodological error that a systematic review will not detect
downstream: the coding note reads the same, the CSV row reads the same,
and the manuscript then reports what the authors *first thought* as what
the journal published. So the plugin will not do it silently. Two
guards, and neither is optional:

- **`--allow-preprints` is the only way in.** Naming the source directly
  is not enough; the flag is where the hazard is explained, so there is
  exactly one gesture to review and exactly one place it is documented.
- **Every attachment carries `pdf:preprint-version`.** The tag rides
  into Zotero and the coding stage surfaces it, so whoever reads the
  coded output knows which rows rest on a working paper. Provenance that
  stops at the download is provenance that fails at the moment it
  matters.

**Discovery is anchored on the DOI, never on the title.** Both routes
start from the published DOI the item already carries:

1. **OpenAlex `locations`** — every location whose `pdf_url` sits on a
   preprint host. Since the host is what qualifies a location, this
   cannot accidentally return a publisher's own PDF and mislabel it.
2. **Crossref `relation.has-preprint`** — the preprint DOI the publisher
   itself deposited. arXiv DOIs also yield a direct `arxiv.org/pdf` URL;
   others are resolved back through OpenAlex.

No title search, no fuzzy matching. A near-miss here would attach a
*different paper's* manuscript under a preprint tag that invites less
scrutiny, not more.

**Coverage is uneven, and worst where this plugin's hardest bucket
lives.** arXiv and RePEc are indexed well. SSRN — the server that
matters for management and organisational behaviour, where an
AoM-published paper usually has a working-paper twin — rarely exposes a
downloadable `pdf_url`, because SSRN itself blocks automated download.
So a miss here means "no preprint copy this plugin can reach", not "no
preprint exists"; for Sage and AoM the browser pass remains the route
that works.
"""

from __future__ import annotations

import logging
import os
import urllib.parse
from pathlib import Path

from fetchers import _pdf_validate
from fetchers.base import PdfFetcher

logger = logging.getLogger(__name__)

_OPENALEX_BASE = "https://api.openalex.org/works"
_CROSSREF_BASE = "https://api.crossref.org/works"

#: Filename marker identifying a cache file as preprint-sourced. Same
#: mechanism as `core._REPOSITORY_COPY_SUFFIX`: by attach time the
#: orchestrator holds only a path, so provenance has to be recoverable
#: from the filename rather than threaded through the ABC's return type.
_PREPRINT_SUFFIX = "-preprint"

#: Applied to every attachment this source produces. Follows the
#: `pdf:<status>` convention of `pdf:tdm-recovered` and
#: `pdf:repository-copy`, and is the reason those two exist: what was
#: attached is not always what was published.
PREPRINT_VERSION_TAG = "pdf:preprint-version"

#: Hostname suffixes that identify a URL as a preprint copy. A location
#: qualifies by host and by nothing else — that is what makes it
#: impossible for this source to return a version of record and label it
#: a preprint.
PREPRINT_HOSTS: dict[str, tuple[str, ...]] = {
    "arxiv": ("arxiv.org",),
    "ssrn": ("ssrn.com",),
    "repec": ("repec.org",),
}

#: DOI prefixes registered by preprint servers, used to read Crossref's
#: `has-preprint` relations.
_PREPRINT_DOI_PREFIXES = {
    "10.48550": "arxiv",          # arXiv's own DOI prefix
    "10.2139": "ssrn",            # SSRN (Elsevier)
}


def _doi_safe(doi: str) -> str:
    return doi.replace("/", "_").replace(":", "_")


def _cache_pdf_path(cache_dir: str | Path, doi: str) -> Path:
    return Path(cache_dir) / f"{_doi_safe(doi)}{_PREPRINT_SUFFIX}.pdf"


def preprint_server_for(url: str) -> str | None:
    """Which preprint server serves `url`, or None if it is not one.

    Suffix-matched on the hostname so `export.arxiv.org` and
    `papers.ssrn.com` count, while a lookalike domain
    (`arxiv.org.example.com`) does not.
    """
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return None
    if not host:
        return None
    for server, domains in PREPRINT_HOSTS.items():
        for domain in domains:
            if host == domain or host.endswith(f".{domain}"):
                return server
    return None


def _arxiv_pdf_url(preprint_doi: str) -> str | None:
    """`https://arxiv.org/pdf/<id>` for an arXiv DOI.

    arXiv mints `10.48550/arXiv.2401.01234`, and the identifier after
    the `arXiv.` marker is the same one its PDF path uses. Worth doing
    directly because it needs no second lookup.
    """
    tail = preprint_doi.split("/", 1)[-1]
    marker = "arxiv."
    lowered = tail.lower()
    if not lowered.startswith(marker):
        return None
    arxiv_id = tail[len(marker):].strip()
    return f"https://arxiv.org/pdf/{arxiv_id}" if arxiv_id else None


class PreprintSource(PdfFetcher):
    name = "preprint"

    def _mailto(self) -> str:
        return (
            getattr(self.config, "crossref_mailto", None)
            or os.environ.get("CROSSREF_MAILTO", "")
        )

    # -- discovery -----------------------------------------------------

    def _openalex_preprint_url(self, doi: str) -> str | None:
        """A preprint-hosted `pdf_url` OpenAlex lists for this DOI."""
        params = {"mailto": self._mailto()} if self._mailto() else None
        try:
            resp = self.http.get(
                f"{_OPENALEX_BASE}/doi:{urllib.parse.quote(doi, safe='')}",
                params=params, timeout=30,
            )
        except Exception as e:
            logger.debug("preprint: openalex lookup for %s failed: %s", doi, e)
            return None
        if resp.status_code != 200:
            return None
        try:
            work = resp.json() or {}
        except ValueError:
            return None
        for location in work.get("locations") or []:
            url = (location.get("pdf_url") or "").strip()
            if url and preprint_server_for(url):
                return url
        return None

    def _crossref_preprint_dois(self, doi: str) -> list[str]:
        """Preprint DOIs the publisher deposited as related to this one.

        Crossref's `relation` block is the publisher's own statement
        that these are the same paper, which is stronger evidence than
        anything this plugin could infer.
        """
        try:
            resp = self.http.get(
                f"{_CROSSREF_BASE}/{urllib.parse.quote(doi, safe='')}",
                timeout=30,
            )
        except Exception as e:
            logger.debug("preprint: crossref lookup for %s failed: %s", doi, e)
            return []
        if resp.status_code != 200:
            return []
        try:
            message = (resp.json() or {}).get("message") or {}
        except ValueError:
            return []
        relation = message.get("relation") or {}
        found: list[str] = []
        for entry in relation.get("has-preprint") or []:
            if (entry.get("id-type") or "").lower() != "doi":
                continue
            preprint_doi = (entry.get("id") or "").strip()
            prefix = preprint_doi.split("/", 1)[0]
            if preprint_doi and prefix in _PREPRINT_DOI_PREFIXES:
                found.append(preprint_doi)
        return found

    def _pdf_url_for(self, doi: str) -> str | None:
        """The best preprint PDF URL for `doi`, or None.

        OpenAlex first — one call, and it already knows about hosted
        copies. Crossref's relations are the fallback, because they name
        a preprint DOI that still has to be resolved to a file.
        """
        url = self._openalex_preprint_url(doi)
        if url:
            return url
        for preprint_doi in self._crossref_preprint_dois(doi):
            arxiv_url = _arxiv_pdf_url(preprint_doi)
            if arxiv_url:
                return arxiv_url
            hosted = self._openalex_preprint_url(preprint_doi)
            if hosted:
                return hosted
        return None

    # -- PdfFetcher ----------------------------------------------------

    def fetch_pdf(
        self, doi: str, *, cache_dir, bypass_prefix_filter: bool = False,
    ) -> tuple[Path, str] | None:
        del bypass_prefix_filter          # not prefix-filtered
        path = _cache_pdf_path(cache_dir, doi)
        if path.exists():
            defect = _pdf_validate.file_defect(path)
            if defect is None:
                return path, f"cache://{path}"
            logger.warning("discarding cached PDF for %s — %s", doi, defect)
            path.unlink(missing_ok=True)

        pdf_url = self._pdf_url_for(doi)
        if not pdf_url:
            return None

        try:
            resp = self.http.get(
                pdf_url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=60,
                allow_redirects=True,
            )
        except Exception as e:
            logger.debug("preprint PDF %s failed: %s", pdf_url, e)
            return None

        defect = _pdf_validate.response_defect(resp)
        if defect is not None:
            # Preprint servers answer the same URL shape with abstract
            # pages, embargo notices and "download unavailable" splash
            # screens.
            logger.warning("%s: rejected PDF for %s — %s", self.name, doi, defect)
            return None

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
        return path, pdf_url


def is_preprint_path(path: str | Path) -> bool:
    """True when `path` is a cache file this source produced.

    Mirrors `core.is_repository_copy_path`, and is read at attach time
    to apply `PREPRINT_VERSION_TAG`.
    """
    return Path(path).stem.endswith(_PREPRINT_SUFFIX)
