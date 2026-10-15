"""templates/manuscript_stats.py exposes the removed-before-screening counts.

search.py records supplements / front matter / notices under
`unique_records_by_doctype`; the PRISMA flow diagram needs them as
`search.n_removed_*` stats.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[2] / "templates" / "manuscript_stats.py"


def _load():
    spec = importlib.util.spec_from_file_location("manuscript_stats_t", TEMPLATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_removed_before_screening_counts(tmp_path, monkeypatch):
    mod = _load()
    meta = tmp_path / "search_metadata.json"
    meta.write_text(json.dumps({"unique_records_by_doctype": {
        "supplement": 152, "front-matter": 127, "notice": 2,
    }}), encoding="utf-8")
    monkeypatch.setattr(mod, "SEARCH_METADATA_PATH", meta)
    monkeypatch.setattr(mod, "SEARCH_RUN_PATH", tmp_path / "absent.json")
    stats = mod._search_stats()
    assert stats["search.n_removed_not_research"] == 281
    assert stats["search.n_removed_supplement"] == 152
    assert stats["search.n_removed_front_matter"] == 127
    assert stats["search.n_removed_notice"] == 2


def test_older_metadata_reads_as_zero(tmp_path, monkeypatch):
    mod = _load()
    meta = tmp_path / "search_metadata.json"
    meta.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(mod, "SEARCH_METADATA_PATH", meta)
    monkeypatch.setattr(mod, "SEARCH_RUN_PATH", tmp_path / "absent.json")
    assert mod._search_stats()["search.n_removed_not_research"] == 0
