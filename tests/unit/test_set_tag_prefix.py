"""Tests for the `TAG_PREFIX` writer in `scripts/setup/`.

The script duplicates `scripts/pipelines/tag_prefix.py`'s validation rules on
purpose — `scripts/setup/` is stdlib-only and does not import from
`scripts/pipelines/`. The duplication is only safe while the two agree, so the
sync tests at the bottom are the load-bearing ones here.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import tag_prefix as pipeline_tp

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "setup" / "set_tag_prefix.py"
TEMPLATE = REPO_ROOT / "templates" / "screening_config.py"


def _load():
    spec = importlib.util.spec_from_file_location("set_tag_prefix", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


stp = _load()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["agentic-ai", "slr2026", "a", "x1-y2-z3"])
def test_accepts_well_formed(raw):
    assert stp.validate(raw) == raw


@pytest.mark.parametrize(
    "raw",
    ["My Review", "UPPER", "a/b", "x:y", "-lead", "trail-", "", "   ", "a" * 33],
)
def test_rejects_malformed_via_exit(raw):
    with pytest.raises(SystemExit) as exc:
        stp.validate(raw)
    assert "tag prefix" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Rewriting
# ---------------------------------------------------------------------------


def test_rewrite_replaces_the_assignment():
    text, n = stp.rewrite('TAG_PREFIX = ""\nOTHER = 1\n', "agentic-ai")
    assert n == 1
    assert 'TAG_PREFIX = "agentic-ai"' in text
    assert "OTHER = 1" in text


def test_rewrite_reports_zero_when_the_constant_is_absent():
    _, n = stp.rewrite("OTHER = 1\n", "agentic-ai")
    assert n == 0


def test_rewrite_replaces_only_the_first_assignment():
    text, n = stp.rewrite('TAG_PREFIX = "a"\nTAG_PREFIX = "b"\n', "c")
    assert n == 1
    assert text.splitlines()[0] == 'TAG_PREFIX = "c"'


def test_rewrite_does_not_touch_a_commented_mention():
    src = '# TAG_PREFIX = "example"\nTAG_PREFIX = ""\n'
    text, n = stp.rewrite(src, "real")
    assert n == 1
    assert '# TAG_PREFIX = "example"' in text
    assert 'TAG_PREFIX = "real"' in text


def test_rewrite_preserves_crlf_line_endings(tmp_path):
    """A `screening_config.py` is in the user's git; don't reflow the file."""
    cfg = tmp_path / "screening_config.py"
    with open(cfg, "w", encoding="utf-8", newline="") as fh:
        fh.write('TAG_PREFIX = "x"\r\nOTHER = 1\r\n')
    text = stp._read(cfg)
    updated, _ = stp.rewrite(text, "agentic-ai")
    stp._write(cfg, updated)
    with open(cfg, encoding="utf-8", newline="") as fh:
        assert fh.read() == 'TAG_PREFIX = "agentic-ai"\r\nOTHER = 1\r\n'


def test_a_prefix_with_a_backslash_cannot_be_read_as_a_group_reference():
    """`rewrite` substitutes through a lambda; validation bars this anyway."""
    text, n = stp.rewrite('TAG_PREFIX = ""\n', r"a\1b")
    assert n == 1
    assert r'TAG_PREFIX = "a\1b"' in text


# ---------------------------------------------------------------------------
# Reading back
# ---------------------------------------------------------------------------


def test_current_reads_the_declared_value():
    assert stp.current('TAG_PREFIX = "agentic-ai"\n') == "agentic-ai"


def test_current_is_empty_for_unset_and_absent():
    assert stp.current('TAG_PREFIX = ""\n') == ""
    assert stp.current("OTHER = 1\n") == ""


def test_current_handles_single_quotes():
    assert stp.current("TAG_PREFIX = 'agentic-ai'\n") == "agentic-ai"


# ---------------------------------------------------------------------------
# Preview — what the skill shows the user before they confirm
# ---------------------------------------------------------------------------


def test_preview_shows_real_prefixed_tags():
    out = stp.preview("agentic-ai")
    assert "agentic-ai/abstract:include" in out
    assert "agentic-ai/fulltext:exclude" in out
    assert "agentic-ai/qa-adjudicated-include" in out


def test_preview_names_the_globals_as_globals():
    out = stp.preview("agentic-ai")
    assert "predatory:flag" in out
    assert "agentic-ai/predatory:flag" not in out


def test_preview_explains_how_to_filter():
    assert "tag selector" in stp.preview("agentic-ai")


# ---------------------------------------------------------------------------
# Sync guards — the duplication is only safe while these hold
# ---------------------------------------------------------------------------


def test_validation_rules_match_the_pipeline_helper():
    assert stp.PREFIX_RE.pattern == pipeline_tp.PREFIX_RE.pattern
    assert stp.MAX_PREFIX_LEN == pipeline_tp.MAX_PREFIX_LEN
    assert stp.CONSTANT == pipeline_tp.CONFIG_ATTR
    assert stp.RULES == pipeline_tp.RULES


@pytest.mark.parametrize(
    "raw",
    ["agentic-ai", "a", "x1-y2", "My Review", "a/b", "x:y", "-lead", "trail-",
     "", "UPPER", "a" * 33, "double--hyphen"],
)
def test_both_copies_agree_on_every_verdict(raw):
    setup_ok = True
    try:
        stp.validate(raw)
    except SystemExit:
        setup_ok = False
    pipeline_ok = True
    try:
        pipeline_tp.validate(raw)
    except ValueError:
        pipeline_ok = False
    assert setup_ok == pipeline_ok, f"copies disagree on {raw!r}"


def test_the_shipped_template_declares_the_constant():
    """`rewrite` returns 0 on a file without it, so the template must have it."""
    _, n = stp.rewrite(TEMPLATE.read_text(encoding="utf-8"), "agentic-ai")
    assert n == 1


def test_the_shipped_template_ships_it_unset():
    """An accidentally-shipped default would namespace every review alike."""
    assert stp.current(TEMPLATE.read_text(encoding="utf-8")) == ""
