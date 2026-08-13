"""`no_pdf` is a decision, not a reason string.

"No PDF to read" and "coding blew up" were both logged as
`decision=error` and told apart afterwards by matching the literal
`reason` text. Two things went wrong with that. The CSV disagreed with
the summary printed next to it — a run showed `error: 0, no_pdf: 2` over
two rows that both said `error` — and `verify` reported "2 items still in
error state" for items whose only problem was a missing file, pointing at
the model instead of at retrieval.

They stay equally unresolved: neither gets a stage tag, and `verify` must
keep failing on both. The point is only that the log says which one you
have, because the fix differs — find the PDF, versus re-run the model.
"""

from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path

import fulltext_code
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

FIELDS = [{"name": "sample"}, {"name": "method"}]
BASE = {
    "item_key": "ABC12345", "doi": "10.1/x", "title": "A paper",
    "year": "2019", "journal": "JBV", "model": "claude-sonnet-4-6",
}


# --- the log row -------------------------------------------------------

def test_missing_pdf_is_not_logged_as_an_error() -> None:
    row = fulltext_code.no_pdf_row(BASE, FIELDS)
    assert row["decision"] == "no_pdf"
    assert row["decision"] != "error", (
        "the whole point: retrieval failures stop masquerading as "
        "model failures"
    )


def test_the_reason_still_says_what_happened() -> None:
    """Downstream readers and operators read `reason`; only the
    classification moved, not the explanation."""
    assert fulltext_code.no_pdf_row(BASE, FIELDS)["reason"] == (
        "no PDF attachment found"
    )


def test_row_carries_identity_and_blank_coding_fields() -> None:
    row = fulltext_code.no_pdf_row(BASE, FIELDS)
    assert row["item_key"] == "ABC12345"
    assert row["fulltext_chars"] == 0 and row["pdf_path"] == ""
    assert row["sample"] == "" and row["method"] == ""


def test_no_pdf_is_not_a_taggable_decision() -> None:
    """error / no_pdf stay untagged in Zotero so a re-run picks them up;
    tagging one would strand it as if it had been decided."""
    assert fulltext_code.NO_PDF_DECISION not in fulltext_code.STAGE_TAG_VALUES


def test_both_unresolved_states_are_named_together() -> None:
    assert set(fulltext_code.UNRESOLVED_DECISIONS) == {"error", "no_pdf"}


# --- the guard that must not weaken ------------------------------------

@pytest.fixture(scope="module")
def verify_template():
    """Load templates/test_systematic_review.py, which ships to user
    projects and is what actually gates a review."""
    path = REPO_ROOT / "templates" / "test_systematic_review.py"
    sys.path.insert(0, str(REPO_ROOT / "templates"))
    try:
        spec = importlib.util.spec_from_file_location("verify_template", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["verify_template"] = module
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(REPO_ROOT / "templates"))
    return module


def _write_log(path: Path, rows: list[tuple[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["item_key", "decision"])
        w.writeheader()
        for key, decision in rows:
            w.writerow({"item_key": key, "decision": decision})


@pytest.mark.parametrize("state", ["error", "no_pdf"])
def test_verify_fails_on_either_unresolved_state(
    verify_template, tmp_path, monkeypatch, state,
) -> None:
    """Matching only `error` would have let every missing-PDF item
    through the moment those stopped being logged as errors — the exact
    regression this state split could have introduced."""
    log = tmp_path / "fulltext_screening.csv"
    _write_log(log, [("AAA", "include"), ("BBB", state)])
    monkeypatch.setattr(verify_template, "FULLTEXT_LOG", str(log))

    with pytest.raises(AssertionError) as exc:
        verify_template.test_no_remaining_errors_in_fulltext_log()

    assert "BBB" in str(exc.value)


def test_verify_passes_when_everything_is_decided(
    verify_template, tmp_path, monkeypatch,
) -> None:
    log = tmp_path / "fulltext_screening.csv"
    _write_log(log, [("AAA", "include"), ("BBB", "exclude")])
    monkeypatch.setattr(verify_template, "FULLTEXT_LOG", str(log))

    verify_template.test_no_remaining_errors_in_fulltext_log()
