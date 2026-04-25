"""Tests for scripts/setup/scaffold_project.py (T3-1 reshaped).

Replaces the bash mkdir + cp dance for setting up a fresh SLR project
with a single shipped script. Per user feedback (2026-04-25):
> Creating project structure should be a script instead of bash commands.

The wizard's allowlist therefore intentionally drops `mkdir` and `cp`
from auto-approval; this script does the same job behind a single
allow-rule the wizard already covers.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAFFOLD = REPO_ROOT / "scripts" / "setup" / "scaffold_project.py"


def _load_module():
    """Load scaffold_project.py as a module without invoking main()."""
    spec = importlib.util.spec_from_file_location("scaffold_project", SCAFFOLD)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def scaffold():
    return _load_module()


# ---------------------------------------------------------------------------
# ensure_dirs
# ---------------------------------------------------------------------------


def test_ensure_dirs_creates_missing_directories(tmp_path: Path, scaffold) -> None:
    created = scaffold.ensure_dirs(tmp_path, ("foo", "bar/nested"))
    assert (tmp_path / "foo").is_dir()
    assert (tmp_path / "bar" / "nested").is_dir()
    assert {p.name for p in created} == {"foo", "nested"}


def test_ensure_dirs_is_idempotent_on_existing_dirs(tmp_path: Path, scaffold) -> None:
    """Running scaffold twice must not error or report directories
    twice — that would mislead the user about what changed on a re-run."""
    (tmp_path / "foo").mkdir()
    created = scaffold.ensure_dirs(tmp_path, ("foo", "bar"))
    # foo was already there → not in `created`. bar was new → in `created`.
    assert {p.name for p in created} == {"bar"}


# ---------------------------------------------------------------------------
# copy_templates
# ---------------------------------------------------------------------------


def test_copy_templates_copies_missing_files(tmp_path: Path, scaffold) -> None:
    """Happy path: SR layout copies real templates from the plugin."""
    new, kept = scaffold.copy_templates(tmp_path, scaffold.SR_TEMPLATE_COPIES)
    # At least the CLAUDE.md + screening_config + manuscript.qmd should be created.
    paths = {p.relative_to(tmp_path).as_posix() for p in new}
    assert "CLAUDE.md" in paths
    assert "screening_config.py" in paths
    assert "manuscript/manuscript.qmd" in paths
    assert kept == []


def test_copy_templates_respects_existing_files(tmp_path: Path, scaffold) -> None:
    """User edits to e.g. screening_config.py must survive a re-run.
    The script never overwrites — pre-existing files land in `kept`."""
    user_edit = "USER LOCAL EDIT — must survive scaffold rerun"
    (tmp_path / "screening_config.py").write_text(user_edit, encoding="utf-8")
    new, kept = scaffold.copy_templates(tmp_path, scaffold.SR_TEMPLATE_COPIES)
    assert (tmp_path / "screening_config.py").read_text(encoding="utf-8") == user_edit
    kept_paths = {p.relative_to(tmp_path).as_posix() for p in kept}
    assert "screening_config.py" in kept_paths


def test_copy_templates_creates_intermediate_dirs(tmp_path: Path, scaffold) -> None:
    """Templates that target nested paths (manuscript/manuscript.qmd) need
    their parent directory created when missing."""
    new, _ = scaffold.copy_templates(tmp_path, scaffold.SR_TEMPLATE_COPIES)
    qmd = tmp_path / "manuscript" / "manuscript.qmd"
    assert qmd.is_file()
    assert qmd in new


# ---------------------------------------------------------------------------
# update_gitignore
# ---------------------------------------------------------------------------


def test_update_gitignore_appends_only_missing_entries(
    tmp_path: Path, scaffold,
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text(".claude/\n", encoding="utf-8")
    added = scaffold.update_gitignore(tmp_path, (".claude/", "output/"))
    assert added == ["output/"]
    content = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    # Pre-existing line preserved.
    assert ".claude/" in content
    # Missing line appended.
    assert "output/" in content


def test_update_gitignore_creates_file_when_missing_inside_repo(
    tmp_path: Path, scaffold,
) -> None:
    (tmp_path / ".git").mkdir()
    added = scaffold.update_gitignore(tmp_path, (".claude/",))
    assert added == [".claude/"]
    assert (tmp_path / ".gitignore").is_file()


def test_update_gitignore_skips_outside_git_repo(
    tmp_path: Path, scaffold,
) -> None:
    """Don't create / modify .gitignore when there's no git repo —
    surprises the user otherwise."""
    added = scaffold.update_gitignore(tmp_path, (".claude/",))
    assert added == []
    assert not (tmp_path / ".gitignore").exists()


def test_update_gitignore_respects_commented_entries(
    tmp_path: Path, scaffold,
) -> None:
    """If `.claude/` is already present as a comment-out line, that
    counts as 'present' — don't add a duplicate."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text(
        "# Ignore Claude internals\n# .claude/\n", encoding="utf-8",
    )
    added = scaffold.update_gitignore(tmp_path, (".claude/",))
    assert added == []


# ---------------------------------------------------------------------------
# CLI integration — exercise the script end-to-end via subprocess.
# ---------------------------------------------------------------------------


def test_cli_sr_layout_creates_full_project(tmp_path: Path) -> None:
    """End-to-end: invoke the script as a subprocess (CI-equivalent)
    and verify the standard SLR layout lands."""
    rc = subprocess.run(
        ["python3", str(SCAFFOLD), "--target", str(tmp_path), "--kind", "sr"],
        check=False,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True,
    )
    assert rc.returncode == 0, rc.stderr.decode()
    assert (tmp_path / "screening").is_dir()
    assert (tmp_path / "manuscript").is_dir()
    assert (tmp_path / "critic-reviews").is_dir()
    assert (tmp_path / "CLAUDE.md").is_file()
    assert (tmp_path / "screening_config.py").is_file()


def test_cli_manuscript_layout_omits_screening_dirs(tmp_path: Path) -> None:
    """Manuscript-only kind should NOT create screening/ pilot/ analysis/."""
    rc = subprocess.run(
        ["python3", str(SCAFFOLD), "--target", str(tmp_path), "--kind", "manuscript"],
        check=False,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True,
    )
    assert rc.returncode == 0, rc.stderr.decode()
    assert (tmp_path / "manuscript").is_dir()
    assert not (tmp_path / "screening").exists()
    assert not (tmp_path / "pilot").exists()
    assert not (tmp_path / "analysis").exists()


def test_cli_idempotent_on_rerun(tmp_path: Path) -> None:
    """Running twice must not error; user customisations survive."""
    rc1 = subprocess.run(
        ["python3", str(SCAFFOLD), "--target", str(tmp_path), "--kind", "sr"],
        check=False, env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True,
    )
    assert rc1.returncode == 0
    config_path = tmp_path / "screening_config.py"
    config_path.write_text("USER EDIT", encoding="utf-8")
    rc2 = subprocess.run(
        ["python3", str(SCAFFOLD), "--target", str(tmp_path), "--kind", "sr"],
        check=False, env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True,
    )
    assert rc2.returncode == 0
    assert config_path.read_text(encoding="utf-8") == "USER EDIT"


def test_cli_exits_on_missing_target(tmp_path: Path) -> None:
    rc = subprocess.run(
        ["python3", str(SCAFFOLD),
         "--target", str(tmp_path / "does-not-exist")],
        check=False, env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True,
    )
    assert rc.returncode != 0
    assert b"not a directory" in rc.stderr or b"not a directory" in rc.stdout
