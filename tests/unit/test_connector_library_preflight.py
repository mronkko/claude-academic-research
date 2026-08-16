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


# ---------------------------------------------------------------------------
# Elsevier XML->PDF synthesis is opt-in
# ---------------------------------------------------------------------------


def test_xml_pdf_synthesis_is_off_by_default() -> None:
    """A generated text-only PDF appearing in someone's Zotero library is
    surprising unless they asked for it, so the default must be off."""
    from enrich_pdfs import Config

    assert Config().elsevier_render_xml_to_pdf is False


@pytest.mark.parametrize(
    "raw,expected",
    [
        (True, True), (False, False),          # TOML native booleans
        ("true", True), ("1", True), ("yes", True), ("on", True),
        ("false", False), ("0", False), ("no", False), ("off", False),
        ("", False),                           # unset
        ("perhaps", False),                    # unrecognised never enables
        (None, False),
    ],
)
def test_bool_coercion_only_enables_on_an_explicit_token(raw, expected) -> None:
    from enrich_pdfs import _as_bool

    assert _as_bool(raw) is expected


def test_disabled_synthesis_does_not_call_the_xml_endpoint(monkeypatch) -> None:
    """When off, the XML request is skipped entirely — no Elsevier quota
    spent fetching text the run would then refuse to write."""
    from fetchers.sciencedirect import ScienceDirectSource

    src = ScienceDirectSource.__new__(ScienceDirectSource)
    src.config = type("C", (), {"elsevier_render_xml_to_pdf": False})()
    called = []
    src.http = type("H", (), {"get": lambda *a, **k: called.append(1)})()

    out = src._fetch_xml_fallback("10.1016/j.test.2020.01.001", "key", "url", "/tmp")
    assert out is None
    assert called == [], "the XML endpoint must not be called when opted out"


def test_wizard_defaults_the_option_to_off_and_keeps_the_api_key(
    monkeypatch, tmp_path: Path,
) -> None:
    """Non-interactive setup must not enable it, and merging the answer
    into `[elsevier]` must not drop the api_key collected alongside it."""
    w = _wizard()
    entry = w._prompt_elsevier_xml_pdf(False, {})
    assert entry == {}, "absent means absent; do not write a value nobody chose"

    values = {"elsevier": {"api_key": "SECRET"}}
    entry = w._prompt_elsevier_xml_pdf(
        False, {"elsevier": {"render_xml_to_pdf": True}},
    )
    values.setdefault("elsevier", {}).update(entry)
    assert values["elsevier"] == {"api_key": "SECRET", "render_xml_to_pdf": True}


def test_wizard_preserves_an_existing_opt_in_non_interactively() -> None:
    """A re-run with --no-input must not silently revoke consent."""
    w = _wizard()
    assert w._prompt_elsevier_xml_pdf(
        False, {"elsevier": {"render_xml_to_pdf": True}},
    ) == {"render_xml_to_pdf": True}
    assert w._prompt_elsevier_xml_pdf(
        False, {"elsevier": {"render_xml_to_pdf": False}},
    ) == {"render_xml_to_pdf": False}


def test_enabled_synthesis_does_reach_the_xml_endpoint() -> None:
    """The complement: opting in must actually opt in. A gate that never
    opens is the same bug as one that never closes."""
    from fetchers.sciencedirect import ScienceDirectSource

    src = ScienceDirectSource.__new__(ScienceDirectSource)
    src.config = type("C", (), {"elsevier_render_xml_to_pdf": True})()
    called = []

    class _Resp:
        status_code = 500          # bail immediately after the call
        headers: dict = {}

    def _get(*a, **k):
        called.append(1)
        return _Resp()

    src.http = type("H", (), {"get": staticmethod(_get)})()
    src._fetch_xml_fallback("10.1016/j.test.2020.01.001", "key", "url", "/tmp")
    assert called == [1], "opting in must reach the XML endpoint"


# ---------------------------------------------------------------------------
# The after-failure prompt reaches the user through the control file too
# ---------------------------------------------------------------------------


class _Args:
    def __init__(self, on_first_failure=""):
        self.on_first_failure = on_first_failure


class _Handler:
    name = "apa"
    display_name = "APA PsycNET"


def test_first_failure_prompt_uses_the_interaction_channel(monkeypatch) -> None:
    """The regression: it tested `sys.stdin.isatty()` and returned 'skip'.

    Under --control-file the user is present and answering other prompts in
    the conversation, but one failed item silently discarded every remaining
    item for that publisher. reinert_2025_sgr lost its second APA article
    that way -- never attempted, and indistinguishable in the log from an
    article nobody had a route to.
    """
    import enrich_pdfs
    from fetchers.browser import interaction

    asked = []

    class Chan(interaction.InteractionChannel):
        interactive = True
        def wait_for_user(self, prompt): asked.append(prompt)
        def read_line(self, prompt):
            asked.append(prompt)
            return "k"

    monkeypatch.setattr(interaction, "get_channel", lambda: Chan())
    # stdin is not a tty under pytest -- the old code returned "skip" here
    # without ever asking.
    assert enrich_pdfs._prompt_on_first_failure(_Handler(), 1, _Args()) == "keep"
    assert asked, "the user was never asked"
    assert "APA PsycNET" in asked[0]


@pytest.mark.parametrize(
    "reply,expected",
    [("k", "keep"), ("keep", "keep"),
     ("A", "always_skip"), ("always", "always_skip"),
     ("s", "skip"), ("", "skip"), ("nonsense", "skip")],
)
def test_first_failure_answers_map_as_documented(monkeypatch, reply, expected) -> None:
    import enrich_pdfs
    from fetchers.browser import interaction

    class Chan(interaction.InteractionChannel):
        interactive = True
        def wait_for_user(self, prompt): pass
        def read_line(self, prompt): return reply

    monkeypatch.setattr(interaction, "get_channel", lambda: Chan())
    assert enrich_pdfs._prompt_on_first_failure(_Handler(), 2, _Args()) == expected


def test_first_failure_skips_without_asking_when_nobody_is_reachable(monkeypatch) -> None:
    """AutoSkipChannel means no human is available; skipping is right, and
    asking would hang an unattended run."""
    import enrich_pdfs
    from fetchers.browser import interaction

    class Chan(interaction.InteractionChannel):
        interactive = False
        def wait_for_user(self, prompt): raise AssertionError("must not ask")
        def read_line(self, prompt): raise AssertionError("must not ask")

    monkeypatch.setattr(interaction, "get_channel", lambda: Chan())
    assert enrich_pdfs._prompt_on_first_failure(_Handler(), 3, _Args()) == "skip"


def test_explicit_override_still_wins_without_asking(monkeypatch) -> None:
    import enrich_pdfs
    from fetchers.browser import interaction

    class Chan(interaction.InteractionChannel):
        interactive = True
        def wait_for_user(self, prompt): raise AssertionError("must not ask")
        def read_line(self, prompt): raise AssertionError("must not ask")

    monkeypatch.setattr(interaction, "get_channel", lambda: Chan())
    assert enrich_pdfs._prompt_on_first_failure(
        _Handler(), 3, _Args(on_first_failure="keep")) == "keep"
