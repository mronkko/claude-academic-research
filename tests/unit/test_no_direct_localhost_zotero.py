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

The first version grepped each line for the literal URL, so a URL
assembled from parts — `f"http://{host}:{PORT}/api/..."`, or a port held
as the integer `23119` — passed. The scan now parses each file: f-strings
are rendered with `{}` for their placeholders before matching, an integer
`23119` is flagged on its own, and an `/api/` or `/better-bibtex/` path
fragment is flagged in any file that also names the port.
"""

from __future__ import annotations

import ast
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

# Match the two Zotero data-paths the IRON RULE governs:
#   - `/api/...`         (Zotero REST API)
#   - `/better-bibtex/...` (Better BibTeX endpoints)
# Either bare 127.0.0.1 or the friendlier `localhost`. Other paths on
# the same port (`/connector/ping`, `/connector/getSelectedCollection`)
# are a separate browser-bridge surface and are out of scope for this
# guard.
LOCALHOST_PATTERN = re.compile(
    r"(?:127\.0\.0\.1|localhost|\{\}):(?:23119|\{\})/(?:api|better-bibtex)/",
)
ZOTERO_PORT = 23119
PATH_FRAGMENT = re.compile(r"^/?(?:api|better-bibtex)/")


def _rendered_strings(tree: ast.AST) -> list[tuple[int, str]]:
    """(line, text) for every string literal, f-strings rendered with
    `{}` in place of each interpolation."""
    out: list[tuple[int, str]] = []
    in_fstring: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            text = "".join(
                v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "{}"
                for v in node.values
            )
            out.append((node.lineno, text))
            in_fstring.update(id(v) for v in node.values)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in in_fstring):
            out.append((node.lineno, node.value))
    return out


def _offences(source: str) -> list[tuple[int, str]]:
    tree = ast.parse(source)
    strings = _rendered_strings(tree)
    found = [(line, text) for line, text in strings if LOCALHOST_PATTERN.search(text)]
    port_ints = [
        (node.lineno, "23119 (integer port)")
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value == ZOTERO_PORT
        and not isinstance(node.value, bool)
    ]
    found += port_ints
    names_port = bool(port_ints) or any(str(ZOTERO_PORT) in text for _, text in strings)
    if names_port:
        found += [(line, text) for line, text in strings if PATH_FRAGMENT.match(text)]
    return sorted(set(found))


def test_the_scan_sees_assembled_urls() -> None:
    caught = [
        'URL = "http://127.0.0.1:23119/api/users/0/items"',
        'u = f"http://localhost:{port}/api/users/0/items"',
        'u = f"http://{host}:23119/better-bibtex/json-rpc"',
        'PORT = 23119\nu = "http://127.0.0.1:" + str(PORT) + "/api/"',
        'BASE = "http://127.0.0.1:23119"\nPATH = "/api/users/0"',
    ]
    for src in caught:
        assert _offences(src), f"missed: {src!r}"
    allowed = [
        'PING = "http://127.0.0.1:23119/connector/ping"',
        'msg = "the local server on localhost:23119 is disabled"',
        'u = "https://api.zotero.org/users/1/items"',
        '# http://localhost:23119/api/ in a comment\nx = 1',
    ]
    for src in allowed:
        assert not _offences(src), f"false positive: {src!r}"


def _walk_pipeline_files() -> list[Path]:
    """Every .py file under scripts/pipelines/ except allow-listed ones."""
    out: list[Path] = []
    for p in PIPELINES_ROOT.rglob("*.py"):
        if p in ALLOWED_FILES:
            continue
        out.append(p)
    return out


def test_no_direct_localhost_zotero_outside_canonical_modules() -> None:
    """Every file under scripts/pipelines/ (except zotero_io.py and
    bbt_client.py) must be free of references to
    127.0.0.1:23119 or localhost:23119. Direct HTTP calls steer
    Claude away from the canonical Zotero surface and break the
    "no improvised pipeline code" rule.
    """
    offenders: list[tuple[Path, int, str]] = []
    for path in _walk_pipeline_files():
        for lineno, snippet in _offences(path.read_text(encoding="utf-8")):
            offenders.append((path.relative_to(REPO_ROOT), lineno, snippet.strip()))

    if offenders:
        formatted = "\n".join(
            f"  {path}:{lineno}: {snippet}" for path, lineno, snippet in offenders
        )
        raise AssertionError(
            "Direct Zotero localhost:23119 reference found outside "
            "zotero_io.py / bbt_client.py. Route the call "
            "through the canonical helpers instead — see the IRON RULE "
            "in skills/zotero-operations/SKILL.md.\n"
            f"Offending lines:\n{formatted}"
        )
