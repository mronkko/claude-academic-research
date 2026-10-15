"""`pdf_consent.py` — the show / set helper behind `pdf_consent_show` and
`pdf_consent_set`. Writes go to the suite's scratch HOME."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pdf_consent
from core import publisher_consent as pc

REPO = Path(__file__).resolve().parents[2]


def _last_json(out: str) -> dict:
    line = [ln for ln in out.splitlines() if ln.startswith("CONSENT_JSON: ")][-1]
    return json.loads(line[len("CONSENT_JSON: "):])


def test_the_answer_choices_are_the_stored_answers() -> None:
    parser = pdf_consent._build_parser()
    sub = next(a for a in parser._actions if a.dest == "command")
    put = sub.choices["set"]
    action = next(a for a in put._actions if a.dest == "automated")
    assert tuple(action.choices) == pc.ANSWERS


def test_show_lists_known_sites_unanswered(capsys) -> None:
    assert pdf_consent.main(["show"]) == 0
    data = _last_json(capsys.readouterr().out)
    sites = {r["site"]: r for r in data["sites"]}
    assert {"sage", "tandf", "ebsco", "jstor"} <= set(sites)
    assert all(r["automated"] == "ask" and not r["stored"] for r in sites.values())
    assert sites["jstor"]["terms_url"] == "https://about.jstor.org/terms/"


def test_set_records_and_show_reflects_it(capsys) -> None:
    assert pdf_consent.main(["set", "--site", "sage", "--automated", "allow"]) == 0
    recorded = _last_json(capsys.readouterr().out)["recorded"]
    assert recorded["automated"] == "allow" and recorded["confirmed"]
    assert pc.lookup("sage").automated == "allow"
    assert pdf_consent.main(["show", "--site", "sage"]) == 0
    [row] = _last_json(capsys.readouterr().out)["sites"]
    assert row["stored"] and row["automated"] == "allow"


def test_show_includes_the_plans_sites_and_what_is_unanswered(
        tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    plan = tmp_path / ".claude" / "pdf" / "plan.json"
    plan.parent.mkdir(parents=True)
    plan.write_text(json.dumps({"consent": {
        "degruyter.com": {"items": 4, "modes": ["connector"]},
        "sage": {"items": 9, "modes": ["browser"]},
    }}), encoding="utf-8")
    pc.record("sage", "deny")
    assert pdf_consent.main(["show"]) == 0
    out = capsys.readouterr().out
    data = _last_json(out)
    rows = {r["site"]: r for r in data["sites"]}
    assert rows["degruyter.com"]["plan_items"] == 4
    assert rows["degruyter.com"]["homepage"] == "https://degruyter.com/"
    assert data["plan_sites_to_ask"] == ["degruyter.com"]
    assert "Not answered yet for this plan: degruyter.com." in out


def test_a_bad_site_key_is_refused(capsys) -> None:
    assert pdf_consent.main(["set", "--site", "Sage Journals", "--automated",
                             "allow"]) == 2
    assert pc.read_all() == {}


def test_the_helper_and_the_site_registry_import_without_third_party_packages() -> None:
    """`uv run --no-project` gives the MCP tool no dependencies, so the
    script must import with the standard library alone (`-S`: no
    site-packages)."""
    code = ("import sys; sys.path[:0] = ['scripts', 'scripts/pipelines']; "
            "import pdf_consent, fetchers.browser.consent")
    proc = subprocess.run([sys.executable, "-S", "-c", code], cwd=REPO,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
