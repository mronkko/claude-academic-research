"""The one DOI → cache-filename rule for every fetcher.

Eleven fetchers and the browser base each carried their own copy of
`doi.replace("/", "_").replace(":", "_")`. That is enough on POSIX and
not on Windows, where a SICI DOI such as
`10.1002/(sici)1097-0266(199701)18:1<1::aid-smj894>3.0.co;2-d` keeps its
`<` and `>` — illegal in an NTFS filename, so the write raised and the
item was filed as a paywall miss. A crafted DOI holding `\\` was worse:
on Windows that is a path separator, so the name could leave
`cache_dir` altogether.

Normal DOIs map exactly as before (only `/` and `:` change), so caches
written by earlier versions are still found. Stdlib only.
"""

from __future__ import annotations

import re
from pathlib import Path

#: `/` `:` are the historical replacements; the rest are what Windows
#: forbids in a filename (`<>"\\|?*`) plus control characters. `#` and
#: `%` are legal but break `file://` URLs and shell quoting in the
#: diagnostics the run prints, so they go too.
_UNSAFE = re.compile(r'[/:<>"\\|?*#%\x00-\x1f]')


def doi_safe(doi: str) -> str:
    """`doi` as a single, portable filename component."""
    safe = _UNSAFE.sub("_", doi or "")
    # "." and ".." are directory references, not names.
    if set(safe) <= {"."}:
        safe = safe.replace(".", "_")
    return safe


def cache_pdf_path(cache_dir: str | Path, doi: str, suffix: str = "") -> Path:
    """`<cache_dir>/<doi_safe(doi)><suffix>.pdf`."""
    return Path(cache_dir) / f"{doi_safe(doi)}{suffix}.pdf"


#: Every suffix a fetcher writes after the DOI stem. Kept here so a
#: caller that must find *every* cached copy of a DOI (`--replace`) does
#: not have to know which fetcher wrote which.
CACHE_SUFFIXES: tuple[str, ...] = (
    "", "-repository-copy", "-tdm-recovered", "-preprint",
)


def all_cache_paths(cache_dir: str | Path, doi: str) -> list[Path]:
    """Every path a fetcher may have cached `doi` under, existing or not.

    Covers the DOI as given and lower-cased (older runs disagreed on
    case), every fetcher suffix, and `wiley-tdm`'s own naming, which
    replaces `/` and `.` with `-`.
    """
    doi = (doi or "").strip()
    out: list[Path] = []
    for variant in dict.fromkeys((doi, doi.lower())):
        if not variant:
            continue
        for suffix in CACHE_SUFFIXES:
            out.append(cache_pdf_path(cache_dir, variant, suffix))
        wiley = _wiley_path(cache_dir, variant)
        if wiley is not None:
            out.append(wiley)
    return list(dict.fromkeys(out))


def _wiley_path(cache_dir: str | Path, doi: str) -> Path | None:
    """Where `wiley-tdm` caches `doi`, by its own rule — or None when the
    package is not installed (then it cannot have written anything)."""
    try:
        from wiley_tdm.file_utils import FileUtils
    except Exception:  # noqa: BLE001
        return None
    try:
        path = Path(FileUtils.to_file_path(str(cache_dir), doi, "pdf"))
    except Exception:  # noqa: BLE001
        return None
    # Never hand back a path outside the cache, whatever the DOI held.
    if path.parent.resolve() != Path(cache_dir).resolve():
        return None
    return path
