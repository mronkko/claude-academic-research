"""One failing database must not discard the others' results.

Seen live: a Semantic Scholar 429 raised out of `source.run()` and killed
the process, throwing away completed Scopus (156 rows), WoS (191) and
OpenAlex (310) queries — and the API quota they had already cost — before
anything was written to disk. The retry had to re-pay for all three.

The fix keeps the run a hard failure, because a corpus assembled from a
subset of its declared databases is not the corpus the protocol
describes, and `search_run.json`'s DOI hash is what downstream stages
treat as proof of a complete search. What changes is that the rows that
were paid for survive, and every failure is reported at once instead of
the first one aborting the loop.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest
import search
from searchers import SEARCH_ROW_FIELDS

CONFIG = """
FROM_YEAR = 2019
TO_YEAR = 2019
JOURNALS = {"0883-9026": ("n/a", "Journal of Business Venturing")}
"""


class _Source:
    """A search source that either yields rows or raises."""

    def __init__(self, name: str, *, rows: int = 0,
                 error: Exception | None = None) -> None:
        self.name = name
        self._rows = rows
        self._error = error

    def credentials_error(self, _ctx) -> str | None:
        return None

    def run(self, _cfg, _ctx) -> list[dict]:
        if self._error:
            raise self._error
        return [
            {**{f: "" for f in SEARCH_ROW_FIELDS},
             "doi": f"10.1/{self.name}{i}", "title": f"{self.name} {i}"}
            for i in range(self._rows)
        ]


@pytest.fixture
def run_search(tmp_path, monkeypatch):
    """Invoke search.main() over a fake registry, in an isolated tree."""
    def _run(sources: list[_Source]):
        cfg_path = tmp_path / "search_config.py"
        cfg_path.write_text(CONFIG, encoding="utf-8")
        out = tmp_path / "raw"
        meta = tmp_path / "meta"
        monkeypatch.setattr(
            search, "searchers_by_name",
            lambda: {s.name: s for s in sources},
        )
        monkeypatch.setattr(sys, "argv", [
            "search.py", "--config", str(cfg_path),
            "--output-dir", str(out), "--metadata-dir", str(meta),
        ])
        return search.main, out, meta
    return _run


def _rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# --- the regression ----------------------------------------------------

def test_a_failing_database_does_not_discard_the_others(run_search) -> None:
    main, out, meta = run_search([
        _Source("alpha", rows=3),
        _Source("bravo", error=RuntimeError("stayed rate-limited")),
        _Source("charlie", rows=2),
    ])

    with pytest.raises(SystemExit) as exc:
        main()

    partial = out / "search_results_raw.partial.csv"
    assert partial.is_file(), "the rows already paid for must survive"
    assert len(_rows(partial)) == 5, (
        "alpha's 3 and charlie's 2 both kept — charlie runs *after* the "
        "failure, so an early abort would lose it too"
    )
    assert "stayed rate-limited" in str(exc.value)


def test_the_incomplete_search_is_not_certified(run_search) -> None:
    """The dedup CSV and search_run.json are what downstream stages read
    as a complete, reproducible corpus. Writing them here would certify a
    search that never finished."""
    main, out, meta = run_search([
        _Source("alpha", rows=3),
        _Source("bravo", error=RuntimeError("boom")),
    ])

    with pytest.raises(SystemExit):
        main()

    assert not (out / "search_results.csv").exists()
    assert not (meta / "search_run.json").exists()


def test_every_failure_is_reported_not_just_the_first(run_search) -> None:
    main, _, _ = run_search([
        _Source("alpha", error=RuntimeError("alpha down")),
        _Source("bravo", error=RuntimeError("bravo down")),
        _Source("charlie", rows=1),
    ])

    with pytest.raises(SystemExit) as exc:
        main()

    msg = str(exc.value)
    assert "alpha down" in msg and "bravo down" in msg
    assert "2 of 3 database(s) failed" in msg
    assert "charlie=1" in msg, "say which ones did work, and how much"


def test_all_databases_succeeding_is_unaffected(run_search) -> None:
    """The happy path must still write the real artefacts."""
    main, out, meta = run_search([
        _Source("alpha", rows=2), _Source("bravo", rows=1),
    ])

    assert main() == 0
    assert not (out / "search_results_raw.partial.csv").exists()
    assert (out / "search_results.csv").is_file()
    assert (meta / "search_run.json").is_file()
    assert len(_rows(out / "search_results.csv")) == 3
