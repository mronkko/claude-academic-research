"""Queued model calls are cancelled when the result loop dies (D3).

With a plain `with ThreadPoolExecutor(...)`, an exception or Ctrl+C in
the `as_completed` loop left the block, whose exit waited for every
queued future to run — sending (and paying for) every remaining item.
"""

from __future__ import annotations

import ast
import threading
from concurrent.futures import as_completed
from pathlib import Path

import pytest
import worker_pool

PIPELINES = Path(__file__).resolve().parents[2] / "scripts" / "pipelines"


@pytest.mark.parametrize("exc", [RuntimeError("csv write failed"),
                                 KeyboardInterrupt()])
def test_an_exception_in_the_loop_cancels_queued_calls(exc) -> None:
    started: list[int] = []
    gate = threading.Event()

    def call(i: int) -> int:
        started.append(i)
        if i == 0:
            return i
        gate.wait(2)
        return i

    with pytest.raises(type(exc)):
        with worker_pool.cancelling_pool(1) as pool:
            futures = [pool.submit(call, i) for i in range(20)]
            for fut in as_completed(futures):
                fut.result()
                raise exc
    gate.set()
    # One worker: call 0 finished, at most call 1 was in flight; the
    # other 18 were queued and must never have run.
    assert len(started) <= 2
    assert sum(f.cancelled() for f in futures) >= 18


def test_a_clean_exit_still_runs_everything() -> None:
    with worker_pool.cancelling_pool(4) as pool:
        futures = [pool.submit(lambda i=i: i * 2) for i in range(10)]
    assert sorted(f.result() for f in futures) == [i * 2 for i in range(10)]


@pytest.mark.parametrize("name", [
    "abstract_screen.py", "fulltext_code.py", "run_manifest.py",
])
def test_every_screening_pool_cancels_on_error(name) -> None:
    """No bare ThreadPoolExecutor left where as_completed drives paid calls."""
    tree = ast.parse((PIPELINES / name).read_text(encoding="utf-8"))
    bare = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and (
            (isinstance(n.func, ast.Name) and n.func.id == "ThreadPoolExecutor")
            or (isinstance(n.func, ast.Attribute)
                and n.func.attr == "ThreadPoolExecutor")
        )
    ]
    assert not bare, f"{name}: use worker_pool.cancelling_pool"
