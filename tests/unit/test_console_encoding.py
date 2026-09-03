"""Scripts must survive an output encoding that cannot spell their output.

Python takes stdout's encoding from the locale when stdout is not a
terminal. On Windows that is normally cp1252, which cannot encode most of
what this plugin prints — em dashes, arrows, the `──` section rules, the
`•` bullets in run reports — so printing any of them raises
UnicodeEncodeError and kills the script mid-run.

It only happens when output is redirected, because an attached Windows
console handles UTF-8 separately. That is why it survived a CI matrix
that includes Windows: nothing captured a script's stdout there until a
test ran one as a subprocess, and then a completed search died on its own
progress banner.

`PYTHONIOENCODING` reproduces it on any platform, which is what makes
this testable at all — the bug is about the encoding, not about Windows.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

#: Every entry point that prints a character cp1252 cannot represent.
#: Kept as a literal list rather than rediscovered at runtime so that a
#: new script printing an arrow has to be added here deliberately.
NON_ASCII_ENTRY_POINTS = [
    "scripts/cluster/run_batch.py",
    "scripts/dev/mini_slr.py",
    "scripts/dev/probe_browser_handler.py",
    "scripts/pipelines/abstract_screen.py",
    "scripts/pipelines/audit_zotero_library.py",
    "scripts/pipelines/build_journal_list_from_abs.py",
    "scripts/pipelines/enrich_abstracts.py",
    "scripts/pipelines/enrich_dois.py",
    "scripts/pipelines/enrich_pdfs.py",
    "scripts/pipelines/export_coded_includes.py",
    "scripts/pipelines/filter_search_results.py",
    "scripts/pipelines/fulltext_code.py",
    "scripts/pipelines/generate_bib.py",
    "scripts/pipelines/import_to_zotero.py",
    "scripts/pipelines/pilot_analyze.py",
    "scripts/pipelines/run_manifest.py",
    "scripts/pipelines/search.py",
    "scripts/setup/check_cluster_config.py",
    "scripts/setup/check_llm_provider.py",
    "scripts/setup/check_model_connection.py",
    "scripts/setup/check_zotero_mcp_version.py",
    "scripts/setup/resolve_models.py",
    "scripts/setup/scaffold_project.py",
    "scripts/setup/set_cluster_automation.py",
    "scripts/setup/set_llm_provider.py",
    "scripts/setup/wizard.py",
]


def test_the_helper_makes_a_hostile_stream_safe() -> None:
    """The unit the entry points delegate to."""
    code = (
        "import sys; sys.path.insert(0, r'%s');"
        "from core.console import enable_utf8_output;"
        "enable_utf8_output();"
        "print('\\u2500\\u2500 rule \\u2192 arrow \\u2022 bullet')"
    ) % (REPO / "scripts")
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    done = subprocess.run([sys.executable, "-c", code],
                          capture_output=True, text=True, env=env)
    assert done.returncode == 0, done.stderr
    assert "rule" in done.stdout


def test_the_helper_is_idempotent() -> None:
    """Called twice — once by an entry point, once by something it
    imports — must not raise or undo itself."""
    code = (
        "import sys; sys.path.insert(0, r'%s');"
        "from core.console import enable_utf8_output;"
        "enable_utf8_output(); enable_utf8_output();"
        "print('\\u2192 ok')"
    ) % (REPO / "scripts")
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    done = subprocess.run([sys.executable, "-c", code],
                          capture_output=True, text=True, env=env)
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("script", NON_ASCII_ENTRY_POINTS)
def test_entry_point_survives_a_cp1252_pipe(script: str) -> None:
    """`--help` is enough: argparse prints the description, and the
    process has already run every module-level statement — which is
    where the encoding has to have been fixed. A script that crashes
    here crashes on its first progress line in a real Windows run.
    """
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    done = subprocess.run(
        [sys.executable, str(REPO / script), "--help"],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert "UnicodeEncodeError" not in done.stderr, (
        f"{script} cannot write its own output under cp1252:\n{done.stderr}"
    )


def test_the_list_of_affected_entry_points_is_still_accurate() -> None:
    """A new script that prints an arrow must be added above rather than
    silently going unprotected. Scans for characters cp1252 cannot
    encode, which is the exact condition that crashes the process.
    """
    missing = []
    for path in sorted(REPO.glob("scripts/**/*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "\nif __name__ ==" not in text:
            continue
        rel = path.relative_to(REPO).as_posix()
        if rel in NON_ASCII_ENTRY_POINTS:
            continue
        for line in text.splitlines():
            if "print(" not in line and 'f"' not in line:
                continue
            try:
                line.encode("cp1252")
            except UnicodeEncodeError:
                missing.append(rel)
                break
    assert not missing, (
        f"entry point(s) print characters cp1252 cannot encode but are not "
        f"listed in NON_ASCII_ENTRY_POINTS: {missing}. Add the "
        f"enable_utf8_output() call and list them."
    )
