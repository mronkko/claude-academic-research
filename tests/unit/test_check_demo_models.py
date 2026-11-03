"""check_demo_models: the demo's model line matches what the run resolves."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "setup"))

import check_demo_models  # noqa: E402
from core import models  # noqa: E402


def test_lines_name_the_models_the_demo_will_use():
    lines = check_demo_models.status_lines("anthropic")
    fast = models.resolve_model("fast", provider="anthropic")
    balanced = models.resolve_model("balanced", provider="anthropic")
    assert lines[0] == "provider: anthropic"
    assert fast in lines[1] and "--model fast" in lines[1]
    assert balanced in lines[2] and "--model balanced" in lines[2]


def test_a_provider_without_catalogue_models_says_so():
    lines = check_demo_models.status_lines("openrouter")
    assert all("not resolved" in ln for ln in lines[1:])


def test_main_prints_three_lines(capsys):
    assert check_demo_models.main() == 0
    assert len(capsys.readouterr().out.strip().splitlines()) == 3
