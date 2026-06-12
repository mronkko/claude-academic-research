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
    assert isinstance(m["plugins"], list) and len(m["plugins"]) >= 1
    by_name = {p["name"]: p for p in m["plugins"]}

    assert "academic-research" in by_name, "marketplace must list the academic-research plugin"
    assert (
        by_name["academic-research"]["source"] == "./"
    ), "academic-research source must be './' for root-repo hosting"

    # The repo is a marketplace hosting more than one plugin; editorial-tools
    # is sourced from its own subdirectory.
    assert "editorial-tools" in by_name, "marketplace must list the editorial-tools plugin"
    assert (
        by_name["editorial-tools"]["source"] == "./editorial-tools"
    ), "editorial-tools source must point at its subdirectory"


def test_marketplace_owner_present() -> None:
    m = _load("marketplace.json")
    assert "owner" in m and m["owner"].get("name")


def test_antigravity_academic_research_plugin_manifest() -> None:
    ag_path = REPO / "plugin.json"
    claude_path = REPO / ".claude-plugin" / "plugin.json"
    
    assert ag_path.is_file(), "Antigravity plugin.json manifest missing at root"
    assert claude_path.is_file(), "Claude Code plugin.json manifest missing at .claude-plugin/"
    
    ag_manifest = json.loads(ag_path.read_text(encoding="utf-8"))
    claude_manifest = json.loads(claude_path.read_text(encoding="utf-8"))
    
    # Assert exact keys for Antigravity manifest schema
    assert set(ag_manifest.keys()) == {"name", "description", "disabled"}
    assert ag_manifest["disabled"] is False
    
    # Bidirectional sync validation
    assert ag_manifest["name"] == claude_manifest["name"], "Plugin 'name' mismatch between manifests"
    assert ag_manifest["description"] == claude_manifest["description"], "Plugin 'description' mismatch between manifests"


def test_antigravity_editorial_tools_plugin_manifest() -> None:
    ag_path = REPO / "editorial-tools" / "plugin.json"
    claude_path = REPO / "editorial-tools" / ".claude-plugin" / "plugin.json"
    
    assert ag_path.is_file(), "Antigravity plugin.json manifest missing in editorial-tools/"
    assert claude_path.is_file(), "Claude Code plugin.json manifest missing in editorial-tools/.claude-plugin/"
    
    ag_manifest = json.loads(ag_path.read_text(encoding="utf-8"))
    claude_manifest = json.loads(claude_path.read_text(encoding="utf-8"))
    
    # Assert exact keys for Antigravity manifest schema
    assert set(ag_manifest.keys()) == {"name", "description", "disabled"}
    assert ag_manifest["disabled"] is False
    
    # Bidirectional sync validation
    assert ag_manifest["name"] == claude_manifest["name"], "Editorial tools 'name' mismatch between manifests"
    assert ag_manifest["description"] == claude_manifest["description"], "Editorial tools 'description' mismatch between manifests"

