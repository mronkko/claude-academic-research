"""search.py labels supplements / front matter / notices, never drops them.

They stay in the dedup CSV and the DOI hash (the corpus is what the
databases returned), carry a `doctype` column, and are counted in
`search_metadata.json` so PRISMA can report them as removed before
screening.
"""

from __future__ import annotations

import csv
import json
import sys

import search
from searchers import SEARCH_ROW_FIELDS

CONFIG = """
FROM_YEAR = 2022
TO_YEAR = 2022
JOURNALS = {"0021-9010": ("n/a", "Journal of Applied Psychology")}
"""

ROWS = [
    ("10.1037/apl0000550", "Reconciling female agentic advantage", "Doe, J"),
    ("10.1037/apl0000550.supp",
     "Supplemental Material for Reconciling Female Agentic Advantage", ""),
    ("10.1016/s1048-9843(22)00006-6", "Editorial Board", ""),
    ("10.1002/job.2525", "Issue Information", ""),
    ("10.1177/10422587221120291", "Publication Notice", ""),
]


class _Source:
    name = "alpha"

    def credentials_error(self, _ctx):
        return None

    def run(self, _cfg, _ctx):
        return [
            {**{f: "" for f in SEARCH_ROW_FIELDS},
             "doi": doi, "title": title, "authors": authors}
            for doi, title, authors in ROWS
        ]


def test_search_labels_doctypes_and_counts_them(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "search_config.py"
    cfg.write_text(CONFIG, encoding="utf-8")
    out, meta = tmp_path / "raw", tmp_path / "meta"
    monkeypatch.setattr(search, "searchers_by_name",
                        lambda: {"alpha": _Source()})
    monkeypatch.setattr(sys, "argv", [
        "search.py", "--config", str(cfg),
        "--output-dir", str(out), "--metadata-dir", str(meta),
    ])

    assert search.main() == 0

    for name in ("search_results.csv", "search_results_raw.csv"):
        with (out / name).open(newline="", encoding="utf-8") as f:
            rows = {r["doi"]: r["doctype"] for r in csv.DictReader(f)}
        assert rows == {
            "10.1037/apl0000550": "",
            "10.1037/apl0000550.supp": "supplement",
            "10.1016/s1048-9843(22)00006-6": "front-matter",
            "10.1002/job.2525": "front-matter",
            "10.1177/10422587221120291": "notice",
        }, name

    metadata = json.loads((meta / "search_metadata.json").read_text("utf-8"))
    assert metadata["unique_records_by_doctype"] == {
        "supplement": 1, "front-matter": 2, "notice": 1,
    }
    # Kept, not dropped: the corpus count includes them.
    assert metadata["total_unique_records"] == 5
    assert "supplement 1, front-matter 2, notice 1" in capsys.readouterr().out


def test_doctype_is_the_last_search_column():
    # Appended, so hand-inspected CSVs from older runs keep their layout.
    assert SEARCH_ROW_FIELDS[-1] == "doctype"
    assert SEARCH_ROW_FIELDS[-2] == "discovery_source"
