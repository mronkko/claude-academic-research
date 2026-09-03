"""`--rerun` has to retry the error rows, and only those.

Its own help said "Re-process items whose last logged decision is
`error`" and the skill said "`--rerun` retries only those". Neither was
true. The flag selected nothing; it only removed the guard that was
keeping error rows out of the ordinary population:

    if last_decision == "error" and not args.rerun:
        continue

Every untagged item was coded either way. A user retrying 7 JSON-parse
failures got 182 items processed, and two of them — abstract-excluded,
so never eligible in the first place — were given real `fulltext:exclude`
tags. That is the same silent-promotion this stage's abstract filter was
just fixed to prevent, arriving through a different door.

The abstract filter (0.20.0) already stops that specific contamination,
because it narrows the collection before this logic runs. What it does
not do is make the flag mean what it says: on a completed run the two
coincide, and on an interrupted one, or a collection that has gained new
items, `--rerun` still codes everything untagged. A flag whose documented
selector is trusted for a narrow, paid retry has to actually select.
"""

from __future__ import annotations

import fulltext_code


def _item(key: str) -> dict:
    return {"key": key, "data": {"title": key, "tags": []}}


ITEMS = [_item("done"), _item("err"), _item("fresh"), _item("err2")]
TAGGED = {"done"}
LAST = {"done": "include", "err": "error", "err2": "error"}


def _select(*, rerun: bool) -> list[str]:
    return [it["key"] for it in fulltext_code._select_to_code(
        ITEMS, tagged=TAGGED, last_decisions=LAST, rerun=rerun,
    )]


# ---------------------------------------------------------------------------
# --rerun selects
# ---------------------------------------------------------------------------


def test_rerun_takes_the_error_rows_and_nothing_else() -> None:
    """The documented contract, now implemented. `fresh` has never been
    coded, but the user asked for a retry, not for a resume."""
    assert _select(rerun=True) == ["err", "err2"]


def test_rerun_leaves_a_tagged_item_alone() -> None:
    """A decision already written is not an error row, whatever the CSV
    said earlier."""
    assert "done" not in _select(rerun=True)


def test_rerun_with_no_error_rows_selects_nothing() -> None:
    """Honest emptiness. Previously this coded the whole untagged
    population, which is how a 7-item retry became 182."""
    picked = fulltext_code._select_to_code(
        ITEMS, tagged=TAGGED, last_decisions={"done": "include"}, rerun=True,
    )
    assert picked == []


# ---------------------------------------------------------------------------
# The default is unchanged
# ---------------------------------------------------------------------------


def test_a_plain_run_codes_untagged_non_error_items() -> None:
    """Nothing loses coverage: the error rows stay skipped by default,
    exactly as before, and are reachable with the flag."""
    assert _select(rerun=False) == ["fresh"]


def test_a_plain_run_still_skips_error_rows() -> None:
    assert "err" not in _select(rerun=False)


def test_between_them_the_two_modes_cover_every_untagged_item() -> None:
    """The property that makes this safe to change: one invocation each
    reaches everything the single old invocation did."""
    covered = set(_select(rerun=False)) | set(_select(rerun=True))
    untagged = {it["key"] for it in ITEMS} - TAGGED
    assert covered == untagged


# ---------------------------------------------------------------------------
# Contradictory flags
# ---------------------------------------------------------------------------


def test_rerun_and_full_recode_are_mutually_exclusive() -> None:
    """`--full-recode` means "code everything again"; `--rerun` means
    "only the failures". Together they cannot both be honoured, and
    silently picking one is how a flag stops meaning what it says."""
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    done = subprocess.run(
        [sys.executable, str(repo / "scripts" / "pipelines" / "fulltext_code.py"),
         "--collection", "X", "--config", "c.py", "--rerun", "--full-recode"],
        capture_output=True, text=True,
    )
    assert done.returncode == 2
    assert "--rerun" in done.stderr and "--full-recode" in done.stderr


def test_the_help_no_longer_promises_a_selector_it_lacks() -> None:
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    out = subprocess.run(
        [sys.executable, str(repo / "scripts" / "pipelines" / "fulltext_code.py"),
         "--help"], capture_output=True, text=True, check=True,
    ).stdout
    assert "--rerun" in out
    assert "only" in out.lower()
