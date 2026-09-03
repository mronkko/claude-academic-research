"""Restricting the citation stream to the protocol's journal list.

Stream B was built open-scope on purpose: a method travels outside the
journals a protocol names, and escaping venue scope is what the stream
adds over a keyword search. But an open stream can also be mostly noise.
On one real review, a seed returned 1839 citing works of which 107
(5.8%) were in the 22 target journals; the other 1732 spanned 760 venues
the review had no interest in — marketing, environmental health, general
psychology — which were fetched, deduplicated and imported before being
trashed by hand.

So scope is now applied by default whenever `JOURNALS` is non-empty, and
`--citation-journal-scope off` opts out.

That default was a judgment call, made against the recommendation in this
file's own history, and it has a cost worth naming: the same config
returns a different corpus before and after this release with no flag
change. Two things exist to keep that from being silent — the resolved
choice is recorded in `search_metadata.json` and printed in the run
banner, and the metadata now stamps the plugin version that produced it.

Where the filter runs differs by source, and the difference is not
cosmetic. OpenAlex ANDs a venue filter into the `cites:` query
server-side: measured live on one seed, 1670 citing works open-scope
against 68 scoped, so the records are never transferred. Semantic
Scholar's `/citations` endpoint takes no venue parameter at all, so
scope there can only be applied after the fact — the API calls happen
either way, and only the import and screening cost is saved.
"""

from __future__ import annotations

import json

import pytest
import search as search_mod
from searchers import OpenAlexSearch, SemanticScholarSearch
from searchers.base import SearchContext

SEED = "10.1037/0021-9010.91.4.917"
JAP = "Journal of Applied Psychology"
ISSNS = ["0021-9010", "0001-4273"]


def _ctx(*, scope: bool, issns=None, titles=None) -> SearchContext:
    return SearchContext(
        from_year=2007, to_year=2020,
        issns=ISSNS if issns is None else issns,
        journal_titles=[JAP] if titles is None else titles,
        citation_journal_scope=scope,
    )


# ---------------------------------------------------------------------------
# Resolving the flag
# ---------------------------------------------------------------------------


def test_auto_scopes_when_the_config_names_journals() -> None:
    assert search_mod._resolve_citation_scope("auto", issns=ISSNS) is True


def test_auto_does_not_scope_when_no_journals_are_named() -> None:
    """Nothing to scope to. Scoping against an empty list would return an
    empty stream, which is worse than the open behaviour it replaced."""
    assert search_mod._resolve_citation_scope("auto", issns=[]) is False


def test_off_opts_out_even_with_journals_named() -> None:
    """The escape hatch for the stream's original open-scope intent."""
    assert search_mod._resolve_citation_scope("off", issns=ISSNS) is False


def test_on_scopes_even_with_no_journals_named() -> None:
    """Explicit request, honoured as given; the searchers then have
    nothing to filter on and behave as unscoped."""
    assert search_mod._resolve_citation_scope("on", issns=[]) is True


def test_the_context_defaults_to_unscoped() -> None:
    """Backward compatible: every construction that predates the field."""
    ctx = SearchContext(from_year=2007, to_year=2020, issns=ISSNS)
    assert ctx.citation_journal_scope is False


# ---------------------------------------------------------------------------
# OpenAlex — server-side, in the filter
# ---------------------------------------------------------------------------


class _FakeOpenAlex(OpenAlexSearch):
    def __init__(self):
        super().__init__()
        self.filters: list[str] = []

    def _resolve_work_id(self, doi, ctx):
        return "W2015257908"

    def _fetch_page_cursor(self, filter_str, cursor, ctx):
        self.filters.append(filter_str)
        return {"results": [], "meta": {"next_cursor": None}}


def test_openalex_ands_the_issn_filter_into_the_cites_query() -> None:
    """Server-side, so the out-of-scope records are never transferred —
    the whole efficiency argument for this feature."""
    src = _FakeOpenAlex()
    src.run_citations([SEED], _ctx(scope=True))
    assert "cites:W2015257908" in src.filters[0]
    assert "primary_location.source.issn:0021-9010|0001-4273" in src.filters[0]


def test_openalex_keeps_the_year_window_when_scoped() -> None:
    src = _FakeOpenAlex()
    src.run_citations([SEED], _ctx(scope=True))
    assert "publication_year:2007-2020" in src.filters[0]


def test_openalex_omits_the_issn_filter_when_unscoped() -> None:
    src = _FakeOpenAlex()
    src.run_citations([SEED], _ctx(scope=False))
    assert "issn" not in src.filters[0]


def test_openalex_omits_the_issn_filter_when_scoped_but_no_issns() -> None:
    """`--citation-journal-scope on` with no JOURNALS must not produce
    `issn:` with an empty value — OpenAlex would reject the filter and
    the run would fail on a flag combination that is merely pointless."""
    src = _FakeOpenAlex()
    src.run_citations([SEED], _ctx(scope=True, issns=[]))
    assert "issn" not in src.filters[0]


# ---------------------------------------------------------------------------
# Semantic Scholar — client-side, after the fact
# ---------------------------------------------------------------------------

def _s2_paper(journal_name: str, paper_id: str = "p1") -> dict:
    return {
        "paperId": paper_id,
        "title": "A citing paper",
        "year": 2015,
        "externalIds": {"DOI": f"10.1/{paper_id}"},
        "authors": [{"name": "A B"}],
        "journal": {"name": journal_name},
        "publicationTypes": ["JournalArticle"],
    }


class _FakeS2(SemanticScholarSearch):
    def __init__(self, papers):
        super().__init__()
        self._papers = papers

    def _fetch_citations(self, doi, ctx, api_key):
        return self._papers


def test_s2_drops_out_of_scope_citing_papers_when_scoped() -> None:
    src = _FakeS2([_s2_paper(JAP, "keep"),
                   _s2_paper("Journal of Marketing", "drop")])
    rows = src.run_citations([SEED], _ctx(scope=True))
    assert [r["s2_paper_id"] for r in rows] == ["keep"]


def test_s2_keeps_everything_when_unscoped() -> None:
    src = _FakeS2([_s2_paper(JAP, "a"), _s2_paper("Journal of Marketing", "b")])
    rows = src.run_citations([SEED], _ctx(scope=False))
    assert {r["s2_paper_id"] for r in rows} == {"a", "b"}


def test_s2_scoped_still_applies_the_year_window() -> None:
    old = {**_s2_paper(JAP, "old"), "year": 1990}
    rows = _FakeS2([_s2_paper(JAP, "new"), old]).run_citations(
        [SEED], _ctx(scope=True),
    )
    assert [r["s2_paper_id"] for r in rows] == ["new"]


def test_s2_scoped_matches_a_journal_title_variant() -> None:
    """Same normalisation the keyword stream uses — S2 renders one
    journal several ways."""
    src = _FakeS2([_s2_paper("The Journal of applied psychology", "v")])
    assert len(src.run_citations([SEED], _ctx(scope=True))) == 1


# ---------------------------------------------------------------------------
# Provenance: the mitigation for a changed default
# ---------------------------------------------------------------------------


def test_metadata_records_the_resolved_scope_choice(tmp_path) -> None:
    meta = _run_search_metadata(tmp_path, extra=[])
    assert meta["citation_journal_scope"] is True


def test_metadata_records_an_opt_out(tmp_path) -> None:
    meta = _run_search_metadata(
        tmp_path, extra=["--citation-journal-scope", "off"],
    )
    assert meta["citation_journal_scope"] is False


def test_metadata_stamps_the_plugin_version(tmp_path) -> None:
    """A search config does not determine a corpus on its own: 0.16.0 and
    0.17.0 return different keyword corpora from identical configs. The
    date and database list alone could not explain that."""
    from plugin_version import plugin_version
    meta = _run_search_metadata(tmp_path, extra=[])
    assert meta["plugin_version"] == plugin_version()
    assert meta["plugin_version"] != "unknown"


def _run_search_metadata(tmp_path, *, extra: list[str]) -> dict:
    """Drive `search.py` end to end with no network, return its metadata."""
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    cfg = tmp_path / "search_config.py"
    cfg.write_text(
        "FROM_YEAR = 2007\n"
        "TO_YEAR = 2008\n"
        "JOURNALS = {'0021-9010': ('ABS 4*', 'Journal of Applied Psychology')}\n"
        "BLOCK_A_TERMS = []\n"
        "BLOCK_B_TERMS = []\n"
        f"CITATION_SEEDS = ['{SEED}']\n",
        encoding="utf-8",
    )
    # Hermetic without tripping the "no database selected" guard: OpenAlex
    # is selected for the keyword stream, but the config defines no block
    # terms, so `_run_keyword_stream` skips before making any request. The
    # citation stream gets no database. Metadata is still written, and the
    # scope resolution under test does not depend on any source running.
    result = subprocess.run(
        [sys.executable, str(repo / "scripts" / "pipelines" / "search.py"),
         "--config", str(cfg),
         "--databases", "openalex",
         "--citation-databases", "none",
         "--output-dir", str(tmp_path / "out"),
         "--metadata-dir", str(tmp_path / "meta"), *extra],
        capture_output=True, text=True,
    )
    meta_path = tmp_path / "meta" / "search_metadata.json"
    assert meta_path.is_file(), (
        f"search.py wrote no metadata (rc={result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return json.loads(meta_path.read_text(encoding="utf-8"))


def test_the_banner_names_the_scope(tmp_path, capsys) -> None:
    """Printed, not just recorded. A changed default that nobody sees is
    the failure mode this whole feature was warned about."""
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    cfg = tmp_path / "search_config.py"
    cfg.write_text(
        "FROM_YEAR = 2007\nTO_YEAR = 2008\n"
        "JOURNALS = {'0021-9010': ('ABS 4*', 'Journal of Applied Psychology')}\n"
        "BLOCK_A_TERMS = []\nBLOCK_B_TERMS = []\n"
        f"CITATION_SEEDS = ['{SEED}']\n", encoding="utf-8",
    )
    out = subprocess.run(
        [sys.executable, str(repo / "scripts" / "pipelines" / "search.py"),
         "--config", str(cfg), "--databases", "openalex",
         "--citation-databases", "none",
         "--output-dir", str(tmp_path / "o"),
         "--metadata-dir", str(tmp_path / "m")],
        capture_output=True, text=True,
    ).stdout
    assert "journal-scoped" in out


def test_the_flag_appears_in_help() -> None:
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    out = subprocess.run(
        [sys.executable, str(repo / "scripts" / "pipelines" / "search.py"),
         "--help"], capture_output=True, text=True, check=True,
    ).stdout
    assert "--citation-journal-scope" in out


def test_an_unknown_scope_value_is_rejected() -> None:
    with pytest.raises(SystemExit):
        search_mod._resolve_citation_scope("maybe", issns=ISSNS)


# ---------------------------------------------------------------------------
# One version reader
# ---------------------------------------------------------------------------


def test_the_pdf_stamp_and_the_search_metadata_agree_on_the_version() -> None:
    """Two artefacts stamp a version and both must name the same release:
    the provenance line on a TDM-recovered PDF, which a later run uses to
    decide whether that cached file predates a transformation change, and
    `search_metadata.json`. Separate readers could drift, and the drift
    would be invisible until someone compared two artefacts from one run.
    """
    from fetchers.sciencedirect import _plugin_version
    from plugin_version import plugin_version

    assert _plugin_version() == plugin_version() != "unknown"


def test_a_missing_manifest_reads_as_unknown(tmp_path) -> None:
    """A stamp must never raise: it annotates work, it is not the work."""
    from plugin_version import plugin_version
    assert plugin_version(tmp_path / "nope.json") == "unknown"


def test_a_malformed_manifest_reads_as_unknown(tmp_path) -> None:
    bad = tmp_path / "plugin.json"
    bad.write_text("{not json", encoding="utf-8")
    from plugin_version import plugin_version
    assert plugin_version(bad) == "unknown"
