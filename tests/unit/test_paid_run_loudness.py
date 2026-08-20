"""What a run says before it starts spending money.

`cost_estimate_line` was printed only under `--dry-run`. A real
screening run went straight from "31 items in collection" to "Screening
with 8 parallel workers (model=…)" — no provider, no price, no pause.
In the session that prompted this, the dry run had itself failed (a
`--dry-run --remote` 403, fixed below), so the agent skipped it and ran
the paid stage with nothing on screen to say that is what was
happening.

Two separate fixes, tested here at the level each one lives at:

1. The banner is unconditional on the paid path.
2. `--dry-run --remote` loads the Zotero key, because remote reads hit
   api.zotero.org and an empty key is a 403 there.
"""

from __future__ import annotations

import argparse

from core import models

# ---------------------------------------------------------------------------
# The banner
# ---------------------------------------------------------------------------


def test_banner_names_provider_model_stage_and_count() -> None:
    banner = models.paid_run_banner(
        "claude-haiku-4-5", stage="abstract_screening", n_items=31,
        provider="anthropic",
    )
    assert "PAID LLM RUN" in banner
    assert "claude-haiku-4-5" in banner
    assert "anthropic" in banner
    assert "abstract_screening" in banner
    assert "31" in banner


def test_banner_carries_the_price() -> None:
    banner = models.paid_run_banner(
        "claude-haiku-4-5", stage="abstract_screening", n_items=1000,
        provider="anthropic",
    )
    assert "~$" in banner
    assert "1,000 item(s)" in banner


def test_banner_says_unknown_rather_than_free_for_an_unpriced_model() -> None:
    banner = models.paid_run_banner(
        "some-model-nobody-priced", stage="abstract_screening", n_items=10,
        provider="openrouter",
    )
    assert "unknown" in banner
    assert "$0" not in banner


def test_banner_is_honest_about_a_local_model() -> None:
    """A local provider is loud about the run, not about a cost that
    isn't there."""
    banner = models.paid_run_banner(
        "llama3:8b", stage="abstract_screening", n_items=10_000,
        provider="ollama",
    )
    assert "own machine" in banner
    assert "~$" not in banner


def test_banner_is_bordered_so_it_cannot_be_missed() -> None:
    banner = models.paid_run_banner(
        "claude-haiku-4-5", stage="abstract_screening", n_items=1,
        provider="anthropic",
    )
    lines = banner.splitlines()
    assert lines[0].startswith("===") and lines[-1].startswith("===")


# ---------------------------------------------------------------------------
# Where it is printed
# ---------------------------------------------------------------------------


def _source(name: str) -> str:
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    return (root / "scripts" / "pipelines" / name).read_text(encoding="utf-8")


def test_abstract_screen_prints_the_banner_before_the_worker_pool() -> None:
    src = _source("abstract_screen.py")
    banner_at = src.index("paid_run_banner(")
    workers_at = src.index('print(f"Screening with {args.workers}')
    assert banner_at < workers_at, (
        "the banner must precede the pool, not report it afterwards"
    )


def test_fulltext_code_prints_the_banner_on_both_paid_paths() -> None:
    """`--update-fields` re-codes items with the same model and the same
    per-paper cost; it is not a cheaper mode."""
    assert _source("fulltext_code.py").count("paid_run_banner(") == 2


def test_dry_run_paths_still_use_the_plain_cost_line() -> None:
    """`--dry-run` output is unchanged — it was already loud."""
    for name in ("abstract_screen.py", "fulltext_code.py"):
        assert "cost_estimate_line(" in _source(name)


# ---------------------------------------------------------------------------
# --dry-run --remote
# ---------------------------------------------------------------------------


def _key_needed(*, dry_run: bool, remote: bool) -> bool:
    """The predicate both orchestrators use, evaluated the same way."""
    args = argparse.Namespace(dry_run=dry_run, remote=remote)
    return not args.dry_run or getattr(args, "remote", False)


def test_a_remote_dry_run_needs_the_zotero_key() -> None:
    """The 403: remote reads go to api.zotero.org, and `api_key = ""`
    is rejected there. The run died before printing its cost quote."""
    assert _key_needed(dry_run=True, remote=True) is True


def test_a_local_dry_run_still_needs_no_key() -> None:
    assert _key_needed(dry_run=True, remote=False) is False


def test_a_real_run_always_needs_the_key() -> None:
    assert _key_needed(dry_run=False, remote=False) is True
    assert _key_needed(dry_run=False, remote=True) is True


def test_both_orchestrators_share_the_predicate() -> None:
    """Guard against fixing one script and leaving the other 403ing."""
    for name in ("abstract_screen.py", "fulltext_code.py"):
        src = _source(name)
        assert 'not args.dry_run or getattr(args, "remote", False)' in src, (
            f"{name} still gates the Zotero key on --dry-run alone"
        )
