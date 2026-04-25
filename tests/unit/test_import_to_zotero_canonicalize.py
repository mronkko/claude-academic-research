"""Tests for import_to_zotero.py source-canonicalization helpers (T2-4).

Scopus emits ISSNs as bare 8-digit strings (`00401625`) while WoS,
Crossref, and OpenAlex keep the L-form hyphen (`0040-1625`). Without
ingest-time canonicalization, two rows pointing at the same journal
across databases survive as duplicates only because their ISSN
strings don't compare equal — the user's session log shows this
shipping a `normalize_sources.py` band-aid, which is the workaround
this fix replaces.

The same applies to journal names: Scopus often abbreviates
("Strat Manag J") where WoS uses the full form ("Strategic Management
Journal"). The aliases CSV maps known variants to a canonical form.
"""

from __future__ import annotations

import import_to_zotero as imp

# ---------------------------------------------------------------------------
# _canonicalize_issn
# ---------------------------------------------------------------------------


def test_canonicalize_issn_inserts_hyphen_in_scopus_form() -> None:
    assert imp._canonicalize_issn("00401625") == "0040-1625"


def test_canonicalize_issn_passes_wos_form_through() -> None:
    assert imp._canonicalize_issn("0040-1625") == "0040-1625"


def test_canonicalize_issn_uppercases_check_digit_x() -> None:
    """ISSN check digit can be the literal X — must be normalized to upper case."""
    assert imp._canonicalize_issn("0317847x") == "0317-847X"


def test_canonicalize_issn_strips_surrounding_whitespace_and_other_chars() -> None:
    assert imp._canonicalize_issn("  0040-1625  ") == "0040-1625"
    assert imp._canonicalize_issn("ISSN: 00401625") == "0040-1625"


def test_canonicalize_issn_returns_empty_when_invalid() -> None:
    assert imp._canonicalize_issn("") == ""
    assert imp._canonicalize_issn("not-an-issn") == ""
    assert imp._canonicalize_issn("123") == ""              # too short
    assert imp._canonicalize_issn("12345678910") == ""      # too long


# ---------------------------------------------------------------------------
# _canonicalize_journal_name
# ---------------------------------------------------------------------------


def test_canonicalize_journal_name_maps_scopus_abbreviation() -> None:
    """`Strat Manag J` is the Scopus rendering — the alias table maps it
    to the WoS / common canonical form `Strategic Management Journal`."""
    assert (
        imp._canonicalize_journal_name("Strat Manag J")
        == "Strategic Management Journal"
    )


def test_canonicalize_journal_name_is_case_insensitive() -> None:
    assert (
        imp._canonicalize_journal_name("STRAT MANAG J")
        == "Strategic Management Journal"
    )


def test_canonicalize_journal_name_strips_surrounding_whitespace() -> None:
    assert (
        imp._canonicalize_journal_name("  Strat Manag J  ")
        == "Strategic Management Journal"
    )


def test_canonicalize_journal_name_falls_back_to_issn_when_variant_unknown() -> None:
    """Even if the variant string isn't in the alias table, an ISSN
    match recovers the canonical name. Catches new abbreviation forms
    we haven't yet seeded in journal_aliases.csv."""
    assert (
        imp._canonicalize_journal_name(
            "Strategic Mgmt Journ.",  # not in CSV
            issn="0143-2095",         # but ISSN is
        )
        == "Strategic Management Journal"
    )


def test_canonicalize_journal_name_returns_input_when_no_match() -> None:
    """Unknown name with unknown ISSN: pass-through. We never invent
    a canonical form — that would be worse than the variant."""
    assert (
        imp._canonicalize_journal_name("Quarterly Review of Biology")
        == "Quarterly Review of Biology"
    )


def test_canonicalize_journal_name_returns_empty_for_empty_input() -> None:
    assert imp._canonicalize_journal_name("") == ""
    assert imp._canonicalize_journal_name("", issn="0143-2095") == ""


# ---------------------------------------------------------------------------
# _row_to_zotero_item — integration with the canonicalization helpers
# ---------------------------------------------------------------------------


def test_row_to_zotero_item_writes_canonical_issn_and_source(monkeypatch) -> None:
    """The canonicalized values land in the Zotero payload — not the
    raw input. This is the contract that makes downstream dedup work."""
    # Defang the predatory check; not the focus of this test.
    monkeypatch.setattr(
        imp, "check_predatory", None, raising=False,
    )

    scopus_row = {
        "doi": "10.1002/smj.999",
        "title": "Some paper",
        "authors": "Doe, Jane",
        "year": "2020",
        "source": "Strat Manag J",       # Scopus abbreviation
        "issn": "01432095",                # Scopus no-hyphen ISSN
        "abstract": "",
        "query": "",
    }
    item = imp._row_to_zotero_item(scopus_row, collection_key=None)
    assert item["publicationTitle"] == "Strategic Management Journal"
    assert item["ISSN"] == "0143-2095"


def test_row_to_zotero_item_handles_missing_issn(monkeypatch) -> None:
    """Empty / malformed ISSN canonicalizes to "" — never a partial form."""
    monkeypatch.setattr(imp, "check_predatory", None, raising=False)
    row = {
        "doi": "10.1234/x",
        "title": "Some paper",
        "authors": "Doe, J",
        "year": "2020",
        "source": "Some Random Journal",
        "issn": "",
        "abstract": "",
        "query": "",
    }
    item = imp._row_to_zotero_item(row, collection_key=None)
    assert item["ISSN"] == ""
    # Unknown name with no ISSN: passthrough as-is.
    assert item["publicationTitle"] == "Some Random Journal"


def test_canonicalize_helpers_load_aliases_idempotently() -> None:
    """First call lazy-loads the table; subsequent calls reuse it.
    Verifies we don't re-read the CSV per row."""
    # Force a reload to a clean state, then call twice and assert the
    # second call is a no-op against the cached table.
    imp._JOURNAL_ALIAS_BY_NAME.clear()
    imp._JOURNAL_ALIAS_BY_ISSN.clear()
    imp._JOURNAL_ALIASES_LOADED = False

    imp._canonicalize_journal_name("Strat Manag J")
    name_table_size = len(imp._JOURNAL_ALIAS_BY_NAME)
    issn_table_size = len(imp._JOURNAL_ALIAS_BY_ISSN)
    assert name_table_size > 0
    assert issn_table_size > 0

    # Second call — table sizes unchanged.
    imp._canonicalize_journal_name("Acad Manag J")
    assert len(imp._JOURNAL_ALIAS_BY_NAME) == name_table_size
    assert len(imp._JOURNAL_ALIAS_BY_ISSN) == issn_table_size


def test_journal_aliases_csv_loads_without_errors() -> None:
    """Smoke-test the bundled data file: it parses cleanly and contains
    at least the canonical entries the pipeline relies on."""
    imp._JOURNAL_ALIAS_BY_NAME.clear()
    imp._JOURNAL_ALIAS_BY_ISSN.clear()
    imp._JOURNAL_ALIASES_LOADED = False
    imp._load_journal_aliases()
    # Spot-check a few entries we ship.
    assert imp._JOURNAL_ALIAS_BY_NAME["strat manag j"] == "Strategic Management Journal"
    assert imp._JOURNAL_ALIAS_BY_ISSN["0143-2095"] == "Strategic Management Journal"
    assert imp._JOURNAL_ALIAS_BY_NAME["res policy"] == "Research Policy"
