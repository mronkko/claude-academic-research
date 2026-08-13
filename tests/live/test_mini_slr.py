"""Live end-to-end mini systematic review (BACKLOG.md "L1").

Drives `scripts/dev/mini_slr.py --stage all` — the whole pipeline (search,
trim, Zotero import, sync, enrichment, audit, abstract screening,
full-text coding, export) against real APIs and a real, disposable Zotero
group, then verifies the run against `templates/test_systematic_review.py`
copied into the run directory. Real API spend; real wall-clock time
(~5-12 min, see BACKLOG.md's Cost/runtime note). Opt in with
`pytest -m live_slr`.

One-time setup (not automated — the Zotero Web API cannot create groups):
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
GROUP_NAME = "academic-research-e2e"

# BACKLOG.md estimates ~5-12 min; give real headroom for slow publisher
# APIs / Zotero sync before treating the run as hung.
RUN_TIMEOUT_S = 1800


def _require_e2e_group() -> None:
    """Skip cleanly if ZOTERO_API_KEY isn't configured or the dedicated
    e2e group hasn't been hand-created yet — see this module's docstring
    and tests/live/README.md."""
    import zotero_io
    from core.config_loader import get

    api_key = get("zotero", "api_key", env="ZOTERO_API_KEY")
    if not api_key:
        pytest.skip("ZOTERO_API_KEY (or config [zotero].api_key) not set; "
                    "skipping live_slr test.")
    user_id = get("zotero", "user_id", env="ZOTERO_USER_ID")
    if not user_id:
        pytest.skip("ZOTERO_USER_ID not set (re-run /setup); skipping "
                    "live_slr test.")
    try:
        group = zotero_io.find_group_by_name(GROUP_NAME, api_key=api_key,
                                              user_id=user_id)
    except ValueError as e:
        pytest.skip(f"ambiguous Zotero group name {GROUP_NAME!r}: {e}")
    if group is None:
        pytest.skip(
            f"no Zotero group named {GROUP_NAME!r} is accessible. "
            "One-time setup: create it by hand at "
            "https://www.zotero.org/groups/new (Private membership is "
            "fine), let Zotero Desktop sync it locally, then re-run. "
            "See tests/live/README.md."
        )


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
