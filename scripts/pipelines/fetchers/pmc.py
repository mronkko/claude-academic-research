"""PubMed Central — PDF download with SHA-256 proof-of-work challenge.

PMC fronts PDFs with a JavaScript PoW challenge. The client requests
the PDF URL, receives HTML with `POW_CHALLENGE` + `POW_DIFFICULTY` +
`POW_COOKIE_NAME`, computes a nonce whose SHA-256(challenge + nonce)
has `difficulty` leading hex zeros, sets the resulting cookie, and
re-requests. requests.Session handles the cookie jar automatically —
no manual http.cookiejar glue needed.

The PoW solver is kept in-house; no library replaces it.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
import urllib.parse
from pathlib import Path

from fetchers.base import PdfFetcher

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def _doi_safe(doi: str) -> str:
    return doi.replace("/", "_").replace(":", "_")


def _cache_pdf_path(cache_dir: str | Path, doi: str) -> Path:
    return Path(cache_dir) / f"{_doi_safe(doi)}.pdf"


def _solve_pow(challenge: str, difficulty: int) -> int:
    """SHA-256 nonce-search. Returns smallest nonce where the hash has
    `difficulty` leading hex zeros."""
    prefix = "0" * difficulty
    nonce = 0
    while True:
        h = hashlib.sha256((challenge + str(nonce)).encode()).hexdigest()
        if h.startswith(prefix):
            return nonce
        nonce += 1


class PmcSource(PdfFetcher):
    name = "pubmed_central"

    def fetch_pdf(self, doi: str, *, cache_dir) -> tuple[Path, str] | None:
        path = _cache_pdf_path(cache_dir, doi)
        if path.exists():
            return path, f"cache://{path}"

        # Step 1: DOI → PMC ID via NCBI ID converter.
        conv_url = (
            "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"
            f"?ids={urllib.parse.quote(doi, safe='')}&format=json"
        )
        try:
            conv = self.http.get(conv_url, timeout=30)
        except Exception as e:
            logger.debug("PMC idconv %s failed: %s", doi, e)
            return None
        if conv.status_code != 200:
            return None
        records = (conv.json() or {}).get("records") or []
        if not records or "pmcid" not in records[0]:
            return None
        pmcid = records[0]["pmcid"]

        # Step 2: request the PDF — may land either on the PDF itself or on
        # a PoW challenge page.
        pdf_url = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf/"
        try:
            resp = self.http.get(
                pdf_url, headers={"User-Agent": _USER_AGENT}, timeout=30,
            )
        except Exception as e:
            logger.debug("PMC first GET %s failed: %s", pdf_url, e)
            return None

        if resp.content[:4] == b"%PDF":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(resp.content)
            return path, pdf_url

        # Step 3: parse PoW challenge out of the HTML.
        html = resp.text
        m_challenge = re.search(r'POW_CHALLENGE\s*=\s*"([^"]+)"', html)
        m_diff = re.search(r'POW_DIFFICULTY\s*=\s*"(\d+)"', html)
        m_name = re.search(r'POW_COOKIE_NAME\s*=\s*"([^"]+)"', html)
        m_path = re.search(r'POW_COOKIE_PATH\s*=\s*"([^"]+)"', html)
        if not all([m_challenge, m_diff, m_name, m_path]):
            return None

        challenge = m_challenge.group(1)
        difficulty = int(m_diff.group(1))
        cookie_name = m_name.group(1)
        cookie_path = m_path.group(1)

        # Step 4: solve.
        nonce = _solve_pow(challenge, difficulty)
        cookie_value = f"{challenge},{nonce}"

        # Step 5: set the cookie on the session and re-request. requests
        # persists cookies across calls automatically (unlike the legacy
        # http.cookiejar + build_opener dance).
        self.http.cookies.set(
            cookie_name, cookie_value,
            domain=".ncbi.nlm.nih.gov",
            path=cookie_path,
            secure=True,
            expires=int(time.time()) + 18000,
        )
        try:
            resp2 = self.http.get(
                pdf_url, headers={"User-Agent": _USER_AGENT}, timeout=60,
            )
        except Exception as e:
            logger.debug("PMC second GET %s failed: %s", pdf_url, e)
            return None
        if resp2.content[:4] != b"%PDF":
            return None

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp2.content)
        return path, pdf_url
