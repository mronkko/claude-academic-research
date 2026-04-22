"""CLI-level tests for enrich_pdfs.py argument validation.

We avoid mocking the deep Zotero + Playwright machinery; these tests
just exercise the `argparse` surface and the cheap pre-Zotero
validation paths. Each test invokes the script via subprocess so
the argparser runs end-to-end (catching regressions in flag
naming / argparse-action drift).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "pipelines" / "enrich_pdfs.py"


def test_all_flag_appears_in_help() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True, text=True, check=True,
    )
    assert "--all" in result.stdout
    assert "Pass 1" in result.stdout and "Pass 2" in result.stdout


def test_all_rejects_combined_sources_flag() -> None:
    """--all and --sources are mutually exclusive: --all runs the
    API cascade first and then the browser pipeline on residuals;
    --sources <x> is an explicit single-source override."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--all", "--sources", "wiley"],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert "--all cannot be combined with --sources" in result.stderr


def test_sources_browser_still_works_on_its_own() -> None:
    """Regression: `--sources browser` without `--all` is the v0.3.x
    single-invocation browser mode. It should NOT trigger the
    mutually-exclusive error.

    We assert the parser accepts the combination by checking that
    the script fails LATER than argparse (exit code 0 or non-2 error
    from Zotero / filter-keys setup, but NOT the specific `--all
    cannot be combined` stderr line)."""
    result = subprocess.run(
        [
            sys.executable, str(SCRIPT),
            "--sources", "browser",
            "--filter-keys-file", "/tmp/does-not-exist-definitely",
        ],
        capture_output=True, text=True,
    )
    # Exact exit code depends on Zotero config / missing filter file,
    # but it must not be our specific 2 with the --all message.
    assert "--all cannot be combined with --sources" not in result.stderr
