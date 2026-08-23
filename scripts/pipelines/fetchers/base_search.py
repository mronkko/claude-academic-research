"""BASE (Bielefeld Academic Search Engine) — repository full text.

BASE indexes ~400M documents from ~11,000 repositories, which is the
widest green-OA net available and a genuinely different population from
CORE's. Same caveat as CORE and OpenAIRE: what it returns is the
author's accepted manuscript, so attachments carry
`pdf:repository-copy`.

**Access is gated by IP registration, not by an API key.** BASE
whitelists the IP ranges of registered organisations; an unregistered
caller gets HTTP 200 with a JSON body that is an error, not results:

    {"error": "Access denied for IP address 130.233.23.169 and
     user agent curl/8.7.1."}

That shape matters — a 200 with an `error` key would otherwise be
parsed as "no results", making an entitlement problem look like a
coverage gap forever. `_denied()` detects it explicitly and logs once
with the registration URL.

Registration is free for academic institutions:
https://www.base-search.net/about/en/faq_use.php

Because of that gate this fetcher could not be exercised live against
the library it was written for — Aalto's range is not registered — so
its parsing is covered by unit tests against recorded response shapes
rather than by a live run. The live test exists and will skip until
registration lands.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fetchers import _pdf_validate
from fetchers.base import PdfFetcher

logger = logging.getLogger(__name__)

_API = "https://api.base-search.net/cgi-bin/BaseHttpSearchInterface.fcgi"

_REPOSITORY_COPY_SUFFIX = "-repository-copy"

_REGISTER_URL = "https://www.base-search.net/about/en/faq_use.php"

#: Pre-peer-review hosts, excluded for the reason given in
#: `fetchers/openaire.py`.
_PREPRINT_HOSTS = (
    "osf.io", "arxiv.org", "ssrn.com", "biorxiv.org", "medrxiv.org",
    "researchsquare.com", "preprints.org",
)

_warned_denied = False


def _doi_safe(doi: str) -> str:
    return doi.replace("/", "_").replace(":", "_")


def _cache_pdf_path(cache_dir: str | Path, doi: str) -> Path:
    return Path(cache_dir) / f"{_doi_safe(doi)}{_REPOSITORY_COPY_SUFFIX}.pdf"


def is_access_denied(payload: dict) -> bool:
    """True when BASE answered 200 with an IP-registration refusal.

    Kept public and separate because the failure is silent otherwise:
    the body has no `results` key, so any generic parser reports "no
    match" and the operator never learns the source is switched off.
    """
    return bool(isinstance(payload, dict) and payload.get("error"))


def candidate_urls(payload: dict) -> list[str]:
    """Full-text links from a BASE `PerformSearch` JSON response.

    BASE returns Dublin Core, so the link field is `dclink` and the
    document type is `dctypenorm`. Both are sometimes a bare string and
    sometimes a list, exactly like OpenAIRE.
    """
    docs = (
        ((payload.get("response") or {}).get("docs"))
        or (payload.get("docs"))
        or []
    )
    if isinstance(docs, dict):
        docs = [docs]
    urls: list[str] = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        for field in ("dclink", "dcidentifier"):
            value = doc.get(field)
            for url in (value if isinstance(value, list) else [value]):
                if isinstance(url, str) and url.startswith("http"):
                    urls.append(url)
    out: list[str] = []
    for url in dict.fromkeys(urls):
        low = url.lower()
        if "doi.org/" in low:
            continue
        if any(host in low for host in _PREPRINT_HOSTS):
            continue
        out.append(url)
    return out


class BaseSearchSource(PdfFetcher):
    name = "base"

    def fetch_pdf(
        self, doi: str, *, cache_dir, bypass_prefix_filter: bool = False,
    ) -> tuple[Path, str] | None:
        global _warned_denied
        del bypass_prefix_filter          # not prefix-filtered
        path = _cache_pdf_path(cache_dir, doi)
        if path.exists():
            defect = _pdf_validate.file_defect(path)
            if defect is None:
                return path, f"cache://{path}"
            logger.warning("discarding cached PDF for %s — %s", doi, defect)
            path.unlink(missing_ok=True)

        try:
            resp = self.http.get(
                _API,
                params={
                    "func": "PerformSearch",
                    "query": f'dcdoi:"{doi}"',
                    "format": "json",
                    "hits": 5,
                },
                timeout=30,
            )
        except Exception as e:
            logger.debug("base lookup %s failed: %s", doi, e)
            return None
        if resp.status_code != 200:
            return None
        try:
            payload = resp.json() or {}
        except Exception:
            return None

        if is_access_denied(payload):
            if not _warned_denied:
                _warned_denied = True
                logger.warning(
                    "base: BASE refused this IP — its API is gated by "
                    "organisational IP registration, not an API key. "
                    "Register at %s; skipping this source for the run.",
                    _REGISTER_URL,
                )
            return None

        for url in candidate_urls(payload)[:3]:
            try:
                got = self.http.get(
                    url, headers={"User-Agent": "Mozilla/5.0"}, timeout=60,
                )
            except Exception as e:
                logger.debug("base PDF %s failed: %s", url, e)
                continue
            defect = _pdf_validate.response_defect(got)
            if defect is not None:
                logger.debug("%s: rejected %s — %s", self.name, url, defect)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(got.content)
            return path, url
        return None
