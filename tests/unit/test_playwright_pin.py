"""One Playwright version, everywhere a Chromium build is chosen.

Each Playwright release drives exactly one Chromium build. The script
headers said `playwright>=1.40` and every install instruction said
`uvx playwright install chromium`, so the two were resolved separately:
on 2026-09-22 uv rebuilt the enrich_pdfs environment with 1.63.0, which
wants chromium-1243, while the machine held 1208 and 1234 from earlier
installs. The download then timed out, and a working browser pass died at
launch with only "not installed" to go on.

So the version is pinned once, in `fetchers.browser.base`, and every
header, install instruction and permission rule must name it.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _pin() -> str:
    src = (REPO / "scripts/pipelines/fetchers/browser/base.py").read_text()
    m = re.search(r'^PLAYWRIGHT_VERSION = "([\d.]+)"', src, re.M)
    assert m, "fetchers/browser/base.py must define PLAYWRIGHT_VERSION"
    return m.group(1)


def _files():
    for sub in ("scripts", "skills", "tests/live"):
        for path in (REPO / sub).rglob("*"):
            if path.suffix in (".py", ".md") and path.is_file():
                yield path
    yield REPO / "pyproject.toml"


def test_every_playwright_requirement_is_the_pin() -> None:
    pin = _pin()
    offenders = []
    for path in _files():
        for m in re.finditer(r'"playwright([<>=~!][^"]*)?"', path.read_text()):
            if m.group(1) and m.group(1) != f"=={pin}":
                offenders.append(f"{path.relative_to(REPO)}: playwright{m.group(1)}")
    assert not offenders, offenders


def test_every_uvx_install_instruction_names_the_pin() -> None:
    pin = _pin()
    offenders = [
        f"{path.relative_to(REPO)}: {m.group(0)}"
        for path in _files()
        for m in re.finditer(r"uvx playwright(@[\d.]+)? install", path.read_text())
        if m.group(1) != f"@{pin}"
    ]
    assert not offenders, offenders


def test_the_launch_error_gives_the_pinned_install_command() -> None:
    import asyncio

    from fetchers.browser import base

    class _Chromium:
        async def launch_persistent_context(self, **kw):
            raise Exception("Executable doesn't exist at /x/chromium-9999/chrome")

    class _PW:
        chromium = _Chromium()

    try:
        asyncio.run(base.launch_context(_PW(), "/tmp/pw-pin-test"))
    except RuntimeError as e:
        assert f"uvx playwright@{_pin()} install chromium" in str(e)
    else:
        raise AssertionError("expected RuntimeError")
