"""Tests for the Connector library-selection pre-flight and the wizard's
stored `extension_dir`.

Both cover the same class of bug: a check that cannot pass, reported as
though the user had done something wrong. The pre-flight compared only
`groupID`, which a personal library never reports, so every `--user` run
warned that saves would land in the wrong library. The wizard stored the
extension's *version* subdirectory, which Chrome deletes on auto-update,
so a working install was later reported as "extension not found".
"""

from __future__ import annotations

from pathlib import Path

import pytest
from enrich_pdfs import library_selection_matches


class _Zot:
    """Minimal stand-in for the Zotero wrapper's library identity."""

    def __init__(self, library_type: str, group_id: str, name: str | None = None):
        self.library_type = library_type
        self.group_id = group_id
        self._name = name

    def group_name(self) -> str | None:
        return self._name


# ---------------------------------------------------------------------------
# Personal-library targets
# ---------------------------------------------------------------------------


def test_my_library_selected_matches_a_user_target() -> None:
    """The regression. Desktop reports no group ID for My Library, and the
    old check read that absence as a mismatch — warning that every save
    would land in the wrong place while it was in fact correctly set up."""
    matched, reason = library_selection_matches(
        _Zot("user", "5591"), {"libraryName": "My Library"},
    )
    assert matched is True
    assert "personal library" in reason


def test_a_group_selected_against_a_user_target_is_a_real_mismatch() -> None:
    """The fix must not silence the genuine case: saves would land in the
    group, not the personal library."""
    matched, _ = library_selection_matches(
        _Zot("user", "5591"),
        {"libraryName": "Shared group", "groupID": 6015547},
    )
    assert matched is False


def test_a_user_target_is_not_matched_by_a_group_of_the_same_number() -> None:
    """A user id and a group id share a namespace only by accident. The old
    code compared `zot.group_id` to the selected group ID with no regard
    for which kind of library the target was, so user 5591 would have
    matched group 5591."""
    matched, _ = library_selection_matches(
        _Zot("user", "5591"), {"libraryName": "Coincidence", "groupID": 5591},
    )
    assert matched is False


# ---------------------------------------------------------------------------
# Group targets — unchanged behaviour
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "selected,expected",
    [
        ({"libraryName": "AI", "groupID": 6015547}, True),
        ({"libraryName": "Other", "groupID": 999}, False),
        ({"libraryName": "My Library"}, False),
        ({"libraryName": "AI", "groupId": 6015547}, True),  # camelCase variant
    ],
)
def test_group_targets_compare_by_group_id(selected, expected) -> None:
    matched, _ = library_selection_matches(
        _Zot("group", "6015547", "AI"), selected,
    )
    assert matched is expected


def test_group_target_falls_back_to_name_when_no_id_reported() -> None:
    """Older Zotero builds omit the group ID; the name comparison is the
    documented fallback and must survive."""
    matched, reason = library_selection_matches(
        _Zot("group", "6015547", "AI in entrepreneurship"),
        {"libraryName": "AI in entrepreneurship"},
    )
    assert matched is True
    assert "name-based" in reason


# ---------------------------------------------------------------------------
# The wizard stores a version-independent extension_dir
# ---------------------------------------------------------------------------


def _wizard():
    import sys
    root = Path(__file__).resolve().parents[2] / "scripts" / "setup"
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import wizard
    return wizard


def test_wizard_records_the_base_folder_not_the_version_subdir(
    monkeypatch, tmp_path: Path,
) -> None:
    """Storing `.../<ext-id>/5.0.200_0` strands the config at the next
    Chrome auto-update, which deletes that folder."""
    w = _wizard()
    base = tmp_path / w._CONNECTOR_EXT_ID
    (base / "5.0.211_0").mkdir(parents=True)
    (base / "5.0.211_0" / "manifest.json").write_text("{}")
    monkeypatch.setattr(w, "_connector_probe_paths", lambda: [base])

    out = w._detect_and_prompt_connector(False, {})
    assert out["extension_dir"] == str(base)


def test_wizard_normalises_a_previously_pinned_version_dir(
    monkeypatch, tmp_path: Path,
) -> None:
    """Re-running setup is the documented cure for a stranded pin, so it
    has to actually cure it rather than write the fragile value back."""
    w = _wizard()
    base = tmp_path / w._CONNECTOR_EXT_ID
    version = base / "5.0.211_0"
    version.mkdir(parents=True)
    (version / "manifest.json").write_text("{}")
    monkeypatch.setattr(w, "_connector_probe_paths", lambda: [base])

    out = w._detect_and_prompt_connector(
        False, {"zotero_connector": {"extension_dir": str(version)}},
    )
    assert out["extension_dir"] == str(base)


def test_wizard_leaves_an_unrelated_explicit_path_alone(
    monkeypatch, tmp_path: Path,
) -> None:
    """Only a path sitting directly under the extension id is a version
    pin. A hand-set directory elsewhere is the user's choice."""
    w = _wizard()
    custom = tmp_path / "my-unpacked-connector"
    custom.mkdir()
    (custom / "manifest.json").write_text("{}")
    monkeypatch.setattr(w, "_connector_probe_paths", lambda: [])

    out = w._detect_and_prompt_connector(
        False, {"zotero_connector": {"extension_dir": str(custom)}},
    )
    assert out["extension_dir"] == str(custom)
