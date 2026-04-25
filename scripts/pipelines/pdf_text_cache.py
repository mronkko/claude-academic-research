"""Content-hash-keyed cache for `pdftotext`-extracted PDF text.

Without this cache, every consumer (`fulltext_code.py`, audits,
re-codes) re-runs `pdftotext -layout` on the same PDF — slow, and
particularly painful after Elsevier TDM remediation replaces a PDF
under the same `item_key` (the new content needs re-extraction; the
old extraction is stale).

Contract:

- Cache key is `(item_key, sha256(pdf_bytes)[:16])`. Replacing the
  PDF (different bytes) automatically invalidates the prior entry —
  next call extracts again.
- Cache file lives at `<cache_dir>/<item_key>-<hash>.txt` so a single
  `item_key` may have multiple snapshots on disk during a remediation
  pass; the lookup picks the one matching the current PDF hash.
- Default `cache_dir` is `Path.cwd() / ".claude" / "pdf_text"`. The
  setup wizard already adds `.claude/` to the project's `.gitignore`,
  so cached text doesn't get committed.
- `pdftotext` is required at runtime (poppler — same dependency the
  legacy `fulltext_code` uses). Missing tool raises `FileNotFoundError`
  with a helpful message; callers can fall back to a fresh subprocess
  run if they need to handle that explicitly.

This module is pure helper code. It does not touch Zotero, the
network, or any pipeline state — just `subprocess` and the filesystem.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_CACHE_SUBDIR = Path(".claude") / "pdf_text"


def _content_hash(pdf_path: Path) -> str:
    """Return the first 16 hex chars of SHA-256 of the PDF bytes.

    16 chars (~64 bits) is plenty for filename disambiguation and
    keeps paths short on Windows where MAX_PATH still bites.
    """
    h = hashlib.sha256()
    with pdf_path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _cache_path(cache_dir: Path, item_key: str, content_hash: str) -> Path:
    return cache_dir / f"{item_key}-{content_hash}.txt"


def _resolve_cache_dir(cache_dir: str | Path | None) -> Path:
    if cache_dir is None:
        return Path.cwd() / DEFAULT_CACHE_SUBDIR
    return Path(cache_dir)


def get_text(
    item_key: str,
    pdf_path: str | Path,
    *,
    cache_dir: str | Path | None = None,
) -> str:
    """Return extracted text for `pdf_path`, caching by content hash.

    On a cache hit, reads `<cache_dir>/<item_key>-<hash>.txt` and
    returns its contents. On a miss, runs `pdftotext -layout`, writes
    the cache file, and returns the text.

    `item_key` and `pdf_path` together identify the entry; replacing
    the PDF bytes (e.g. Elsevier TDM remediation) flips the hash and
    transparently invalidates the prior cache.
    """
    if not item_key:
        raise ValueError("item_key must be a non-empty string")
    pdf_path = Path(pdf_path)
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    cache_dir = _resolve_cache_dir(cache_dir)
    content_hash = _content_hash(pdf_path)
    cache_file = _cache_path(cache_dir, item_key, content_hash)

    if cache_file.is_file():
        return cache_file.read_text(encoding="utf-8", errors="replace")

    text = _run_pdftotext(pdf_path)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(text, encoding="utf-8")
    return text


def _run_pdftotext(pdf_path: Path) -> str:
    """Invoke `pdftotext -layout` and return decoded text.

    Layout mode preserves column structure better than the default
    flow mode for academic papers. Errors raise CalledProcessError so
    the caller can decide whether to retry / fall back.
    """
    if shutil.which("pdftotext") is None:
        raise FileNotFoundError(
            "pdftotext (poppler) is required for PDF text extraction. "
            "Install via your package manager (e.g. `brew install poppler` "
            "on macOS, `apt install poppler-utils` on Debian/Ubuntu)."
        )
    proc = subprocess.run(  # noqa: S603 — input is a Path object, not user-supplied shell
        ["pdftotext", "-layout", str(pdf_path), "-"],
        check=True,
        capture_output=True,
    )
    # pdftotext output is UTF-8 in modern poppler; older versions may emit
    # latin-1 fragments. Replace-on-error keeps cache reads consistent.
    return proc.stdout.decode("utf-8", errors="replace")


def clear_item(item_key: str, *, cache_dir: str | Path | None = None) -> int:
    """Delete every cached extraction for `item_key`. Returns count removed.

    Useful after a manual PDF replacement when the caller wants to be
    explicit about invalidating, rather than relying on the next
    `get_text` to overwrite. Cross-platform: pure Path operations, no
    shell.
    """
    cache_dir = _resolve_cache_dir(cache_dir)
    if not cache_dir.is_dir():
        return 0
    removed = 0
    for entry in cache_dir.glob(f"{item_key}-*.txt"):
        try:
            entry.unlink()
            removed += 1
        except OSError as e:
            print(f"pdf_text_cache.clear_item: could not remove {entry}: {e}",
                  file=sys.stderr)
    return removed
