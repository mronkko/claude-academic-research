"""Plugin and marketplace manifest schema checks."""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _load(filename: str) -> dict:
    return json.loads((REPO / ".claude-plugin" / filename).read_text(encoding="utf-8"))


def test_plugin_manifest_required_fields() -> None:
    m = _load("plugin.json")
    for key in ("name", "version", "description", "author", "license"):
        assert key in m, f"plugin.json missing required field: {key}"
    assert m["name"] == "academic-research"
    assert m["license"] == "MIT"
    assert m["author"].get("name") and m["author"].get("email")


def test_plugin_manifest_version_is_semver() -> None:
    import re

    v = _load("plugin.json")["version"]
    assert re.match(r"^\d+\.\d+\.\d+(-[\w.]+)?$", v), f"non-semver version: {v}"


def test_marketplace_manifest_references_plugin() -> None:
    m = _load("marketplace.json")
    assert m["name"] == "mronkko"
    assert isinstance(m["plugins"], list) and len(m["plugins"]) == 1
    plug = m["plugins"][0]
    assert plug["name"] == "academic-research"
    assert plug["source"] == "./", "marketplace source must be './' for same-repo hosting"


def test_marketplace_owner_present() -> None:
    m = _load("marketplace.json")
    assert "owner" in m and m["owner"].get("name")
