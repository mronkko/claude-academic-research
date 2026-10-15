"""generate_bib: Pandoc citation-key extraction and scaffold-layout defaults.

The key regex used to capture trailing sentence punctuation
(`@smith2020.` became the key `smith2020.`, which no Zotero item has) and
the domain of every e-mail address (`jane@uni.edu` became `uni.edu`). The
default paths pointed at `chapters/` and `<project>/references.bib`, while
the scaffold every skill creates is `manuscript/` with the bib beside
`manuscript.qmd` — so the documented no-flag invocation failed on every
scaffolded project.
"""

from __future__ import annotations

from pathlib import Path

import generate_bib
import pytest


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("as shown by @smith2020.", {"smith2020"}),
        ("@smith2020: a claim", {"smith2020"}),
        ("[@smith2020; @doe2019, p. 5]", {"smith2020", "doe2019"}),
        ("[-@smith2020]", {"smith2020"}),
        ("@brownUsingDailyStock1985a, too", {"brownUsingDailyStock1985a"}),
        # Internal punctuation is part of the key.
        ("@key:sub.part-two/x", {"key:sub.part-two/x"}),
        ("@Foo_bar", {"Foo_bar"}),
        # Pandoc allows a digit or underscore first.
        ("@2020report and @_private", {"2020report", "_private"}),
        # Braced keys may hold characters bare keys cannot.
        ("see @{Smith, J. 2020}.", {"Smith, J. 2020"}),
        # E-mail addresses and doubled @ are not citations.
        ("write to jane@uni.edu or jane.doe@uni.edu", set()),
        # Quarto cross-references share the syntax.
        ("See @fig-prisma and @tbl-methods and @sec-intro.", set()),
    ],
)
def test_extract_keys_from_text_follows_pandoc_rules(text: str, expected: set[str]) -> None:
    assert generate_bib.extract_keys_from_text(text) == expected


def test_code_is_not_scanned_for_keys() -> None:
    text = (
        "Prose cites @smith2020.\n\n"
        "```{python}\n@property\ndef x(self): ...\n```\n\n"
        "Inline `@decorator` code.\n\n~~~\n@tildefenced\n~~~\n"
    )
    keys = generate_bib.extract_keys_from_text(text)
    assert "smith2020" in keys
    assert "property" not in keys
    assert "decorator" not in keys
    assert "tildefenced" not in keys


def test_defaults_follow_the_scaffold_layout(tmp_path: Path) -> None:
    (tmp_path / "manuscript").mkdir()
    (tmp_path / "chapters").mkdir()  # present but ignored: manuscript/ wins
    src, bib = generate_bib.resolve_paths(tmp_path, None, None)
    assert src == tmp_path / "manuscript"
    assert bib == tmp_path / "manuscript" / "references.bib"


def test_chapters_layout_still_works_without_manuscript(tmp_path: Path) -> None:
    (tmp_path / "chapters").mkdir()
    src, bib = generate_bib.resolve_paths(tmp_path, None, None)
    assert src == tmp_path / "chapters"
    assert bib == tmp_path / "references.bib"


def test_overrides_resolve_against_project_dir(tmp_path: Path) -> None:
    src, bib = generate_bib.resolve_paths(tmp_path, "text", "out/refs.bib")
    assert src == tmp_path / "text"
    assert bib == tmp_path / "out" / "refs.bib"


def test_extract_keys_reads_md_and_qmd(tmp_path: Path) -> None:
    (tmp_path / "a.qmd").write_text("Cites @alpha2020.", encoding="utf-8")
    (tmp_path / "b.md").write_text("And [@beta2021].", encoding="utf-8")
    assert generate_bib.extract_keys(tmp_path) == ["alpha2020", "beta2021"]
