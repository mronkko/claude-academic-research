"""Screening and coding calls are pinned to temperature 0.

The systematic-review skill promises reproducible screening, and the
project test template (`templates/test_systematic_review.py`) delegates
this check to the plugin when the project uses the shipped scripts by
path — which is every project scaffolded since the scripts stopped being
copied. Until this test existed nothing checked it, so the template's
check passed vacuously.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PIPELINES = Path(__file__).resolve().parents[2] / "scripts" / "pipelines"
SCRIPTS = ("abstract_screen.py", "fulltext_code.py")


def _temperature_values(tree: ast.AST) -> list[ast.AST]:
    values: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "temperature":
            values.append(node.value)
        elif isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values, strict=True):
                if isinstance(k, ast.Constant) and k.value == "temperature":
                    values.append(v)
    return values


@pytest.mark.parametrize("script", SCRIPTS)
def test_every_temperature_in_screening_scripts_is_zero(script: str) -> None:
    tree = ast.parse((PIPELINES / script).read_text(encoding="utf-8"))
    values = _temperature_values(tree)
    assert values, f"{script}: no temperature setting found at all"
    for v in values:
        assert isinstance(v, ast.Constant) and v.value == 0, (
            f"{script}:{v.lineno}: temperature is {ast.unparse(v)}, not a literal 0"
        )
