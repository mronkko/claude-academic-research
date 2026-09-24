"""The translator poll reads the Playwright page's tab, not the active one.

2026-09-24, JYU: the user signed in to EZproxy in a new tab right after
"Ready to start?". The poll asked for the active tab of the current
window — the sign-in tab — and the first two items were logged
ACCESS_BLOCKED "no translator detected" although their pages were fine.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess

import pytest
from fetchers.browser import connector


class _Worker:
    def __init__(self):
        self.calls = []

    async def evaluate(self, js, *args):
        self.calls.append((js, args))
        return 3


class _Page:
    url = "https://academic-oup-com.ezproxy.jyu.fi/cesifo/article/66/1/1/1"


def test_the_poll_passes_the_page_url_to_the_tab_lookup() -> None:
    w = _Worker()
    assert asyncio.run(connector._wait_for_translators(w, _Page(), timeout_s=1)) == 3
    js, args = w.calls[0]
    assert args == (_Page.url,)
    assert "x.url === pageUrl" in js
    # The active tab is only the last resort.
    assert js.index("x.url === pageUrl") < js.index("active: true")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_poll_script_parses() -> None:
    w = _Worker()
    asyncio.run(connector._wait_for_translators(w, _Page(), timeout_s=1))
    js = w.calls[0][0]
    out = subprocess.run(["node", "--check", "-"], input=f"const f = {js};",
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
