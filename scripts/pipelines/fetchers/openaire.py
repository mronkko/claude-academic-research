"""OpenAIRE Explore — full text from the European OA graph.

OpenAIRE aggregates ~200M records from repositories, funders and
publishers across Europe. Like CORE it is a *repository* index, so what
it returns is normally the author's accepted manuscript rather than the
version of record, and every attachment is tagged `pdf:repository-copy`
for the same reason.

**Measured recall on the corpus this was built against was zero.** Over
a 60-DOI sample drawn from items the whole cascade had already failed,
57 had no usable URL, 3 had a URL that served no PDF, and none produced
a file. That is worth stating plainly rather than discovering later: the
`bestaccessright: OPEN` flag is *not* a promise of retrievable full
text. It is frequently set on records whose only instance is the
publisher DOI landing page — the very page the cascade just failed on —
or an OSF preprint. Reading that flag alone suggested a 13% hit rate;
reading the actual `fulltext` / instance URLs gave 0%.

It is still registered, on the same reasoning that keeps CORE in the
cascade: this is a plugin used across disciplines, and a European
repository index that returns nothing for Anglophone management
journals may do well for a corpus with heavy EU-funded output. It costs
one lookup per item and self-disables on any error.

Two filters make the difference between useful and harmful:

- **`doi.org` URLs are discarded.** They redirect to the publisher
  landing page the cascade has already tried; following one wastes a
  request and, worse, can bank an HTML error page as a "hit".
- **Preprint hosts are discarded.** OpenAIRE happily lists an OSF or
  arXiv copy as the open instance of a published article. This plugin
  treats a preprint as a different paper — see `fetchers/preprint.py`
  — and it must not arrive unlabelled through a side door.
"""

from __future__ import annotations

import logging
import urllib.parse
from pathlib import Path

from fetchers import _pdf_validate
from fetchers.base import PdfFetcher
from fetchers.core import REPOSITORY_COPY_TAG  # noqa: F401  (re-export intent)

logger = logging.getLogger(__name__)

_API = "https://api.openaire.eu/search/publications"

#: Same marker CORE uses — see `fetchers/core.py`. The attachment is an
#: accepted manuscript either way, and the coding stage only needs to
#: know that, not which aggregator supplied it.
_REPOSITORY_COPY_SUFFIX = "-repository-copy"

#: Hosts whose copies are pre-peer-review. Excluded outright; the
#: opt-in preprint path is `fetchers/preprint.py`.
_PREPRINT_HOSTS = (
    "osf.io", "arxiv.org", "ssrn.com", "biorxiv.org", "medrxiv.org",
    "researchsquare.com", "preprints.org", "hal.science",
)


def _doi_safe(doi: str) -> str:
    return doi.replace("/", "_").replace(":", "_")


def _cache_pdf_path(cache_dir: str | Path, doi: str) -> Path:
    return Path(cache_dir) / f"{_doi_safe(doi)}{_REPOSITORY_COPY_SUFFIX}.pdf"


def _as_list(value) -> list:
    """OpenAIRE collapses single-element lists to the bare object."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def candidate_urls(payload: dict) -> list[str]:
    """Full-text URLs from one OpenAIRE JSON response, best first.

    Split out from `fetch_pdf` because the response shape is the whole
    difficulty here — deeply nested, inconsistently list-wrapped — and
    it is the part worth testing without a network.
    """
    res = ((payload.get("response") or {}).get("results") or {}).get("result")
    records = _as_list(res)
    if not records:
        return []
    try:
        meta = records[0]["metadata"]["oaf:entity"]["oaf:result"]
    except (KeyError, TypeError, IndexError):
        return []

    urls: list[str] = []
    # `fulltext` is the direct-link field and is the only one that ever
    # points straight at a .pdf; take it ahead of the instances.
    for entry in _as_list(meta.get("fulltext")):
        if isinstance(entry, dict) and entry.get("$"):
            urls.append(str(entry["$"]))
    children = meta.get("children")
    instances = _as_list(children.get("instance")) if isinstance(children, dict) else []
    for inst in instances:
        if not isinstance(inst, dict):
            continue
        if (inst.get("accessright") or {}).get("@classid") != "OPEN":
            continue
        url = ((inst.get("webresource") or {}).get("url") or {}).get("$")
        if url:
            urls.append(str(url))

    out: list[str] = []
    for url in dict.fromkeys(urls):
        low = url.lower()
        if "doi.org/" in low:
            continue
        if any(host in low for host in _PREPRINT_HOSTS):
            continue
        out.append(url)
    return out


class OpenAireSource(PdfFetcher):
    name = "openaire"

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

        lookup = (
            f"{_API}?format=json&doi="
            f"{urllib.parse.quote(doi, safe='')}"
        )
        try:
            resp = self.http.get(lookup, timeout=30)
        except Exception as e:
            logger.debug("openaire lookup %s failed: %s", doi, e)
            return None
        if resp.status_code != 200:
            return None
        try:
            payload = resp.json() or {}
        except Exception:
            return None

        for url in candidate_urls(payload)[:3]:
            try:
                # Repository platforms (bepress, DSpace) answer a bare
                # client with 403; a normal UA is the difference between
                # a file and nothing.
                got = self.http.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=60,
                )
            except Exception as e:
                logger.debug("openaire PDF %s failed: %s", url, e)
                continue
            defect = _pdf_validate.response_defect(got)
            if defect is not None:
                logger.debug("%s: rejected %s — %s", self.name, url, defect)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(got.content)
            return path, url
        return None
