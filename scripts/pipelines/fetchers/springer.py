"""SpringerLink — direct PDF download for Springer Nature DOIs.

No API key. The URL is public, and this was believed to be gated only by
institutional network access (FinELib / campus VPN).

**That is not what happens in practice, as of 2026-08.** SpringerLink
answers `content/pdf/<doi>.pdf` with an identical ~3 KB HTML page titled
`Client Challenge` — an Imperva JavaScript interstitial — for every
request from an HTTP client. Measured from an on-campus IP
(`*.aalto.fi`) across ten DOIs including licensed titles, and unchanged
by a complete browser header set: the same 3038 bytes every time. Being
entitled makes no difference, because the challenge is served before
entitlement is ever evaluated.

So this source reliably returns None in the automated cascade, and
Springer DOIs have to be recovered through the browser pass instead
(`fetchers/browser/`), where a real JS engine can clear the challenge.
It is kept in the stage-1 list because asking costs one cheap request
that fails fast, and the block may be relaxed at any time — but never
read a Springer miss as evidence that an article is unavailable.

**It stops asking once the wall is confirmed.** "One cheap request"
was true per item and wrong per run: a live 1,895-item pass made 144
requests, got the same 3 KB challenge 144 times, attached nothing, and
logged a warning for each — the loudest thing on the user's screen, and
then a second round when the browser pass re-tried the same source. So
after `_CHALLENGE_LIMIT` consecutive challenges this run gives up for
the rest of the process and says so once. It still asks at the start of
every run, which is what keeps "the block may be relaxed at any time"
true, and one non-challenge answer resets the counter.
"""

from __future__ import annotations

import logging
import threading
import urllib.parse
from pathlib import Path

from fetchers import _pdf_validate
from fetchers.base import PdfFetcher

logger = logging.getLogger(__name__)

#: Consecutive Imperva challenges before this source stops asking for the
#: rest of the process. Small because the evidence is unambiguous — the
#: challenge is byte-identical every time and arrives before entitlement
#: is evaluated, so three in a row is not a slow patch, it is the wall.
_CHALLENGE_LIMIT = 3

_challenge_lock = threading.Lock()
_consecutive_challenges = 0
_gave_up_announced = False


def _note_challenge(doi: str) -> None:
    """Count an Imperva challenge; announce the giving-up once."""
    global _consecutive_challenges, _gave_up_announced
    with _challenge_lock:
        _consecutive_challenges += 1
        if _consecutive_challenges == _CHALLENGE_LIMIT and not _gave_up_announced:
            _gave_up_announced = True
            print(
                f"  springer: {_CHALLENGE_LIMIT} consecutive Imperva "
                f"challenges (last: {doi}) — not asking again this run. "
                f"These items need `--sources browser`, where a real JS "
                f"engine clears the challenge.",
                flush=True,
            )


def _note_real_answer() -> None:
    """Springer answered with something other than the challenge."""
    global _consecutive_challenges
    with _challenge_lock:
        _consecutive_challenges = 0


def _blocked() -> bool:
    with _challenge_lock:
        return _consecutive_challenges >= _CHALLENGE_LIMIT

#: `10.1023` is Kluwer Academic, absorbed into Springer in 2004; its
#: titles serve from link.springer.com under the same URL shape as
#: `10.1007`. Legacy imprints like this are where a prefix list quietly
#: costs retrieval — a live 1,895-item pass left 12 Kluwer-era items
#: with no handler at all, so they fell through to the resolver route
#: for want of one line.
_SPRINGER_PREFIXES = (
    "10.1007/", "10.1023/", "10.1057/", "10.1038/", "10.1140/",
    "10.1186/", "10.1365/", "10.1245/",
)


def _doi_safe(doi: str) -> str:
    return doi.replace("/", "_").replace(":", "_")


def _is_challenge(resp) -> bool:
    """True when this is Imperva's interstitial rather than a real answer.

    Matched on the body, not the status code: the challenge arrives as
    HTTP 200 with a ~3 KB HTML page, which is indistinguishable from a
    broken publisher unless you look inside. `fetchers/browser/base.py`
    keys on the same markers for the same reason.
    """
    try:
        body = (resp.content or b"")[:2000].decode("utf-8", errors="replace")
    except Exception:
        return False
    low = body.lower()
    return "client challenge" in low or "incapsula" in low or "_incapsula_" in low


def _cache_pdf_path(cache_dir: str | Path, doi: str) -> Path:
    return Path(cache_dir) / f"{_doi_safe(doi)}.pdf"


class SpringerSource(PdfFetcher):
    name = "springer"
    direct_access_domains = ("link.springer.com", "springer.com")

    def fetch_pdf(
        self, doi: str, *, cache_dir, bypass_prefix_filter: bool = False,
    ) -> tuple[Path, str] | None:
        if (not bypass_prefix_filter
                and not any(doi.startswith(p) for p in _SPRINGER_PREFIXES)):
            return None
        path = _cache_pdf_path(cache_dir, doi)
        if path.exists():
            # Validate before serving: an entry written by an earlier,
            # unvalidated run may be truncated, and returning it unchecked
            # made the corruption permanent — every later run
            # short-circuited on the bad file instead of re-fetching.
            _defect = _pdf_validate.file_defect(path)
            if _defect is None:
                return path, f"cache://{path}"
            logger.warning("discarding cached PDF for %s — %s", doi, _defect)
            path.unlink(missing_ok=True)

        if _blocked():
            # Already established this run. Returning None keeps the
            # cascade falling through exactly as a real miss would.
            return None

        encoded = urllib.parse.quote(doi, safe="")
        url = f"https://link.springer.com/content/pdf/{encoded}.pdf"
        try:
            resp = self.http.get(
                url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30,
            )
        except Exception as e:
            logger.debug("springer %s failed: %s", doi, e)
            return None
        _defect = _pdf_validate.response_defect(resp)
        if _defect is not None:
            # None (not an exception) so the cascade falls through to the
            # next source — a truncated copy at one provider is often
            # served intact by another.
            if _is_challenge(resp):
                _note_challenge(doi)
                logger.debug(
                    "%s: Imperva challenge for %s", self.name, doi,
                )
            else:
                _note_real_answer()
                logger.warning(
                    "%s: rejected PDF for %s — %s", self.name, doi, _defect,
                )
            return None
        _note_real_answer()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
        return path, url
