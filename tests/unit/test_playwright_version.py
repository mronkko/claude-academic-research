"""Newest Playwright, and a clear ask when its Chromium is missing.

Each Playwright release drives exactly one Chromium build. The policy is
to run the newest release — uv resolves it whenever it rebuilds a script
environment — and to ask the user to install the matching browser,
*before* any work starts, naming the exact command for the version that
is actually running. A pin was tried and rejected: it freezes the
browser at whatever was current when someone last edited a header.

What went wrong without the ask: on 2026-09-22 uv rebuilt the
enrich_pdfs environment with 1.63.0 (chromium-1243) while the machine
held 1208 and 1234, and the pass died at launch, after the whole
Zotero read and resolver pre-flight, with only "not installed".
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _files():
    for sub in ("scripts", "skills", "tests/live"):
        for path in (REPO / sub).rglob("*"):
            if path.suffix in (".py", ".md") and path.is_file():
                yield path
    yield REPO / "pyproject.toml"


def test_no_playwright_requirement_is_pinned() -> None:
    offenders = [
        f"{path.relative_to(REPO)}: {m.group(0)}"
        for path in _files()
        for m in re.finditer(r'"playwright\s*(==|~=|<)[^"]*"', path.read_text())
    ]
    assert not offenders, offenders


def test_uvx_install_instructions_ask_for_the_latest() -> None:
    """Bare `uvx playwright` can reuse an older cached tool, installing a
    browser for a version the script no longer runs."""
    offenders = [
        f"{path.relative_to(REPO)}: {m.group(0)}"
        for path in _files()
        for m in re.finditer(r"uvx playwright(@\S+)? install", path.read_text())
        if m.group(1) not in ("@latest", "@{version}")
    ]
    assert not offenders, offenders


def test_missing_chromium_names_the_running_version(tmp_path) -> None:
    from fetchers.browser.base import chromium_install_problem

    present = tmp_path / "chrome"
    present.write_text("")
    assert chromium_install_problem(str(present), "1.63.0") is None
    msg = chromium_install_problem(str(tmp_path / "chromium-1243/chrome"), "1.63.0")
    assert "uvx playwright@1.63.0 install chromium" in msg
    assert "1.63.0" in msg


def test_the_launch_error_names_the_running_version(monkeypatch) -> None:
    import asyncio

    from fetchers.browser import base

    monkeypatch.setattr(base, "_playwright_version", lambda: "1.63.0")

    class _Chromium:
        async def launch_persistent_context(self, **kw):
            raise Exception("Executable doesn't exist at /x/chromium-1243/chrome")

    class _PW:
        chromium = _Chromium()

    try:
        asyncio.run(base.launch_context(_PW(), "/tmp/pw-version-test"))
    except RuntimeError as e:
        assert "uvx playwright@1.63.0 install chromium" in str(e)
    else:
        raise AssertionError("expected RuntimeError")


def test_the_browser_check_runs_before_any_work() -> None:
    import inspect

    import enrich_pdfs

    src = inspect.getsource(enrich_pdfs.main)
    assert src.index("_check_browser_ready(") < src.index("os.makedirs(args.cache_dir")
