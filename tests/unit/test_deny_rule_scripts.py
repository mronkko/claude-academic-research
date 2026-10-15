"""add_deny_rules.py / check_deny_rules.py: project-level deny guard.

These stay after P20 (the wizard writes no rules): `empirical-integrity`
uses them to protect `analysis/results/**`. They run as subprocesses in
a scratch project directory, the way skills invoke them.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SETUP = Path(__file__).resolve().parents[2] / "scripts" / "setup"
RULE = "Edit(analysis/results/**)"


def _run(script: str, cwd: Path, *rules: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SETUP / script), *rules],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )


def test_add_then_check(tmp_path) -> None:
    r = _run("add_deny_rules.py", tmp_path, RULE)
    assert r.returncode == 0, r.stderr
    assert "added" in r.stdout
    assert _run("check_deny_rules.py", tmp_path, RULE).stdout.strip() == "ok"
    r = _run("add_deny_rules.py", tmp_path, RULE)
    assert "already present" in r.stdout


def test_add_preserves_other_settings_and_non_ascii(tmp_path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    original = {"permissions": {"allow": ["Bash(ls)"]}, "note": "Rönkkö — ✓"}
    settings.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    assert _run("add_deny_rules.py", tmp_path, RULE).returncode == 0
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["note"] == "Rönkkö — ✓"
    assert data["permissions"]["allow"] == ["Bash(ls)"]
    assert data["permissions"]["deny"] == [RULE]


@pytest.mark.parametrize("body", [
    "{not json",
    "[1, 2]",
    '{"permissions": []}',
    '{"permissions": {"deny": "Edit(x)"}}',
])
def test_malformed_settings_are_reported_and_untouched(tmp_path, body) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(body, encoding="utf-8")

    add = _run("add_deny_rules.py", tmp_path, RULE)
    assert add.returncode == 3
    assert "Traceback" not in add.stderr
    assert settings.read_text(encoding="utf-8") == body

    check = _run("check_deny_rules.py", tmp_path, RULE)
    assert check.returncode == 3
    assert check.stdout.startswith("error:")
    assert "Traceback" not in check.stderr


def test_check_without_settings_reports_missing(tmp_path) -> None:
    r = _run("check_deny_rules.py", tmp_path, RULE)
    assert r.stdout.strip() == f"missing: {RULE}"
    assert not (tmp_path / ".claude").exists()
