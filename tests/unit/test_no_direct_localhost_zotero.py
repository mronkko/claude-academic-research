"""CI guard: pipeline scripts must not talk to Zotero directly.

The plugin's standing rule (per the IRON RULE in
`skills/zotero-operations/SKILL.md`) routes every Zotero / BBT
interaction through `zotero_io.py` (heavy ops with pyzotero) or
`bbt_client.py` (stdlib-only BBT helpers). Anything else under
`scripts/pipelines/` writing `urllib.request.urlopen(...)` or
`curl` against `127.0.0.1:23119` / `localhost:23119` is a defect
signal — it bypasses retries, schema versioning, and cross-project
reuse, and steers Claude into improvising pipeline code.

This test is the regression guard. Adding a new direct-HTTP call
fails the build until it is moved into one of the allowed modules.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PIPELINES_ROOT = REPO_ROOT / "scripts" / "pipelines"

# Files allowed to reference the localhost:23119 endpoints. Both modules
# are themselves the canonical Zotero / BBT transports — every other
# pipeline file must call into them rather than re-implementing the URL.
ALLOWED_FILES = {
    PIPELINES_ROOT / "zotero_io.py",
    PIPELINES_ROOT / "bbt_client.py",
}

# `legacy/` is the pre-v0.3.0 rollback path; it is POSIX-only and not
# under active development. Any new improvement must land in the
# refactored `enrich_*.py` orchestrators, never in legacy.
LEGACY_ROOT = PIPELINES_ROOT / "legacy"

# Match the two Zotero data-paths the IRON RULE governs:
#   - `/api/...`         (Zotero REST API)
#   - `/better-bibtex/...` (Better BibTeX endpoints)
# Either bare 127.0.0.1 or the friendlier `localhost`. Other paths on
# the same port (`/connector/ping`, `/connector/getSelectedCollection`)
# are a separate browser-bridge surface and are out of scope for this
# guard.
LOCALHOST_PATTERN = re.compile(
    r"(?:127\.0\.0\.1|localhost):23119/(?:api|better-bibtex)/",
)


def _walk_pipeline_files() -> list[Path]:
    """Every .py file under scripts/pipelines/ except allow-listed ones."""
    out: list[Path] = []
    for p in PIPELINES_ROOT.rglob("*.py"):
        if p in ALLOWED_FILES:
            continue
        try:
            p.relative_to(LEGACY_ROOT)
            continue  # under legacy/, skip
        except ValueError:
            pass
        out.append(p)
    return out


def test_no_direct_localhost_zotero_outside_canonical_modules() -> None:
    """Every file under scripts/pipelines/ (except zotero_io.py,
    bbt_client.py, and legacy/) must be free of references to
    127.0.0.1:23119 or localhost:23119. Direct HTTP calls steer
    Claude away from the canonical Zotero surface and break the
    "no improvised pipeline code" rule.
    """
    offenders: list[tuple[Path, int, str]] = []
    for path in _walk_pipeline_files():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if LOCALHOST_PATTERN.search(line):
                offenders.append((path.relative_to(REPO_ROOT), lineno, line.strip()))

    if offenders:
        formatted = "\n".join(
            f"  {path}:{lineno}: {snippet}" for path, lineno, snippet in offenders
        )
        raise AssertionError(
            "Direct Zotero localhost:23119 reference found outside "
            "zotero_io.py / bbt_client.py / legacy/. Route the call "
            "through the canonical helpers instead — see the IRON RULE "
            "in skills/zotero-operations/SKILL.md.\n"
            f"Offending lines:\n{formatted}"
        )
