"""This plugin's version string, read from the manifest.

Two things stamp a version into an artefact they produce, and both need
the same answer: the provenance line on a TDM-recovered PDF (which
decides, on a later run, whether that cached file predates a
transformation change), and `search_metadata.json` (which records the
code that interpreted a search config).

The second one exists because a search config does not determine a
corpus on its own. 0.16.0 and 0.17.0 return different keyword corpora
from identical configs — 0.17.0 fixed a scope filter that had been
rejecting every Semantic Scholar record — and the metadata recorded the
search date and the databases but nothing about the code in between. A
reviewer comparing two runs had no way to see why they differed.

Stdlib only, and never raises: a version stamp must not be able to fail
the work it is annotating.
"""

from __future__ import annotations

import json
from pathlib import Path

#: `scripts/pipelines/plugin_version.py` -> repo root.
_MANIFEST = (
    Path(__file__).resolve().parents[2] / ".claude-plugin" / "plugin.json"
)

UNKNOWN = "unknown"


def plugin_version(manifest: Path | None = None) -> str:
    """The `version` field of `.claude-plugin/plugin.json`, or `"unknown"`.

    Read rather than hardcoded so a stamp cannot drift from the release
    that produced it — the whole point of stamping is that the artefact
    and the manifest agree.
    """
    try:
        raw = (manifest or _MANIFEST).read_text(encoding="utf-8")
        version = json.loads(raw).get("version")
    except Exception:  # noqa: BLE001 — a stamp must never fail the work
        return UNKNOWN
    return str(version) if version else UNKNOWN
