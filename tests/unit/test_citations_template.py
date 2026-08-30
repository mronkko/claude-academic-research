"""Test 3 of `templates/test_citations.py` — the rule-2 backstop.

Test 3 (`no uncited 'Author (YYYY)' mentions`) is the *designed control*
for `grounded-citations` core rule 2: every in-text reference must be a
`[@citekey]`, never bare author-year prose. It runs inside the
`critic-loop` test gate, so a green result is what downstream consumers
trust.

It shipped matching only the **narrative** form (`Ert et al. (2016)`) and
was blind to the **parenthetical** form (`(Varma et al., 2016)`), which is
the dominant form in APA prose: `coauthor_re` consumed `et al.`, then the
pattern required `\\s*` before the year, which cannot match APA's comma.
A real manuscript with 106 parenthetical citations and zero `[@citekey]`
reported 5/5 passing.

These cases pin both forms so the blind spot cannot come back.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "templates"


def _module():
    """Load `templates/test_citations.py` by path, without writing a .pyc.

    `test_zotero_mcp_sync.py` scans `templates/**/*` as UTF-8, so a stray
    bytecode file there breaks an unrelated test.
    """
    sys.dont_write_bytecode = True
    added = str(TEMPLATES) not in sys.path
    if added:
        sys.path.insert(0, str(TEMPLATES))
    try:
        spec = importlib.util.spec_from_file_location(
            "template_test_citations", TEMPLATES / "test_citations.py"
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if added:
            sys.path.remove(str(TEMPLATES))


def _violations(prose: str, tmp_path: Path) -> int:
    """Run test 3 over `prose` and return the number of flagged mentions."""
    mod = _module()
    manuscript = tmp_path / "manuscript.qmd"
    manuscript.write_text(prose, encoding="utf-8")
    mod.MANUSCRIPT = str(manuscript)
    try:
        mod.test_no_uncited_author_year_mentions()
    except AssertionError as e:
        m = re.match(r"(\d+) uncited", str(e))
        assert m, f"unexpected failure message: {e}"
        return int(m.group(1))
    return 0


@pytest.mark.parametrize(
    ("prose", "expected"),
    [
        # Parenthetical APA — the form that was invisible.
        ("Peers rent to strangers (Ladegaard, 2018).", 1),
        ("Trust matters (Teubner & Flath, 2015).", 1),
        ("Tourism dominates (Varma et al., 2016; Birinci, 2018).", 2),
        # Narrative APA — worked before, must keep working.
        ("Ert et al. (2016) found a photo effect.", 1),
        ("Kolvereid (1992) surveyed founders.", 1),
        # Proper citekeys — must stay silent.
        ("Both agree [@schaefers2016] and [@rihova2018].", 0),
        ("Grouped keys [@varma2016; @birinci2018] are fine.", 0),
        # Suppressed-author form carries no bare year.
        ("Varma et al. [-@varma2016] report growth.", 0),
        # Structural words are not surnames.
        ("See Table 3, 2016 wave, for details.", 0),
        ("Collected in December, 2019, from Scopus.", 0),
    ],
)
def test_author_year_detection(prose: str, expected: int, tmp_path: Path) -> None:
    assert _violations(prose, tmp_path) == expected


def test_absent_manuscript_is_skipped(tmp_path: Path) -> None:
    """No manuscript → the test returns quietly rather than erroring."""
    mod = _module()
    mod.MANUSCRIPT = str(tmp_path / "does-not-exist.qmd")
    mod.test_no_uncited_author_year_mentions()
