"""G5: install_templates.py stays inside templates/ and the project."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "setup" / "install_templates.py"


@pytest.fixture
def it():
    spec = importlib.util.spec_from_file_location("install_templates_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    proj = tmp_path / "project"
    proj.mkdir()
    monkeypatch.chdir(proj)
    return proj


def test_copies_a_template_into_the_project(it, project) -> None:
    assert it.main(["search_config.py:scripts/search_config.py"]) == 0
    assert (project / "scripts" / "search_config.py").is_file()


def test_skip_if_exists_and_force(it, project) -> None:
    dest = project / "search_config.py"
    dest.write_text("mine", encoding="utf-8")
    assert it.main(["search_config.py:search_config.py"]) == 0
    assert dest.read_text(encoding="utf-8") == "mine"
    assert it.main(["--force", "search_config.py:search_config.py"]) == 0
    assert dest.read_text(encoding="utf-8") != "mine"


@pytest.mark.parametrize("dest", [
    "../outside.py",
    "sub/../../outside.py",
    ".",
])
def test_refuses_destinations_outside_the_project(it, project, dest, capsys) -> None:
    victim = project.parent / "outside.py"
    victim.write_text("precious", encoding="utf-8")
    assert it.main(["--force", f"search_config.py:{dest}"]) == 2
    assert victim.read_text(encoding="utf-8") == "precious"
    assert "refused" in capsys.readouterr().err


def test_refuses_an_absolute_destination(it, project, tmp_path) -> None:
    victim = tmp_path / "abs.py"
    victim.write_text("precious", encoding="utf-8")
    assert it.main(["--force", f"search_config.py:{victim}"]) == 2
    assert victim.read_text(encoding="utf-8") == "precious"


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_refuses_a_symlink_leading_out_of_the_project(it, project, tmp_path) -> None:
    victim = tmp_path / "victim.py"
    victim.write_text("precious", encoding="utf-8")
    os.symlink(victim, project / "link.py")
    assert it.main(["--force", "search_config.py:link.py"]) == 2
    assert victim.read_text(encoding="utf-8") == "precious"


@pytest.mark.parametrize("src", ["../scripts/setup/wizard.py", "../../../../etc/hosts"])
def test_refuses_sources_outside_templates(it, project, src) -> None:
    assert it.main([f"{src}:copied.py"]) == 2
    assert not (project / "copied.py").exists()
