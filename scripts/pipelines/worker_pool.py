"""A thread pool that stops sending work when its caller stops reading.

The screening orchestrators and `run_manifest.py` submit every item up
front and read results with `as_completed`. With a plain
`with ThreadPoolExecutor(...)`, an exception in that loop — a CSV write
that fails, a Zotero error, Ctrl+C — leaves the `with` block, whose exit
*waits for every queued future to run*. Every remaining item is then sent
to the model anyway: paid for, and its result thrown away.

`cancelling_pool` cancels whatever has not started the moment the body
raises (any exception, `KeyboardInterrupt` included), then waits only for
the calls already in flight. Stdlib-only.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager


@contextmanager
def cancelling_pool(max_workers: int) -> Iterator[ThreadPoolExecutor]:
    pool = ThreadPoolExecutor(max_workers=max(1, int(max_workers or 1)))
    try:
        yield pool
    except BaseException as exc:
        pool.shutdown(wait=False, cancel_futures=True)
        if isinstance(exc, KeyboardInterrupt):
            print(
                "\nInterrupted: queued calls cancelled; waiting for the "
                "ones already in flight to return.",
                file=sys.stderr, flush=True,
            )
        raise
    finally:
        pool.shutdown(wait=True)
