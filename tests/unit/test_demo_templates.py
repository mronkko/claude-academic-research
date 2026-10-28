"""The demo-slr templates stay loadable and keep their contract."""

from __future__ import annotations

import importlib.util
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[2] / "templates"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TEMPLATES / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_demo_search_config_has_what_search_py_requires():
    mod = _load("demo_search_config")
    assert mod.JOURNALS == {} and hasattr(mod, "FROM_YEAR") and hasattr(mod, "TO_YEAR")


def test_demo_screening_config_codes_evidence_quotes_and_pins_no_model():
    mod = _load("demo_screening_config")
    names = [f["name"] for f in mod.FULLTEXT_CODING_FIELDS]
    assert "evidence_quotes" in names
    assert mod.ABSTRACT_SCREENING_MODEL == "" and mod.FULLTEXT_CODING_MODEL == ""
    assert "{coding_fields_json_placeholder}" in mod.FULLTEXT_CODING_SYSTEM_PROMPT
