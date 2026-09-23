"""arXiv — the PDF of an arXiv-native item, straight from its own DOI.

arXiv mints `10.48550/arXiv.<id>` (DataCite often records it upper-case,
`10.48550/ARXIV.<id>`), and `<id>` is the path of the PDF at
`https://arxiv.org/pdf/<id>`. No lookup is needed.

This used to be reachable only indirectly: through an aggregator whose
record happened to list the PDF, or through `preprint`, which follows a
*published* article's preprint relations and never looks at the item's
own DOI. Four arXiv items came back "every route tried and came back
empty" on 2026-09-23 that way, although arXiv serves a PDF for every
posting.

For an item whose DOI is the arXiv one, that PDF is the work itself,
not a preprint of some other version of record, so this is an ordinary
open-access source: on by default, and without the
`pdf:preprint-version` tag. A published article with an arXiv preprint
is still `preprint`'s business, behind `--allow-preprints`.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fetchers import _pdf_validate
from fetchers.base import PdfFetcher

logger = logging.getLogger(__name__)

_ARXIV_PREFIX = "10.48550/"
_MARKER = "arxiv."


def arxiv_pdf_url(doi: str) -> str | None:
    """`https://arxiv.org/pdf/<id>` for an arXiv DOI in any case, else None."""
    doi = (doi or "").strip()
    if not doi.lower().startswith(_ARXIV_PREFIX):
        return None
    tail = doi.split("/", 1)[1]
    if not tail.lower().startswith(_MARKER):
        return None
    arxiv_id = tail[len(_MARKER):].strip()
    return f"https://arxiv.org/pdf/{arxiv_id}" if arxiv_id else None


def _cache_pdf_path(cache_dir: str | Path, doi: str) -> Path:
    return Path(cache_dir) / f"{doi.lower().replace('/', '_').replace(':', '_')}.pdf"


class ArxivSource(PdfFetcher):
    name = "arxiv"

    def fetch_pdf(
        self, doi: str, *, cache_dir, bypass_prefix_filter: bool = False,
    ) -> tuple[Path, str] | None:
        del bypass_prefix_filter          # the DOI shape is the filter
        url = arxiv_pdf_url(doi)
        if url is None:
            return None
        path = _cache_pdf_path(cache_dir, doi)
        if path.exists():
            defect = _pdf_validate.file_defect(path)
            if defect is None:
                return path, f"cache://{path}"
            logger.warning("discarding cached PDF for %s — %s", doi, defect)
            path.unlink(missing_ok=True)
        try:
            resp = self.http.get(
                url, headers={"User-Agent": "Mozilla/5.0"}, timeout=60,
                allow_redirects=True,
            )
        except Exception as e:
            logger.debug("arxiv PDF %s failed: %s", url, e)
            return None
        defect = _pdf_validate.response_defect(resp)
        if defect is not None:
            logger.warning("%s: rejected PDF for %s — %s", self.name, doi, defect)
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
        return path, url
