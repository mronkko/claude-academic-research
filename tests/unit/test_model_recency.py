"""What the model listing tells a reader about which model is newer.

An agent asked to pin a screening model saw `claude-sonnet-4-5-20250929`
and `claude-sonnet-5` in the same alphabetically-sorted listing, with a
blank `released` column for both, and pinned the first one — a dated
snapshot of the previous generation.

Two enabling defects, both fixed here, neither of which picks a model
for anyone:

1. `released` was blank for Anthropic because the parser accepted only
   an integer `created`, and Anthropic returns an ISO-8601 `created_at`.
2. Nothing marked a dated snapshot as one.

The proposing rule itself lives in the systematic-review skill, where a
human confirms it. Discovery reports; it does not decide.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from core import model_discovery, providers

SETUP = Path(__file__).resolve().parents[2] / "scripts" / "setup"


def _load(name: str):
    """`scripts/setup/` is not a package — load the module by path."""
    spec = importlib.util.spec_from_file_location(name, SETUP / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


resolve_models = _load("resolve_models")

# ---------------------------------------------------------------------------
# ISO created_at
# ---------------------------------------------------------------------------


def _anthropic_payload(models: list[dict]) -> dict:
    return {"data": models}


def _normalise(models: list[dict]) -> list:
    spec = providers.get("anthropic")
    return model_discovery._normalise(spec, _anthropic_payload(models))


def test_iso_created_at_is_parsed() -> None:
    """Anthropic's shape. This is the fix: the column populates."""
    out = _normalise([
        {"id": "claude-sonnet-5", "created_at": "2026-02-05T00:00:00Z"},
    ])
    assert out[0].created > 0
    assert resolve_models._released(out[0].created) == "2026-02-05"


def test_epoch_integer_still_works() -> None:
    """OpenAI-compatible listings, unchanged."""
    out = _normalise([{"id": "gpt-x", "created": 1_700_000_000}])
    assert out[0].created == 1_700_000_000


def test_an_iso_date_without_a_timezone_is_read_as_utc() -> None:
    out = _normalise([{"id": "m", "created_at": "2025-09-29T00:00:00"}])
    assert resolve_models._released(out[0].created) == "2025-09-29"


def test_a_bare_date_is_parsed() -> None:
    out = _normalise([{"id": "m", "created_at": "2025-09-29"}])
    assert resolve_models._released(out[0].created) == "2025-09-29"


@pytest.mark.parametrize("junk", ["", "  ", "not-a-date", None, {}, []])
def test_unparseable_dates_are_zero_not_a_crash(junk) -> None:
    """A missing date must leave the column blank, not fail discovery
    for every model in the listing."""
    out = _normalise([{"id": "m", "created_at": junk}])
    assert out[0].created == 0
    assert resolve_models._released(out[0].created) == ""


def test_a_newer_model_sorts_later_by_date() -> None:
    out = _normalise([
        {"id": "old", "created_at": "2025-09-29T00:00:00Z"},
        {"id": "new", "created_at": "2026-02-05T00:00:00Z"},
    ])
    by_id = {m.id: m.created for m in out}
    assert by_id["new"] > by_id["old"]


# ---------------------------------------------------------------------------
# Dated-snapshot annotation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model_id", [
    "claude-sonnet-4-5-20250929",
    "claude-3-5-haiku-20241022",
    "gpt-4o-2024-08-06".replace("-2024-08-06", "-20240806"),
])
def test_dated_ids_are_annotated(model_id) -> None:
    assert resolve_models._snapshot_note(model_id) == "dated snapshot"


@pytest.mark.parametrize("model_id", [
    "claude-sonnet-5",
    "claude-opus-4-1",
    "gpt-5",
    "llama3:8b",
    "gemini-2.5-flash",
])
def test_undated_ids_are_not_annotated(model_id) -> None:
    assert resolve_models._snapshot_note(model_id) == ""


def test_a_version_number_is_not_mistaken_for_a_date() -> None:
    """Eight digits at the end, not any digits."""
    assert resolve_models._snapshot_note("model-4-5") == ""
    assert resolve_models._snapshot_note("model-2025") == ""


# ---------------------------------------------------------------------------
# The rendered listing
# ---------------------------------------------------------------------------


class _M:
    def __init__(self, ident, created=0):
        self.id = ident
        self.created = created


def _listing(models):
    return resolve_models.listing_lines(providers.get("anthropic"), models)


def test_listing_shows_the_release_date_and_the_snapshot_note() -> None:
    lines = _listing([
        _M("claude-sonnet-4-5-20250929", 1_759_104_000),
        _M("claude-sonnet-5", 1_770_249_600),
    ])
    snapshot = next(line for line in lines if "20250929" in line)
    rolling = next(
        line for line in lines
        if "claude-sonnet-5" in line and "20250929" not in line
    )
    assert "dated snapshot" in snapshot
    assert "dated snapshot" not in rolling
    assert "2025-09-29" in snapshot


def test_listing_keeps_its_alphabetical_order() -> None:
    """Still sorted, still not ranked — the annotation informs a choice
    rather than making one."""
    lines = _listing([_M("b-model"), _M("a-model"), _M("c-model")])
    ids = [line.split()[-1] for line in lines[1:]]
    assert ids == sorted(ids)


def test_listing_has_a_notes_header() -> None:
    header = _listing([_M("m")])[0]
    assert "notes" in header
    assert "released" in header


def test_listing_survives_a_model_with_no_date() -> None:
    lines = _listing([_M("m", 0), _M("n-20250101", 0)])
    assert any("dated snapshot" in line for line in lines)
