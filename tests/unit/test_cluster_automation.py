"""What the agent may do with a cluster, and who decides it.

`check_cluster_config.py` answers one question — *how much of the remote
loop may the assistant drive on its own?* — and the answer is the user's,
never the agent's. The resource at stake is a shared facility account the
plugin does not own, on an allocation somebody is accountable for.

Two properties are worth pinning, and they are different in kind:

- **The precedence chain is arithmetic.** Flag over environment over
  config over `manual`, with an unrecognised value warned about and
  ignored rather than raised on. Falling back to `manual` is the safe
  direction: ignoring a typo'd `auto` costs the user a printed command
  block, while honouring a typo'd `manual` would cost them a job.
- **The effective level is a claim about the harness, not about this
  script.** Nothing here grants or withholds anything. Absent an allow
  rule, the agent's Bash prompt *is* the approval — which means a single
  "don't ask again" click, possibly months ago in a different project,
  silently converts `confirm` into `auto`. That promotion is invisible
  everywhere else, so it is reported here, and the reporting is what
  these tests guard.

The last test in the file is the negative one, and the most important:
the wizard must never allow-list `ssh`, `scp`, `rsync` or `sbatch`. The
whole `confirm` level is the claim that those commands prompt. A helpful
allow rule added to the wizard some future afternoon would remove the
mechanism while leaving every level name and every document describing
them intact — the failure would be silent, permanent, and machine-wide.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from core import config_loader, config_writer

SCRIPTS_ROOT = Path(__file__).resolve().parents[2] / "scripts"


def _load_setup_script(name: str):
    """Import a `scripts/setup/` script by path.

    They are stdlib-only and run under a bare `python3` rather than as a
    package, so there is no import path to reach them by; the suite loads
    the wizard the same way.
    """
    spec = importlib.util.spec_from_file_location(
        name, SCRIPTS_ROOT / "setup" / f"{name}.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ccc():
    return _load_setup_script("check_cluster_config")


@pytest.fixture()
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A config.toml under `tmp_path`, for tests that read or write one.

    `CONFIG_PATH` is bound at import time from `Path.home()`, and
    `config_writer` imports it by name, so both bindings have to move —
    patching `Path.home` alone would leave the reader and the writer
    pointed at the real file.
    """
    path = tmp_path / "config" / "config.toml"
    monkeypatch.setattr(config_loader, "CONFIG_PATH", path)
    monkeypatch.setattr(config_writer, "CONFIG_PATH", path)
    config_loader.load_config.cache_clear()
    yield path
    config_loader.load_config.cache_clear()


def _write_settings(path: Path, permissions: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"permissions": permissions}), encoding="utf-8")


# ---------------------------------------------------------------------------
# The precedence chain
# ---------------------------------------------------------------------------


def test_nothing_configured_means_manual(ccc) -> None:
    """The default is the level that touches no remote host.

    `confirm`'s safety comes from a human answering a prompt, and a
    headless session has nobody to answer one. A level whose safety
    depends on a TTY cannot be the default.
    """
    level, source, warnings = ccc.requested_level(None, None, "")
    assert level == "manual"
    assert "default" in source
    assert warnings == []


def test_config_is_read_when_nothing_overrides_it(ccc) -> None:
    level, source, warnings = ccc.requested_level(None, None, "confirm")
    assert level == "confirm"
    assert source == "config.toml [cluster] automation"
    assert warnings == []


def test_the_environment_beats_the_config_file(ccc) -> None:
    level, source, _ = ccc.requested_level(None, "auto", "manual")
    assert level == "auto"
    assert ccc.ENV_VAR in source


def test_the_flag_beats_everything(ccc) -> None:
    level, source, _ = ccc.requested_level("manual", "auto", "auto")
    assert level == "manual"
    assert "--automation" in source


def test_the_source_line_names_where_the_value_came_from(ccc) -> None:
    """"I set it in config.toml and it is being ignored" is the question
    the source line exists to answer, so the two sources are read
    separately rather than through the loader's env-over-config merge."""
    _, from_env, _ = ccc.requested_level(None, "confirm", "confirm")
    _, from_cfg, _ = ccc.requested_level(None, "", "confirm")
    assert from_env != from_cfg


def test_a_blank_environment_variable_is_not_a_value(ccc) -> None:
    """`export ACADEMIC_RESEARCH_CLUSTER_AUTOMATION=` is how a shell
    unsets a variable in practice; treating it as a level would make the
    config file unreachable for anyone whose profile does that."""
    level, source, warnings = ccc.requested_level(None, "   ", "auto")
    assert level == "auto"
    assert source == "config.toml [cluster] automation"
    assert warnings == []


def test_a_bad_environment_value_warns_and_falls_through(ccc) -> None:
    """Ignoring a typo'd level costs a printed command block. Raising
    would leave the user with a traceback and no status at all, from a
    script whose entire job is to report status."""
    level, source, warnings = ccc.requested_level(None, "AUTO!", "confirm")
    assert level == "confirm"
    assert source == "config.toml [cluster] automation"
    assert len(warnings) == 1
    assert ccc.ENV_VAR in warnings[0] and "AUTO!" in warnings[0]


def test_a_bad_config_value_warns_and_falls_back_to_manual(ccc) -> None:
    level, _, warnings = ccc.requested_level(None, None, "unattended")
    assert level == "manual"
    assert len(warnings) == 1
    assert "unattended" in warnings[0]


def test_both_bad_values_are_reported_not_just_the_first(ccc) -> None:
    """A user who typo'd the same word in two places should learn both
    facts from one run, not discover the second after fixing the first."""
    level, _, warnings = ccc.requested_level(None, "yes", "sure")
    assert level == "manual"
    assert len(warnings) == 2


def test_the_levels_are_ordered_weakest_first(ccc) -> None:
    assert ccc.LEVELS == ("manual", "confirm", "auto")
    assert ccc.DEFAULT_LEVEL == "manual"
    assert set(ccc.LEVEL_HELP) == set(ccc.LEVELS)


# ---------------------------------------------------------------------------
# `_rule_command` — reading a permission rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rule", "expected"),
    [
        ("Bash", "*"),
        ("Bash(*)", "*"),
        ("Bash(:*)", "*"),
        ("Bash(ssh:*)", "ssh"),
        ("Bash(ssh)", "ssh"),
        ("Bash(scp:*)", "scp"),
        ("Bash(sbatch --array:*)", "sbatch"),
        # An absolute path with flags before the argument separator: the
        # form a user gets from clicking "don't ask again" on a call that
        # was written out in full.
        ("Bash(/usr/bin/ssh -o BatchMode=yes:*)", "ssh"),
        ("  Bash(rsync -av:*)  ", "rsync"),
        ("Bash(squeue:*)", "squeue"),
        # Not Bash at all, so not this script's business.
        ("Read(//home/x/y)", None),
        ("WebFetch", None),
        ("mcp__zotero__*", None),
    ],
)
def test_rule_command_reads_the_command_out_of_a_rule(ccc, rule, expected) -> None:
    assert ccc._rule_command(rule) == expected


# ---------------------------------------------------------------------------
# Scanning the settings files
# ---------------------------------------------------------------------------


def test_all_four_settings_files_are_searched(ccc, tmp_path: Path) -> None:
    """`settings.local.json` is where "don't ask again" writes. Omitting
    it would leave the detector blind to the exact click it exists to
    catch."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    paths = ccc.settings_paths(project, home)
    names = {(p.parent.parent.name, p.name) for p in paths}
    assert names == {
        ("home", "settings.json"),
        ("home", "settings.local.json"),
        ("project", "settings.json"),
        ("project", "settings.local.json"),
    }


def test_a_gated_rule_is_found_wherever_it_lives(ccc, tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    _write_settings(
        project / ".claude" / "settings.local.json",
        {"allow": ["Bash(grep:*)", "Bash(ssh:*)"]},
    )
    perms = ccc.scan_permissions(ccc.settings_paths(project, home))
    assert [rule for rule, _ in perms["gated"]] == ["Bash(ssh:*)"]
    assert perms["files_read"] == 1


def test_a_read_only_scheduler_query_is_reported_but_does_not_promote(
    ccc, tmp_path: Path,
) -> None:
    """`squeue` spends no allocation and opens no shell. Worth printing —
    it is how polling discipline stops being a decision — but treating it
    as a promotion would cry wolf on the one rule a careful user is most
    likely to add."""
    home = tmp_path / "home"
    _write_settings(
        home / ".claude" / "settings.json",
        {"allow": ["Bash(squeue:*)", "Bash(sacct:*)"]},
    )
    perms = ccc.scan_permissions(ccc.settings_paths(tmp_path / "p", home))
    assert perms["gated"] == []
    assert len(perms["queries"]) == 2
    assert ccc.effective_level("confirm", perms) == ("confirm", [])


def test_a_blanket_bash_rule_counts_as_gating_everything(ccc, tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_settings(home / ".claude" / "settings.json", {"allow": ["Bash(*)"]})
    perms = ccc.scan_permissions(ccc.settings_paths(tmp_path / "p", home))
    assert [rule for rule, _ in perms["gated"]] == ["Bash(*)"]


def test_an_unreadable_settings_file_is_skipped_not_raised_on(
    ccc, tmp_path: Path,
) -> None:
    """A broken settings file is the harness's problem to report. This is
    a diagnostic, and refusing to say anything about the cluster because
    an unrelated file has a trailing comma helps nobody."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text("{ not json", encoding="utf-8")
    _write_settings(
        home / ".claude" / "settings.local.json", {"allow": ["Bash(ssh:*)"]},
    )
    perms = ccc.scan_permissions(ccc.settings_paths(tmp_path / "p", home))
    assert perms["files_read"] == 1
    assert len(perms["gated"]) == 1


def test_settings_without_a_permissions_block_are_harmless(
    ccc, tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"model": "opus", "permissions": {"deny": ["Read(//x)"]}}),
        encoding="utf-8",
    )
    perms = ccc.scan_permissions(ccc.settings_paths(tmp_path / "p", home))
    assert perms["gated"] == [] and perms["bypass"] == []
    assert perms["files_read"] == 1


def test_nothing_at_all_reads_zero_files(ccc, tmp_path: Path) -> None:
    perms = ccc.scan_permissions(ccc.settings_paths(tmp_path / "p", tmp_path / "h"))
    assert perms["files_read"] == 0
    assert perms["gated"] == [] and perms["queries"] == []


# ---------------------------------------------------------------------------
# Drift, in both directions
# ---------------------------------------------------------------------------


def _perms(gated=(), bypass=(), queries=(), files_read=1) -> dict[str, object]:
    fake = Path("/fake/settings.json")
    return {
        "gated": [(r, fake) for r in gated],
        "queries": [(r, fake) for r in queries],
        "bypass": [(m, fake) for m in bypass],
        "files_read": files_read,
    }


def test_confirm_with_an_allow_rule_is_really_auto(ccc) -> None:
    """The level the user chose has had its entire mechanism removed.

    This is the failure the drift detector exists for: one click on
    "don't ask again", six months ago, in a different project, and
    `confirm` has confirmed nothing ever since.
    """
    effective, notes = ccc.effective_level("confirm", _perms(gated=["Bash(ssh:*)"]))
    assert effective == "auto"
    assert len(notes) == 1
    assert "Bash(ssh:*)" in notes[0] and "auto" in notes[0]


def test_auto_without_an_allow_rule_is_really_confirm(ccc) -> None:
    """The reverse surprise, and the worse one headless: every remote
    call prompts, the run stops at the first one, and it reads as a
    hang rather than as a permission question nobody is answering."""
    effective, notes = ccc.effective_level("auto", _perms())
    assert effective == "confirm"
    assert len(notes) == 1
    assert "prompt" in notes[0]


def test_confirm_with_no_rules_is_what_it_says(ccc) -> None:
    assert ccc.effective_level("confirm", _perms()) == ("confirm", [])


def test_auto_with_a_rule_is_what_it_says(ccc) -> None:
    effective, notes = ccc.effective_level("auto", _perms(gated=["Bash(sbatch:*)"]))
    assert effective == "auto"
    assert notes == []


def test_bypass_permissions_makes_every_level_auto(ccc) -> None:
    """Nothing prompts under it, so an allow-list-only detector would
    report `confirm` for a session in which nothing can possibly
    confirm."""
    effective, notes = ccc.effective_level(
        "confirm", _perms(bypass=["bypassPermissions"]),
    )
    assert effective == "auto"
    assert "bypassPermissions" in notes[0]


def test_bypass_permissions_is_read_out_of_the_settings_file(
    ccc, tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _write_settings(
        home / ".claude" / "settings.json",
        {"defaultMode": "bypassPermissions", "allow": []},
    )
    perms = ccc.scan_permissions(ccc.settings_paths(tmp_path / "p", home))
    assert [mode for mode, _ in perms["bypass"]] == ["bypassPermissions"]
    assert ccc.effective_level("auto", perms)[0] == "auto"


def test_manual_is_immune_to_permission_state(ccc) -> None:
    """`manual` issues no gated command, so no allow rule can change what
    it does. That immunity is exactly what makes it the safe default —
    it is the one level whose behaviour does not depend on per-machine
    state the plugin has no control over."""
    effective, notes = ccc.effective_level(
        "manual", _perms(gated=["Bash(ssh:*)"], bypass=["bypassPermissions"]),
    )
    assert effective == "manual"
    assert len(notes) == 1
    assert "nothing changes" in notes[0]


def test_manual_says_nothing_when_there_is_nothing_to_say(ccc) -> None:
    assert ccc.effective_level("manual", _perms()) == ("manual", [])


# ---------------------------------------------------------------------------
# The status block
# ---------------------------------------------------------------------------


def test_the_status_block_answers_the_three_questions(ccc, tmp_path: Path) -> None:
    """Requested, where from, and what will actually happen. `effective`
    is the machine-readable line: a skill reads that one and obeys it."""
    lines = ccc.status_lines(
        "confirm", "config.toml [cluster] automation", "auto",
        _perms(gated=["Bash(ssh:*)"]), tmp_path,
    )
    text = "\n".join(lines)
    assert "automation: confirm" in text
    assert "source: config.toml [cluster] automation" in text
    assert "effective: auto" in text
    assert "settings_files: 1 read" in text


def test_the_status_block_reports_no_rules_as_none(ccc, tmp_path: Path) -> None:
    lines = ccc.status_lines("manual", "default", "manual", _perms(), tmp_path)
    assert "allow_rules: none" in lines
    assert "query_rules: none" in lines


def test_a_rule_under_home_is_shown_with_a_tilde(ccc, tmp_path: Path) -> None:
    """The absolute path of a settings file inside `$HOME` is noise on a
    status line and, in a shared transcript, a small privacy leak."""
    home = tmp_path / "home"
    perms = {
        "gated": [("Bash(ssh:*)", home / ".claude" / "settings.json")],
        "queries": [],
        "bypass": [],
        "files_read": 1,
    }
    lines = ccc.status_lines("auto", "default", "auto", perms, home)
    allow = next(line for line in lines if line.startswith("allow_rules:"))
    assert "~/.claude/settings.json" in allow
    assert str(home) not in allow


def test_a_rule_outside_home_keeps_its_absolute_path(ccc, tmp_path: Path) -> None:
    other = tmp_path / "elsewhere" / "settings.json"
    perms = {
        "gated": [("Bash(ssh:*)", other)],
        "queries": [],
        "bypass": [],
        "files_read": 1,
    }
    lines = ccc.status_lines("auto", "default", "auto", perms, tmp_path / "home")
    assert str(other) in "\n".join(lines)


# ---------------------------------------------------------------------------
# End to end, and the writer
# ---------------------------------------------------------------------------


def test_the_script_runs_and_exits_zero(
    ccc, tmp_path, monkeypatch, capsys, isolated_config,
) -> None:
    """It reports; it never fails. A non-zero exit here would read as
    "the cluster is misconfigured" when the honest answer is "you have
    not chosen a level, so it is manual"."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.delenv(ccc.ENV_VAR, raising=False)
    assert ccc.main(["--project-dir", str(tmp_path / "project")]) == 0
    out = capsys.readouterr().out
    assert "automation: manual" in out
    assert "effective: manual" in out


def test_the_config_file_is_read_end_to_end(
    ccc, tmp_path, monkeypatch, capsys, isolated_config,
) -> None:
    """`get()` reaches `[cluster] automation`, and the source line says
    so — the one part of the chain the pure-function tests cannot see."""
    isolated_config.parent.mkdir(parents=True, exist_ok=True)
    isolated_config.write_text('[cluster]\nautomation = "auto"\n', encoding="utf-8")
    config_loader.load_config.cache_clear()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.delenv(ccc.ENV_VAR, raising=False)
    assert ccc.main(["--project-dir", str(tmp_path / "project")]) == 0
    out = capsys.readouterr().out
    assert "automation: auto" in out
    assert "source: config.toml [cluster] automation" in out


def test_the_drift_note_reaches_stdout(
    ccc, tmp_path, monkeypatch, capsys, isolated_config,
) -> None:
    """The end-to-end version of the detector: a planted allow rule in a
    project settings file must produce the NOTE, not merely a different
    return value from a pure function."""
    project = tmp_path / "project"
    _write_settings(
        project / ".claude" / "settings.local.json", {"allow": ["Bash(ssh:*)"]},
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setenv(ccc.ENV_VAR, "confirm")
    assert ccc.main(["--project-dir", str(project)]) == 0
    out = capsys.readouterr().out
    assert "effective: auto" in out
    assert "NOTE:" in out and "Bash(ssh:*)" in out


def test_a_bad_environment_value_warns_on_stdout(
    ccc, tmp_path, monkeypatch, capsys, isolated_config,
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setenv(ccc.ENV_VAR, "full-auto")
    assert ccc.main(["--project-dir", str(tmp_path / "p")]) == 0
    out = capsys.readouterr().out
    assert "WARNING:" in out and "full-auto" in out
    assert "automation: manual" in out


def test_list_describes_every_level(ccc, capsys) -> None:
    assert ccc.main(["--list"]) == 0
    out = capsys.readouterr().out
    for level in ccc.LEVELS:
        assert level in out


def test_the_writer_refuses_an_unknown_level(
    tmp_path, monkeypatch, isolated_config,
) -> None:
    """A typo must not silently become a level, and must not write. The
    reader tolerates a bad value because it is a diagnostic; the writer
    is the place where a typo is still cheap to reject."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    setter = _load_setup_script("set_cluster_automation")
    assert setter.main(["unattended"]) == 2
    assert not isolated_config.exists()


def test_the_writer_writes_the_level_and_reports_the_effect(
    tmp_path, monkeypatch, capsys, isolated_config,
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    setter = _load_setup_script("set_cluster_automation")
    assert setter.main(["confirm", "--project-dir", str(tmp_path / "p")]) == 0
    written = isolated_config.read_text(encoding="utf-8")
    assert "[cluster]" in written and 'automation = "confirm"' in written
    out = capsys.readouterr().out
    assert "automation: confirm" in out and "effective: confirm" in out


def test_the_writer_says_so_when_the_machine_will_disagree(
    tmp_path, monkeypatch, capsys, isolated_config,
) -> None:
    """Writing `confirm` onto a machine that allow-lists ssh produces a
    file describing an intention the machine will not carry out. Saying
    "done" and letting the next run be the discovery is the failure."""
    project = tmp_path / "p"
    _write_settings(
        project / ".claude" / "settings.local.json", {"allow": ["Bash(ssh:*)"]},
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    setter = _load_setup_script("set_cluster_automation")
    assert setter.main(["confirm", "--project-dir", str(project)]) == 0
    out = capsys.readouterr().out
    assert "effective: auto" in out
    assert "NEXT:" in out and "behave as 'auto'" in out


def test_the_writer_and_the_checker_share_one_description(ccc) -> None:
    """Two scripts describing the same level differently is how a user
    ends up choosing a level that does something else."""
    setter = _load_setup_script("set_cluster_automation")
    assert setter.LEVELS is ccc.LEVELS
    assert setter.LEVEL_HELP is ccc.LEVEL_HELP


# ---------------------------------------------------------------------------
# The negative guard
# ---------------------------------------------------------------------------


def test_the_wizard_never_allow_lists_a_remote_command(ccc) -> None:
    """`confirm` is not enforced by any code in this plugin. It is the
    claim that `ssh`, `scp`, `rsync` and `sbatch` reach the permission
    system without a matching allow rule, so that the prompt Claude Code
    raises *is* the approval step.

    An allow rule added here — reasonably, to spare somebody a prompt —
    would delete that mechanism on every machine that re-runs the wizard,
    while every level name and every paragraph describing them stayed
    exactly as written. Nothing would look different until a job ran
    that nobody approved.

    The check reuses `check_cluster_config._rule_command`, so a rule
    written in a form the detector cannot read (`Bash(/usr/bin/ssh …)`)
    fails here too rather than slipping past both.
    """
    wizard = _load_setup_script("wizard")
    categories, _deny = wizard._permission_categories()
    offenders = [
        (cat.name, rule)
        for cat in categories
        for rule, _ in cat.rules
        if ccc._rule_command(rule) in ccc.GATED_COMMANDS
    ]
    assert not offenders, (
        f"the wizard would allow-list remote commands: {offenders}. The "
        f"plugin must never do this — absent an allow rule the permission "
        f"prompt IS the approval, and adding one silently promotes every "
        f"user of that machine to `auto`."
    )


def test_the_wizard_never_allow_lists_every_command(ccc) -> None:
    """A blanket `Bash(*)` would allow-list the remote commands without
    naming them, defeating the test above by covering everything."""
    wizard = _load_setup_script("wizard")
    categories, _deny = wizard._permission_categories()
    blanket = [
        rule for cat in categories for rule, _ in cat.rules
        if ccc._rule_command(rule) == "*"
    ]
    assert not blanket, f"the wizard ships a blanket Bash rule: {blanket}"


def test_the_wizard_offers_the_same_levels_as_the_checker(ccc) -> None:
    """The wizard imports them rather than re-listing them; this pins
    that it stays that way, because a wizard offering a level the
    precedence chain does not recognise writes an unreadable config."""
    wizard = _load_setup_script("wizard")
    assert tuple(wizard._CLUSTER_LEVELS) == ccc.LEVELS
    assert wizard._CLUSTER_LEVEL_HELP == ccc.LEVEL_HELP
