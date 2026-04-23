"""Tests for the export_coded_includes.py Zotero-based export.

The script reads Zotero as the authoritative source: items tagged with
`fulltext:include` (or `--tag` override), and coded fields from the
machine-readable JSON block embedded in each item's `SLR Coding` child
note. These tests cover the helpers and the Zotero→CSV assembly;
pyzotero is mocked."""

from __future__ import annotations

import csv
import json
import sys
from unittest.mock import MagicMock

import export_coded_includes as exp
import pytest
import zotero_io

# ---------------------------------------------------------------------------
# Helpers — small pure functions.
# ---------------------------------------------------------------------------


def test_bibtex_key_from_extra_parses_bbt_line() -> None:
    extra = "Citation Key: foobar2020baz\nOther: stuff"
    assert exp._bibtex_key_from_extra(extra) == "foobar2020baz"


def test_bibtex_key_from_extra_is_case_insensitive() -> None:
    assert exp._bibtex_key_from_extra("CITATION KEY: abc2020") == "abc2020"
    assert exp._bibtex_key_from_extra("citation key: xyz2019") == "xyz2019"


def test_bibtex_key_from_extra_returns_empty_when_absent() -> None:
    assert exp._bibtex_key_from_extra("") == ""
    assert exp._bibtex_key_from_extra("DOI: 10.1/x\nPMID: 1") == ""


def test_authors_string_joins_author_lastnames() -> None:
    creators = [
        {"creatorType": "author", "lastName": "Smith", "firstName": "J"},
        {"creatorType": "author", "lastName": "Jones", "firstName": "K"},
        {"creatorType": "editor", "lastName": "Wong"},  # non-author skipped
    ]
    assert exp._authors_string(creators) == "Smith; Jones"


def test_authors_string_handles_single_name_field() -> None:
    """Some Zotero items use `name` instead of `lastName` (institutional
    authors, corporate authors)."""
    creators = [
        {"creatorType": "author", "name": "World Bank"},
        {"creatorType": "author", "lastName": "Smith"},
    ]
    assert exp._authors_string(creators) == "World Bank; Smith"


def test_year_from_date_extracts_4_digit_run() -> None:
    assert exp._year_from_date("2020-04-15") == "2020"
    assert exp._year_from_date("April 2019") == "2019"
    assert exp._year_from_date("") == ""
    assert exp._year_from_date("n.d.") == ""


# ---------------------------------------------------------------------------
# _row_from_item — merges Zotero item data with parsed coding payload.
# ---------------------------------------------------------------------------


def test_row_from_item_merges_bib_and_coding_fields() -> None:
    item = {
        "key": "X1",
        "data": {
            "key": "X1",
            "title": "Motivation and growth",
            "DOI": "10.1/x",
            "date": "2020-07-01",
            "publicationTitle": "J Manage",
            "extra": "Citation Key: motiv2020growth",
            "creators": [
                {"creatorType": "author", "lastName": "Smith", "firstName": "J"},
            ],
        },
    }
    payload = {
        "decision": "include",
        "exclusion_code": "",
        "reason": "meets all criteria",
        "model": "claude-sonnet-4-6",
        "prompt_version": "v1",
        "timestamp": "2026-04-23T10:00:00Z",
        "fields": {
            "key_findings": "X predicts Y.",
            "sample": "245 SMEs.",
        },
    }

    row = exp._row_from_item(item, payload)

    assert row["item_key"] == "X1"
    assert row["bibtex_key"] == "motiv2020growth"
    assert row["doi"] == "10.1/x"
    assert row["title"] == "Motivation and growth"
    assert row["authors"] == "Smith"
    assert row["year"] == "2020"
    assert row["journal"] == "J Manage"
    assert row["decision"] == "include"
    assert row["reason"] == "meets all criteria"
    assert row["model"] == "claude-sonnet-4-6"
    assert row["key_findings"] == "X predicts Y."
    assert row["sample"] == "245 SMEs."


# ---------------------------------------------------------------------------
# End-to-end: main() reads Zotero, writes CSV. pyzotero mocked.
# ---------------------------------------------------------------------------


def _tagged_item(key: str, *, title: str, bbt: str, year: str,
                 tag: str = "fulltext:include") -> dict:
    return {
        "key": key,
        "data": {
            "key": key,
            "title": title,
            "date": year,
            "DOI": f"10.1/{key.lower()}",
            "publicationTitle": "J Test",
            "extra": f"Citation Key: {bbt}",
            "creators": [
                {"creatorType": "author", "lastName": f"Author{key}"},
            ],
            "tags": [{"tag": tag}],
        },
    }


def _slr_coding_note_child(fields: dict[str, str]) -> dict:
    """Fake SLR Coding child note with a parseable SLR_CODING_DATA block."""
    payload = {
        "decision": "include",
        "exclusion_code": "",
        "reason": "",
        "model": "m",
        "prompt_version": "v",
        "timestamp": "t",
        "fields": fields,
    }
    body = (
        '<h1>SLR Coding</h1>\n<p>…</p>\n'
        '<!-- SLR_CODING_DATA: ' + json.dumps(payload) + ' -->'
    )
    return {
        "key": "NOTE-" + next(iter(fields)),
        "data": {"itemType": "note", "note": body},
    }


def test_main_writes_rows_for_tagged_items(monkeypatch, tmp_path) -> None:
    """Happy path: two items tagged fulltext:include, each with a
    parseable SLR Coding note. Output has two rows, columns include
    bibliographic + coding fields."""
    # Stub `require` so no real config access. Patch the binding in the
    # export module (from-import copies the reference at module-load time,
    # so patching core.config_loader.require is too late).
    monkeypatch.setattr(exp, "require", lambda *a, **kw: "fake-key")

    items = [
        _tagged_item("A", title="Alpha", bbt="alpha2020", year="2020-01"),
        _tagged_item("B", title="Beta",  bbt="beta2019",  year="2019-05"),
    ]
    children_by_parent = {
        "A": [_slr_coding_note_child({"key_findings": "X", "sample": "N=100"})],
        "B": [_slr_coding_note_child({"key_findings": "Y"})],
    }

    fake_client = MagicMock(spec=zotero_io.ZoteroClient)
    fake_client.items_with_tag.return_value = items

    fake_cloud = MagicMock()
    fake_cloud.children.side_effect = lambda k: children_by_parent.get(k, [])
    fake_client.cloud = fake_cloud

    monkeypatch.setattr(
        zotero_io.ZoteroClient, "from_args",
        classmethod(lambda cls, *a, **kw: fake_client),
    )

    out = tmp_path / "coded.csv"
    monkeypatch.setattr(sys, "argv", [
        "export_coded_includes.py",
        "--group", "12345",
        "--collection", "COLL1",
        "--out", str(out),
    ])

    rc = exp.main()
    assert rc == 0

    with out.open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    keys = {r["item_key"] for r in rows}
    assert keys == {"A", "B"}
    # Bibliographic columns present.
    row_a = next(r for r in rows if r["item_key"] == "A")
    assert row_a["bibtex_key"] == "alpha2020"
    assert row_a["title"] == "Alpha"
    assert row_a["year"] == "2020"
    # Coding-field columns carry through from the note.
    assert row_a["key_findings"] == "X"
    assert row_a["sample"] == "N=100"


def test_main_warns_on_missing_slr_coding_note(
    monkeypatch, tmp_path, capsys,
) -> None:
    """A tagged item with no SLR Coding child note is reported as a
    warning and excluded from the output. Never silently dropped."""
    monkeypatch.setattr(exp, "require", lambda *a, **kw: "fake-key")

    items = [_tagged_item("X", title="Lonely", bbt="lonely", year="2020")]
    children_by_parent: dict[str, list] = {"X": []}  # no note child

    fake_client = MagicMock(spec=zotero_io.ZoteroClient)
    fake_client.items_with_tag.return_value = items
    fake_cloud = MagicMock()
    fake_cloud.children.side_effect = lambda k: children_by_parent.get(k, [])
    fake_client.cloud = fake_cloud
    monkeypatch.setattr(
        zotero_io.ZoteroClient, "from_args",
        classmethod(lambda cls, *a, **kw: fake_client),
    )

    out = tmp_path / "coded.csv"
    monkeypatch.setattr(sys, "argv", [
        "export_coded_includes.py",
        "--group", "12345", "--collection", "COLL1", "--out", str(out),
    ])

    rc = exp.main()
    assert rc == 0
    captured = capsys.readouterr().out
    assert "have no SLR Coding note" in captured
    # Output still written but contains just the header (no rows).
    with out.open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows == []


def test_dry_run_writes_nothing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(exp, "require", lambda *a, **kw: "fake-key")

    fake_client = MagicMock(spec=zotero_io.ZoteroClient)
    fake_client.items_with_tag.return_value = []
    fake_client.cloud = MagicMock()
    monkeypatch.setattr(
        zotero_io.ZoteroClient, "from_args",
        classmethod(lambda cls, *a, **kw: fake_client),
    )

    out = tmp_path / "coded.csv"
    monkeypatch.setattr(sys, "argv", [
        "export_coded_includes.py",
        "--group", "12345", "--collection", "COLL1", "--out", str(out),
        "--dry-run",
    ])

    rc = exp.main()
    assert rc == 0
    assert not out.exists(), "dry-run must not write output"


def test_missing_group_arg_exits() -> None:
    # When --group missing and ZOTERO_GROUP not in env, main() sys.exit()s.
    import os
    old = os.environ.pop("ZOTERO_GROUP", None)
    try:
        sys.argv = [
            "export_coded_includes.py",
            "--collection", "COLL1", "--out", "/tmp/x.csv",
        ]
        with pytest.raises(SystemExit):
            exp.main()
    finally:
        if old is not None:
            os.environ["ZOTERO_GROUP"] = old


# ---------------------------------------------------------------------------
# parse_slr_coding_note — the JSON-in-HTML-comment extractor.
# ---------------------------------------------------------------------------


def test_parse_slr_coding_note_extracts_json_payload() -> None:
    payload = {"decision": "include", "fields": {"a": "1"}}
    html = (
        '<h1>SLR Coding</h1>\n<p>…</p>\n'
        '<!-- SLR_CODING_DATA: ' + json.dumps(payload) + ' -->'
    )
    parsed = zotero_io.parse_slr_coding_note(html)
    assert parsed == payload


def test_parse_slr_coding_note_returns_none_when_absent() -> None:
    assert zotero_io.parse_slr_coding_note("<h1>SLR Coding</h1>") is None


def test_parse_slr_coding_note_returns_none_when_malformed() -> None:
    html = "<!-- SLR_CODING_DATA: {not-json} -->"
    assert zotero_io.parse_slr_coding_note(html) is None
