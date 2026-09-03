"""Per-stream database selection: `--keyword-databases` / `--citation-databases`.

`--databases` was one flat list applied to both streams, which forces an
all-or-nothing choice on a database that is good for one stream and
useless for the other. That is not hypothetical and not merely
ergonomic. Semantic Scholar returns no ISSN, so it cannot be scoped to a
journal list at the source at all — its keyword contribution to a
journal-restricted review is structurally weak — while for a citation
search it found roughly 50% more citing works than OpenAlex on a real
seed. A protocol wanting the second without the first had no way to say
so, and the project that hit this resolved it by admitting the database
to both streams and documenting a deviation that had not actually
happened.

`--databases` stays the default for both streams; each per-stream flag
overrides it for that stream only.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import search as search_mod

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "pipelines" / "search.py"

ALL = ["scopus", "wos", "openalex", "semantic_scholar"]


def _resolve(**kw):
    return search_mod._resolve_stream_databases(
        default=kw.get("default", ALL),
        keyword=kw.get("keyword", ""),
        citation=kw.get("citation", ""),
        available=ALL,
    )


def test_without_overrides_both_streams_get_the_default() -> None:
    keyword, citation = _resolve(default=["openalex", "wos"])
    assert keyword == ["openalex", "wos"]
    assert citation == ["openalex", "wos"]


def test_a_keyword_override_does_not_touch_the_citation_stream() -> None:
    keyword, citation = _resolve(
        default=["openalex", "semantic_scholar"], keyword="openalex",
    )
    assert keyword == ["openalex"]
    assert citation == ["openalex", "semantic_scholar"]


def test_a_citation_override_does_not_touch_the_keyword_stream() -> None:
    """The case that prompted this: Semantic Scholar for citations only."""
    keyword, citation = _resolve(
        default=["openalex", "wos"], citation="openalex,semantic_scholar",
    )
    assert keyword == ["openalex", "wos"]
    assert citation == ["openalex", "semantic_scholar"]


def test_both_streams_can_be_overridden_independently() -> None:
    keyword, citation = _resolve(keyword="wos,scopus", citation="openalex")
    assert keyword == ["wos", "scopus"]
    assert citation == ["openalex"]


def test_whitespace_around_names_is_tolerated() -> None:
    keyword, _ = _resolve(keyword=" wos , scopus ")
    assert keyword == ["wos", "scopus"]


def test_an_unknown_name_in_an_override_is_rejected() -> None:
    """Silently ignoring a typo would drop a database the protocol
    declared and report a complete search anyway."""
    with pytest.raises(SystemExit) as exc:
        _resolve(keyword="openalx")
    assert "openalx" in str(exc.value)


def test_the_error_names_the_flag_that_was_wrong() -> None:
    with pytest.raises(SystemExit) as exc:
        _resolve(citation="nope")
    assert "--citation-databases" in str(exc.value)


def test_an_override_may_name_a_database_outside_the_default() -> None:
    """`--databases` is a default, not a ceiling. Asking for a database
    in one stream should not require adding it to both."""
    keyword, citation = _resolve(
        default=["openalex"], citation="openalex,semantic_scholar",
    )
    assert keyword == ["openalex"]
    assert "semantic_scholar" in citation


def test_an_empty_override_means_run_no_database_for_that_stream() -> None:
    """Distinct from omitting the flag. `--keyword-databases ''` is how a
    citation-only run is expressed without disabling the keyword stream
    wholesale."""
    keyword, citation = _resolve(keyword="none")
    assert keyword == []
    assert citation == ALL


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_both_flags_appear_in_help() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True, text=True, check=True,
    )
    assert "--keyword-databases" in result.stdout
    assert "--citation-databases" in result.stdout
