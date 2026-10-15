"""The agreed PDF retrieval strategy (`pdf_strategy.py`,
`.claude/pdf/strategy.toml`): written, read back, updated step by step,
and resumable by a later session. Every test runs in a tmp project."""

from __future__ import annotations

import json
import tomllib

import pdf_strategy as ps
import pytest
from core import pdf_policy


@pytest.fixture(autouse=True)
def _project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def _plan(tmp_path, **policy):
    path = tmp_path / ".claude" / "pdf" / "plan.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    pol = {"min_version": "accepted", "reconstructed": False, "paid": "prefer",
           "sources": {"min_version": "global", "reconstructed": "default",
                       "paid": "global"}, **policy}
    path.write_text(json.dumps({"library": "group:9", "policy": pol}), encoding="utf-8")
    return path


def _view(capsys) -> dict:
    out = capsys.readouterr().out
    line = [ln for ln in out.splitlines() if ln.startswith("STRATEGY_JSON: ")][-1]
    return json.loads(line[len("STRATEGY_JSON: "):])


def test_init_writes_valid_toml_from_the_plan(tmp_path, capsys) -> None:
    _plan(tmp_path)
    assert ps.main(["init", "--order", "api,paid,manual", "--present", "yes"]) == 0
    data = tomllib.loads((tmp_path / ps.STRATEGY_PATH).read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["library"] == "group:9"            # taken from the plan
    assert data["user_present"] is True and data["unattended_possible"] is False
    assert data["policy"] == {"min_version": "accepted", "reconstructed": False,
                              "paid": "prefer", "where": "global"}
    assert [s["mode"] for s in data["steps"]] == ["api", "paid", "manual"]
    assert {s["status"] for s in data["steps"]} == {"pending"}
    view = _view(capsys)
    assert view["next"]["mode"] == "api"
    # Paying is a step of its own, so the cascade runs without it.
    assert view["next"]["command"].endswith("enrich_pdfs.py --paid off --group 9")


def test_init_refuses_to_overwrite_without_force(tmp_path, capsys) -> None:
    _plan(tmp_path)
    assert ps.main(["init", "--order", "api", "--present", "no"]) == 0
    assert ps.main(["init", "--order", "manual", "--present", "no"]) == 2
    assert "exists" in capsys.readouterr().err
    assert ps.main(["init", "--order", "manual", "--present", "no", "--force"]) == 0
    assert [s["mode"] for s in ps.load()["steps"]] == ["manual"]


@pytest.mark.parametrize(("order", "message"), [
    ("api,api", "appears twice"),
    ("api,ftp", "unknown mode"),
    ("", "empty"),
])
def test_bad_orders_are_refused(order, message, capsys) -> None:
    assert ps.main(["init", "--order", order, "--present", "yes"]) == 2
    assert message in capsys.readouterr().err


def test_steps_resume_in_order(tmp_path, capsys) -> None:
    """A later session reads the file and picks up where this one stopped:
    a running step first, else the first pending one."""
    _plan(tmp_path)
    ps.main(["init", "--order", "api,browser,manual", "--present", "yes"])
    ps.main(["update", "--mode", "api", "--status", "running"])
    capsys.readouterr()
    ps.main(["show"])
    assert _view(capsys)["next"]["mode"] == "api"
    ps.main(["update", "--mode", "api", "--status", "done", "--result", "attached 40"])
    ps.main(["update", "--mode", "browser", "--status", "skipped"])
    capsys.readouterr()
    ps.main(["show"])
    view = _view(capsys)
    assert view["next"]["mode"] == "manual"
    assert view["steps"][0] == {"mode": "api", "status": "done", "result": "attached 40"}
    data = ps.load()
    assert data["steps"][0]["started"] and data["steps"][0]["finished"]
    ps.main(["update", "--mode", "manual", "--status", "done"])
    capsys.readouterr()
    ps.main(["show"])
    view = _view(capsys)
    assert view["complete"] is True and view["next"] is None


def test_steps_that_need_the_user_say_so(tmp_path, capsys) -> None:
    _plan(tmp_path)
    ps.main(["init", "--order", "browser,manual", "--present", "no",
             "--institution", "aalto"])
    view = _view(capsys)
    nxt = view["next"]
    assert nxt["needs_user"] is True
    assert nxt["blocked_until_user_present"] is True
    assert "--control-file .claude/pdf/browser.json" in nxt["command"]
    assert "--library aalto" in nxt["command"]
    assert nxt["background"] is True
    ps.main(["update", "--present", "yes"])
    assert _view(capsys)["next"]["blocked_until_user_present"] is False


def test_api_and_paid_never_need_the_user() -> None:
    assert not ps.MODES["api"].needs_user
    assert not ps.MODES["paid"].needs_user
    assert all(ps.MODES[m].needs_user for m in ("browser", "connector", "manual"))
    assert "pdf_manual_next" in ps.MODES["manual"].tool


def test_strategy_only_policy_travels_as_flags(tmp_path, capsys) -> None:
    """Agreed for this retrieval only: each step passes it itself."""
    _plan(tmp_path)
    ps.main(["init", "--order", "api,manual", "--present", "yes",
             "--min-version", "published", "--paid", "last_resort",
             "--policy-where", "strategy", "--scope-keys-file", "keys.txt"])
    cmd = _view(capsys)["next"]["command"]
    assert "--min-version published" in cmd and "--paid last_resort" in cmd
    assert cmd.endswith("--filter-keys-file keys.txt")


def test_update_errors(tmp_path, capsys) -> None:
    assert ps.main(["update", "--present", "yes"]) == 2       # no strategy yet
    _plan(tmp_path)
    ps.main(["init", "--order", "api", "--present", "yes"])
    capsys.readouterr()
    assert ps.main(["update", "--mode", "manual", "--status", "done"]) == 2
    assert "not in this strategy" in capsys.readouterr().err
    assert ps.main(["update", "--mode", "api"]) == 2
    assert ps.main(["update"]) == 2


def test_hand_edited_garbage_is_refused(tmp_path, capsys) -> None:
    path = tmp_path / ps.STRATEGY_PATH
    path.parent.mkdir(parents=True)
    path.write_text('version = 1\n[[steps]]\nmode = "api"\nstatus = "maybe"\n',
                    encoding="utf-8")
    assert ps.main(["show"]) == 2
    assert "unknown status" in capsys.readouterr().err


def test_strings_round_trip_through_the_writer(tmp_path) -> None:
    _plan(tmp_path)
    s = ps.new_strategy(["api"], present=True, unattended=False,
                        notes='quote " backslash \\ newline\nend')
    ps.save(s)
    assert ps.load()["notes"] == 'quote " backslash \\ newline\nend'


def test_show_without_a_strategy(capsys) -> None:
    assert ps.main(["show"]) == 0
    assert _view(capsys) == {"exists": False}


# --- the project policy tier -------------------------------------------------

TEMPLATE_TAIL = (
    '#   PDF_MIN_VERSION   the lowest article version to attach.\n'
    '# PDF_MIN_VERSION = "accepted"\n'
    '# PDF_RECONSTRUCTED = False\n'
    '# PDF_PAID = "prefer"\n'
)


def test_policy_fills_the_templates_commented_constants(tmp_path, capsys) -> None:
    cfg = tmp_path / "screening_config.py"
    cfg.write_text('TAG_PREFIX = "x"\n' + TEMPLATE_TAIL, encoding="utf-8")
    _plan(tmp_path)
    ps.main(["init", "--order", "api", "--present", "yes"])
    assert ps.main(["policy", "--min-version", "published", "--reconstructed", "no",
                    "--paid", "off"]) == 0
    text = cfg.read_text(encoding="utf-8")
    assert 'PDF_MIN_VERSION = "published"' in text
    assert "PDF_RECONSTRUCTED = False" in text and 'PDF_PAID = "off"' in text
    # The documentation line above them is left alone.
    assert "#   PDF_MIN_VERSION   the lowest article version" in text
    assert pdf_policy.read_project_constants(cfg) == {
        "min_version": "published", "reconstructed": False, "paid": "off"}
    assert ps.load()["policy"]["where"] == "project"


def test_policy_replaces_a_live_constant_and_appends_a_missing_one(tmp_path) -> None:
    cfg = tmp_path / "screening_config.py"
    cfg.write_text('PDF_MIN_VERSION = "preprint"\n', encoding="utf-8")
    assert ps.main(["policy", "--min-version", "accepted", "--paid", "prefer"]) == 0
    assert pdf_policy.read_project_constants(cfg) == {
        "min_version": "accepted", "paid": "prefer"}
    assert cfg.read_text(encoding="utf-8").count("PDF_MIN_VERSION") == 1


def test_policy_without_a_project_config_points_at_setup(capsys) -> None:
    assert ps.main(["policy", "--paid", "prefer"]) == 3
    err = capsys.readouterr().err
    assert "/setup" in err and "--policy-where strategy" in err


def test_policy_refuses_a_bad_value(tmp_path, capsys) -> None:
    (tmp_path / "screening_config.py").write_text("", encoding="utf-8")
    with pytest.raises(SystemExit):
        ps.main(["policy", "--paid", "always"])
