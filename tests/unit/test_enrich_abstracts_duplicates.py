"""A duplicate record must not inherit its sibling's "done" status.

`_already_done` keyed the resume set on the DOI, and `main()` filtered
the work list with it. One library held `10.1037/0882-7974.9.3.391`
three times; enrichment filled one copy, wrote the DOI to the log, and
from then on skipped the other two permanently. The abstract was in the
library and structurally invisible: a consumer joining on the DOI could
land on a copy that had none and could never acquire one. That library
had 229 duplicate-DOI groups, roughly 298 items in the same position.

The DOI key bought nothing in exchange. An item whose update succeeded
carries an `abstractNote` afterwards, so `main()`'s emptiness test
already excludes it on the next run — the DOI key could only ever
exclude *other* items. Keying on `item_key` keeps the resume guard and
drops the collateral damage.

Deduplicating the *lookup* is the separate half of the fix: three copies
of one article are three Zotero writes, but only one cascade.
"""

from __future__ import annotations

import csv

import enrich_abstracts


def _item(key: str, doi: str, *, abstract: str = "") -> dict:
    return {
        "key": key,
        "data": {"DOI": doi, "title": f"Title {key}", "abstractNote": abstract},
    }


def _write_log(path, rows: list[tuple[str, str, str]]) -> str:
    """rows are (item_key, doi, status)."""
    log = path / "abstract_fetch_log.csv"
    with open(log, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["run_date", "item_key", "doi", "title", "source",
                    "status", "detail"])
        for item_key, doi, status in rows:
            w.writerow(["2026-08-17", item_key, doi, "T", "crossref",
                        status, ""])
    return str(log)


# --- the resume set ---------------------------------------------------


def test_resume_set_is_keyed_on_the_item(tmp_path) -> None:
    log = _write_log(tmp_path, [("AAAA", "10.1/x", "updated")])

    assert enrich_abstracts._already_done(log) == {"aaaa"}


def test_a_sibling_of_an_enriched_item_is_not_marked_done(tmp_path) -> None:
    """The regression itself. BBBB is a second copy of AAAA's article."""
    log = _write_log(tmp_path, [("AAAA", "10.1/x", "updated")])

    done = enrich_abstracts._already_done(log)

    assert "aaaa" in done
    assert "bbbb" not in done


def test_only_successful_updates_resume(tmp_path) -> None:
    """`lookup_failed` means the abstract is unknown, not absent — those
    items must stay in the work list."""
    log = _write_log(tmp_path, [
        ("AAAA", "10.1/x", "updated"),
        ("BBBB", "10.1/y", "lookup_failed"),
        ("CCCC", "10.1/z", "not_found"),
    ])

    assert enrich_abstracts._already_done(log) == {"aaaa"}


def test_missing_log_resumes_nothing(tmp_path) -> None:
    assert enrich_abstracts._already_done(str(tmp_path / "absent.csv")) == set()


# --- one lookup per DOI, one write per item ---------------------------


def test_duplicates_collapse_into_one_group() -> None:
    items = [
        _item("AAAA", "10.1/x"),
        _item("BBBB", "10.1/x"),
        _item("CCCC", "10.1/y"),
    ]

    groups = enrich_abstracts.group_by_doi(items)

    assert list(groups) == ["10.1/x", "10.1/y"]
    assert [it["key"] for it in groups["10.1/x"]] == ["AAAA", "BBBB"]
    assert [it["key"] for it in groups["10.1/y"]] == ["CCCC"]


def test_grouping_normalises_case_and_whitespace() -> None:
    """Must agree with `load_done_keys`, which strips and lower-cases.

    If the two disagreed about identity, a DOI could group as one key and
    resume as another — reintroducing the same class of bug from the
    other side.
    """
    groups = enrich_abstracts.group_by_doi([
        _item("AAAA", " 10.1/X "),
        _item("BBBB", "10.1/x"),
    ])

    assert list(groups) == ["10.1/x"]
    assert len(groups["10.1/x"]) == 2


def test_items_without_a_doi_are_dropped() -> None:
    groups = enrich_abstracts.group_by_doi([
        _item("AAAA", ""),
        _item("BBBB", "   "),
        _item("CCCC", "10.1/y"),
    ])

    assert list(groups) == ["10.1/y"]


def test_every_copy_is_written_even_though_one_lookup_ran() -> None:
    """Grouping must not become a covert way of enriching one of N.

    The point of the group is to share the cascade, not the outcome:
    each copy is its own Zotero item and needs its own write.
    """
    items = [_item("AAAA", "10.1/x"), _item("BBBB", "10.1/x")]

    groups = enrich_abstracts.group_by_doi(items)

    assert sum(len(g) for g in groups.values()) == len(items)
