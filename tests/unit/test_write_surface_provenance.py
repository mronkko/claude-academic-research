"""Recording which Zotero surface a write actually went to.

From 0.22.0 until the Web API was dropped, a write went to Zotero
Desktop's local API when `[zotero] local_api_key` was set, and to
`api.zotero.org` when it was not. The two libraries keep unrelated
version counters, so a reader reconciling a run-log afterwards against
item versions cannot tell which surface produced a row — and picking the
wrong one makes every version in it look wrong. Every write now goes to
Desktop and new rows say `local`, but logs written before still mix both,
so the column stays.

The log already records `source` (which publisher supplied the abstract)
and `status` (what happened). `surface` is the third fact needed to
reconcile: where it landed.

Empty is meaningful here and is not a gap. `no_doi`, `not_found`,
`lookup_failed` and `dry_run` rows performed no write at all, and naming
a surface for them would assert something that never happened.

Deliberately NOT added to `PDF_FETCH_FIELDS`: the column would be
constant there. This repo has already learned that a column carrying no
information invites a bug — the crosswalk's always-empty
`zotero_item_key` is the precedent.
"""

from __future__ import annotations

from log_schemas import ABSTRACT_FETCH_FIELDS, PDF_FETCH_FIELDS

# --- the schema -------------------------------------------------------

def test_the_abstract_log_carries_the_surface():
    assert "surface" in ABSTRACT_FETCH_FIELDS


def test_surface_is_appended_last_so_existing_logs_migrate():
    """`_migrate_header` only handles a pure, ordered column addition, so
    `surface` stays eighth and later columns (`ran_at`) come after it."""
    assert ABSTRACT_FETCH_FIELDS[7] == "surface"
    assert ABSTRACT_FETCH_FIELDS[-1] == "ran_at"


def test_an_eight_column_log_gains_ran_at(tmp_path):
    import csv

    import shared_orchestrators as so

    path = tmp_path / "abstract_fetch_log.csv"
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(ABSTRACT_FETCH_FIELDS[:8])
        w.writerow(["2026-09-22", "A", "10.1/x", "T", "crossref", "updated", "", "local"])
    fh, writer = so.open_log(str(path), ABSTRACT_FETCH_FIELDS)
    writer.writerow({"run_date": "2026-09-23", "item_key": "B", "ran_at": "2026-09-23T01:00:00+03:00"})
    fh.close()
    rows = list(csv.DictReader(open(path)))
    assert rows[0]["surface"] == "local" and rows[0]["ran_at"] == ""
    assert rows[1]["ran_at"] == "2026-09-23T01:00:00+03:00"


def test_the_pdf_log_does_not_carry_a_constant_column():
    assert "surface" not in PDF_FETCH_FIELDS


# --- an existing log survives the addition ----------------------------

def test_a_seven_column_log_is_migrated_in_place(tmp_path):
    import csv

    import shared_orchestrators as so

    path = tmp_path / "abstract_fetch_log.csv"
    old = ["run_date", "item_key", "doi", "title", "source", "status", "detail"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=old)
        w.writeheader()
        w.writerow({
            "run_date": "2026-09-01", "item_key": "AAAA1111",
            "doi": "10.1/x", "title": "t", "source": "crossref",
            "status": "updated", "detail": "",
        })

    fh, _ = so.open_log(str(path), ABSTRACT_FETCH_FIELDS)
    fh.close()

    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["item_key"] == "AAAA1111"
    assert rows[0]["surface"] == ""      # historical row: unknown, not guessed
