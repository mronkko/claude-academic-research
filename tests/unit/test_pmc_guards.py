"""PMC fetcher: a bounded proof-of-work and a guarded ID conversion."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import MagicMock

from fetchers import pmc


def test_pow_still_solves_the_difficulty_pmc_serves() -> None:
    nonce = pmc._solve_pow("abc", 3)
    assert nonce is not None
    assert hashlib.sha256(f"abc{nonce}".encode()).hexdigest().startswith("000")


def test_pow_refuses_an_out_of_bounds_difficulty() -> None:
    """Unbounded, a difficulty of 12 pinned a worker thread for days."""
    assert pmc._solve_pow("abc", pmc._MAX_POW_DIFFICULTY + 1) is None


def test_pow_gives_up_when_the_budget_runs_out(monkeypatch) -> None:
    monkeypatch.setattr(pmc, "_POW_BUDGET_FACTOR", 0)
    assert pmc._solve_pow("abc", 2) is None


def test_a_non_json_idconv_answer_is_a_miss_not_a_crash(tmp_path) -> None:
    """A 200 carrying an HTML maintenance page raised out of the fetcher."""
    def bad_json():
        raise ValueError("Expecting value")

    http = MagicMock()
    http.get.return_value = SimpleNamespace(status_code=200, json=bad_json)
    src = pmc.PmcSource(http=http, config=None)
    assert src.fetch_pdf("10.1/x", cache_dir=tmp_path) is None
