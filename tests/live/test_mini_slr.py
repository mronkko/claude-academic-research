"""Live end-to-end mini systematic review (BACKLOG.md "L1").

Drives `scripts/dev/mini_slr.py --stage all` — the whole pipeline (search,
trim, Zotero import, sync, enrichment, audit, abstract screening,
full-text coding, export) against real APIs and a real, disposable Zotero
group, then verifies the run against `templates/test_systematic_review.py`
copied into the run directory. Real API spend; real wall-clock time
(~5-12 min, see BACKLOG.md's Cost/runtime note). Opt in with
`pytest -m live_slr`.

One-time setup (not automated — groups can only be created on zotero.org):
see tests/live/README.md's "live_slr one-time setup" section.

Set MINI_SLR_KEEP=1 to skip teardown on success (inspect the run's Zotero
items/collection and output/e2e/<run-id>/ artefacts afterwards; you are
then responsible for `uv run scripts/dev/mini_slr.py --stage teardown
--run-id <id>` yourself).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_ROOT = REPO_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))
if str(SCRIPTS_ROOT / "pipelines") not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT / "pipelines"))

DRIVER = REPO_ROOT / "scripts" / "dev" / "mini_slr.py"

# BACKLOG.md estimates ~5-12 min; give real headroom for slow publisher
# APIs / a busy Zotero Desktop before treating the run as hung.
RUN_TIMEOUT_S = 1800


def _require_e2e_group() -> None:
    """Skip cleanly unless Zotero Desktop is reachable with a local write
    key on file and the dedicated e2e group has been hand-created — see
    this module's docstring and tests/live/README.md."""
    from tests.live.conftest import e2e_group

    e2e_group()


@pytest.mark.live_slr
def test_mini_slr_end_to_end() -> None:
    _require_e2e_group()

    cmd = ["uv", "run", str(DRIVER), "--stage", "all"]
    if os.environ.get("MINI_SLR_KEEP", "").strip():
        cmd.append("--keep")

    result = subprocess.run(
        cmd, cwd=REPO_ROOT, capture_output=True, text=True,
        timeout=RUN_TIMEOUT_S,
    )
    if result.returncode != 0:
        pytest.fail(
            f"mini_slr.py --stage all exited {result.returncode}.\n"
            f"--- stdout (tail) ---\n{result.stdout[-4000:]}\n"
            f"--- stderr (tail) ---\n{result.stderr[-4000:]}"
        )
