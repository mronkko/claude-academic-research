"""`prisma_flow.py`: PRISMA 2020 counts, arithmetic checks, rendering."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import prisma_flow as pf
import pytest

META = {
    "per_database_counts": {"pubmed": 120, "scopus": 100},
    "per_database_citation_counts": {"pubmed": 0, "scopus": 0},
    "total_unique_records": 160,
    "unique_records_by_discovery_source": {"keyword_search": 160},
    "unique_records_by_doctype": {"supplement": 2, "notice": 1},
}


def _abs(n_inc: int, n_border: int, n_exc: int) -> list[dict]:
    rows = []
    for i in range(n_inc):
        rows.append({"item_key": f"I{i}", "decision": "include"})
    for i in range(n_border):
        rows.append({"item_key": f"B{i}", "decision": "borderline"})
    for i in range(n_exc):
        rows.append({"item_key": f"E{i}", "decision": "exclude"})
    return rows


def _ft(keys_inc: list[str], excluded: dict[str, str]) -> list[dict]:
    rows = [{"item_key": k, "decision": "include", "exclusion_code": ""}
            for k in keys_inc]
    rows += [{"item_key": k, "decision": "exclude", "exclusion_code": c}
             for k, c in excluded.items()]
    return rows


def _consistent() -> dict:
    # 220 rows - 160 unique = 60 duplicates; 3 not research; 157 screened.
    abstract = _abs(10, 5, 142)
    fulltext = _ft(["I0", "I1", "I2", "I3", "I4", "I5", "I6"],
                   {"I7": "FE3-Outcome", "I8": "FE3-Outcome",
                    "I9": "FE1-Population", "B0": "FE2-Index test",
                    "B1": "FE3-Outcome", "B2": "FE4-Design", "B3": "FE3-Outcome",
                    "B4": "FE1-Population"})
    return pf.build_flow(META, abstract, fulltext)


def test_counts_flow_through() -> None:
    flow = _consistent()
    ident = flow["identification"]
    assert ident["databases"]["total"] == 220
    assert ident["removed_before_screening"]["duplicates"]["total"] == 60
    assert ident["removed_before_screening"]["not_research_articles"]["total"] == 3
    assert flow["screening"]["records_screened"] == 157
    assert flow["screening"]["records_excluded"] == 142
    assert flow["screening"]["reports_sought"] == 15
    assert flow["screening"]["reports_assessed"] == 15
    assert flow["screening"]["reports_excluded"]["total"] == 8
    assert flow["screening"]["reports_excluded"]["by_reason"]["FE3-Outcome"] == 4
    assert flow["included"] == {"studies": 7, "reports": 7}
    assert flow["ok"] is True, flow["checks"]


def test_latest_row_wins() -> None:
    rows = [{"item_key": "A", "decision": "include"},
            {"item_key": "A", "decision": "exclude"}]
    assert pf.last_row_per_key(rows)["A"]["decision"] == "exclude"


def test_citation_stream_is_other_methods_not_databases() -> None:
    meta = dict(META)
    meta["per_database_counts"] = {"pubmed": 120, "scopus": 130}
    meta["per_database_citation_counts"] = {"pubmed": 0, "scopus": 30}
    meta["unique_records_by_discovery_source"] = {
        "keyword_search": 150, "citation_search": 10}
    flow = pf.build_flow(meta, [], [])
    ident = flow["identification"]
    assert ident["databases"]["total"] == 220
    assert ident["other_methods"]["by_source"]["citation searching"] == 30
    assert ident["identified_total"] == 250


def test_extra_counts_are_added_and_marked() -> None:
    extra = {"databases_extra": {"embase": 40}, "registers": {"ctgov": 3},
             "duplicates_removed_extra": 25, "reports_not_retrieved": 1,
             "studies_included": 6}
    flow = pf.build_flow(META, _abs(10, 5, 142), [], extra)
    ident = flow["identification"]
    assert ident["databases"]["by_source"]["embase (imported)"] == 40
    assert ident["registers"]["total"] == 3
    assert ident["removed_before_screening"]["duplicates"]["outside_pipeline"] == 25
    assert flow["screening"]["reports_not_retrieved"] == 1
    assert flow["included"]["studies"] == 6
    assert "duplicates_removed_extra" in flow["provenance"]["reported_by_reviewers"]


def test_mismatch_is_reported_not_corrected() -> None:
    flow = pf.build_flow(META, _abs(10, 5, 100), [])  # 115 screened, not 157
    assert flow["screening"]["records_screened"] == 115
    assert flow["ok"] is False
    failed = [c["name"] for c in flow["checks"] if not c["ok"]]
    assert "screened = identified - removed before screening" in failed


def test_unfinished_fulltext_counts_pending_as_not_retrieved_with_a_note() -> None:
    flow = pf.build_flow(META, _abs(10, 5, 142), [])
    assert flow["screening"]["reports_not_retrieved"] == 15
    assert any("not in the full-text log" in n for n in flow["notes"])


def test_fulltext_item_not_in_abstract_set_fails_a_check() -> None:
    flow = pf.build_flow(META, _abs(1, 0, 0), _ft(["ZZZ"], {}))
    assert any(not c["ok"] and "passed abstract screening" in c["name"]
               for c in flow["checks"])


def test_exclusion_without_a_code_is_noted() -> None:
    flow = pf.build_flow(META, _abs(1, 0, 0),
                         [{"item_key": "I0", "decision": "exclude"}])
    assert flow["screening"]["reports_excluded"]["by_reason"] == {"(no code)": 1}
    assert any("exclusion code" in n for n in flow["notes"])


@pytest.mark.parametrize("bad", [{"databases_extra": {"x": -1}},
                                 {"studies_included": "3"},
                                 {"nonsense": 1}])
def test_extra_validation(bad: dict) -> None:
    with pytest.raises(ValueError):
        pf.parse_extra(bad)


def test_rendering_carries_the_numbers() -> None:
    flow = _consistent()
    mermaid = pf.to_mermaid(flow)
    assert mermaid.startswith("flowchart TD")
    assert "Records screened<br/>(n = 157)" in mermaid
    assert "FE3-Outcome: n = 4" in mermaid
    md = pf.to_markdown(flow)
    assert "| Records screened | 157 |" in md and "PASS" in md


def _write_log(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = sorted({k for r in rows for k in r}) or ["item_key"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def test_cli_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                        capsys: pytest.CaptureFixture) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "search_metadata.json").write_text(json.dumps(META))
    _write_log(tmp_path / "screening/abstract_screening.csv", _abs(10, 5, 142))
    _write_log(tmp_path / "screening/fulltext_screening.csv",
               _ft(["I0"], {"I1": "FE3-Outcome"}))
    # Pending full texts are counted as not retrieved, so the arithmetic holds.
    assert pf.main(["--strict"]) == 0
    out = tmp_path / "analysis/results"
    assert json.loads((out / "prisma_flow.json").read_text())["screening"][
        "records_screened"] == 157
    assert (out / "prisma_flow.mmd").is_file() and (out / "prisma_flow.md").is_file()
    assert "Screened:          157" in capsys.readouterr().out


def test_cli_strict_fails_on_a_real_mismatch(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "search_metadata.json").write_text(json.dumps(META))
    _write_log(tmp_path / "screening/abstract_screening.csv", _abs(1, 0, 5))
    assert pf.main([]) == 0
    assert pf.main(["--strict"]) == 1


def test_cli_bad_extra_file_exits_2(tmp_path: Path,
                                    monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "e.json").write_text('{"nonsense": 1}')
    assert pf.main(["--extra", "e.json"]) == 2
