"""`rob_score.py`: QUIN percentages and QUADAS-2 roll-ups are deterministic."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
import rob_score as rs


def _quin_row(**scores: str) -> dict:
    row = {"study_key": "K1", **{c: "2" for c in rs.QUIN_CRITERIA}}
    row.update(scores)
    return row


def test_quin_all_adequate_is_100_percent_low() -> None:
    out = rs.score_quin_row(_quin_row(), 2)
    assert out["percent"] == 100.0
    assert out["risk_of_bias"] == "low"
    assert out["applicable_criteria"] == 12 and out["max_score"] == 24


def test_quin_not_applicable_is_excluded_from_the_denominator() -> None:
    out = rs.score_quin_row(_quin_row(c07="NA", c10="na"), 2)
    assert out["applicable_criteria"] == 10
    assert out["max_score"] == 20 and out["percent"] == 100.0


@pytest.mark.parametrize(
    ("percent", "category"),
    [(100, "low"), (70.1, "low"), (70, "medium"), (50, "medium"),
     (49.9, "high"), (0, "high")],
)
def test_quin_thresholds(percent: float, category: str) -> None:
    assert rs.quin_category(percent) == category


def test_quin_exactly_seventy_percent_is_medium() -> None:
    # 12 criteria, max 24: 16.8 points is 70%, so use 10 applicable (max 20)
    # and 14 points = 70.0%.
    scores = {f"c{i:02d}": "2" for i in range(1, 8)}      # 7 x 2 = 14
    scores.update({f"c{i:02d}": "0" for i in range(8, 11)})  # 3 x 0
    scores.update({"c11": "NA", "c12": "NA"})
    out = rs.score_quin_row({"study_key": "K", **scores}, 2)
    assert out["percent"] == 70.0 and out["risk_of_bias"] == "medium"


def test_quin_rejects_bad_values_and_all_na() -> None:
    with pytest.raises(rs.RatingError, match="c03"):
        rs.score_quin_row(_quin_row(c03="3"), 5)
    with pytest.raises(rs.RatingError, match="every criterion is NA"):
        rs.score_quin_row({"study_key": "K", **{c: "NA" for c in rs.QUIN_CRITERIA}}, 2)
    with pytest.raises(rs.RatingError, match="study_key"):
        rs.score_quin_row(_quin_row(study_key=" "), 2)


def _quadas_row(**cells: str) -> dict:
    row = {"study_key": "K1",
           **{c: "low" for c in rs.QUADAS_ROB + rs.QUADAS_APP}}
    row.update(cells)
    return row


def test_quadas_overall_rules() -> None:
    assert rs.score_quadas_row(_quadas_row(), 2)["overall_risk_of_bias"] == "low"
    assert rs.score_quadas_row(
        _quadas_row(d2_rob="unclear"), 2)["overall_risk_of_bias"] == "unclear"
    assert rs.score_quadas_row(
        _quadas_row(d2_rob="unclear", d3_rob="high"), 2)["overall_risk_of_bias"] == "high"
    assert rs.score_quadas_row(
        _quadas_row(d3_app="high"), 2)["overall_applicability"] == "high"


def test_quadas_rejects_unknown_judgement() -> None:
    with pytest.raises(rs.RatingError, match="d1_rob"):
        rs.score_quadas_row(_quadas_row(d1_rob="moderate"), 4)


def test_cli_round_trip(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    src = tmp_path / "quin.csv"
    cols = ["study_key", *rs.QUIN_CRITERIA]
    with src.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerow({"study_key": "A", **{c: "2" for c in rs.QUIN_CRITERIA}})
        w.writerow({"study_key": "B", **{c: "0" for c in rs.QUIN_CRITERIA}})
    dest = tmp_path / "out" / "scored.csv"
    assert rs.main(["--instrument", "quin", "--input", str(src), "--out", str(dest)]) == 0
    rows = list(csv.DictReader(dest.open()))
    assert [r["risk_of_bias"] for r in rows] == ["low", "high"]
    assert "low: 1, medium: 0, high: 1" in capsys.readouterr().out


def test_cli_reports_a_missing_column(tmp_path: Path) -> None:
    src = tmp_path / "bad.csv"
    src.write_text("study_key,c01\nA,2\n")
    assert rs.main(["--instrument", "quin", "--input", str(src), "--out", str(tmp_path / "o.csv")]) == 1
