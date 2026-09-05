"""Recording which Zotero surface a write actually went to.

Since 0.22.0 a write goes to Zotero Desktop's local API when
`[zotero] local_api_key` is set, and to `api.zotero.org` when it is not.
The two libraries keep unrelated version counters, so a reader
reconciling a run-log afterwards against item versions cannot tell which
surface produced a row — and picking the wrong one makes every version
in it look wrong.

The log already records `source` (which publisher supplied the abstract)
and `status` (what happened). `surface` is the third fact needed to
reconcile: where it landed.

Empty is meaningful here and is not a gap. `no_doi`, `not_found`,
`lookup_failed` and `dry_run` rows performed no write at all, and naming
a surface for them would assert something that never happened.

Deliberately NOT added to `PDF_FETCH_FIELDS`: `attach_pdf` always uses
the cloud (pyzotero's 3-step S3 upload has no local form here), so the
column would be constant. This repo has already learned that a column
carrying no information invites a bug — the crosswalk's always-empty
`zotero_item_key` is the precedent. When `upload_attachments()` gives
uploads a local route, that is when the column earns its place.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from log_schemas import ABSTRACT_FETCH_FIELDS, PDF_FETCH_FIELDS
from zotero_io import ZoteroClient


def _client(**kw) -> ZoteroClient:
    zc = ZoteroClient(api_key="k", group_id="123", **kw)
    zc._local = MagicMock(name="local")
    zc._cloud = MagicMock(name="cloud")
    return zc


# --- what the client reports ------------------------------------------

def test_a_cloud_client_names_the_cloud():
    assert _client().write_surface == "cloud"


def test_a_local_client_names_local():
    assert _client(local_api_key="secret").write_surface == "local"


def test_remote_puts_the_surface_back_on_the_cloud():
    zc = _client(local_api_key="secret", prefer_local=False)
    assert zc.write_surface == "cloud"


def test_uploads_report_the_cloud_even_on_a_local_client():
    """`attach_pdf` has no local route, and the log must not imply one."""
    assert _client(local_api_key="secret").upload_surface == "cloud"


# --- the schema -------------------------------------------------------

def test_the_abstract_log_carries_the_surface():
    assert "surface" in ABSTRACT_FETCH_FIELDS


def test_surface_is_appended_last_so_existing_logs_migrate():
    """`_migrate_header` only handles a pure, ordered column addition."""
    assert ABSTRACT_FETCH_FIELDS[-1] == "surface"


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
