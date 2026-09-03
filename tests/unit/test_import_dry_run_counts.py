"""A dry run must not report a number it did not measure.

`_fetch_existing_items` returned `({}, {})` when `dry_run=True`, skipping
the cloud lookup on the grounds that a dry run patches nothing. But the
summary printed underneath it did not know that, so every dry run
reported "Already in library (patch only): 0" and routed every row into
"New items to create" — for a library with 480 overlapping items, on a
602-record import. The user who hit this nearly ran the real import on
that basis and caught it only by reading the source.

Previewing patch-versus-create is the main thing a dry run is *for*: it
is the decision the operator makes from it. So the lookup now happens in
dry-run too, and the counts are real.

The lookup can still be unavailable — no credentials, no network, an
offline CSV check. That case prints "not checked" instead of a number.
The rule is the same either way: measure it, or say you did not.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import import_to_zotero


def _zot(items: list[dict]) -> MagicMock:
    zot = MagicMock()
    zot.cloud_journal_articles.return_value = items
    return zot


def _item(doi: str, title: str = "A paper") -> dict:
    # Real-shaped DOIs: the map is keyed through the strict normaliser,
    # which correctly refuses a malformed one.
    return {"key": f"K{doi[-1]}", "data": {"DOI": doi, "title": title}}


def test_dry_run_reads_the_library_so_its_counts_are_real() -> None:
    zot = _zot([_item("10.1016/j.example.2020.01.001"), _item("10.1016/j.example.2020.01.002")])
    doi_map, _title_map = import_to_zotero._fetch_existing_items(
        zot, dry_run=True,
    )
    assert set(doi_map) == {"10.1016/j.example.2020.01.001", "10.1016/j.example.2020.01.002"}
    zot.cloud_journal_articles.assert_called_once()


def test_a_real_run_is_unchanged() -> None:
    zot = _zot([_item("10.1016/j.example.2020.01.001")])
    doi_map, _ = import_to_zotero._fetch_existing_items(zot, dry_run=False)
    assert set(doi_map) == {"10.1016/j.example.2020.01.001"}


def test_a_failed_lookup_in_dry_run_degrades_instead_of_crashing(capsys) -> None:
    """An offline CSV check still has to work — it just may not know."""
    zot = MagicMock()
    zot.cloud_journal_articles.side_effect = RuntimeError("403 Forbidden")
    doi_map, title_map = import_to_zotero._fetch_existing_items(
        zot, dry_run=True,
    )
    assert doi_map == {} and title_map == {}
    assert import_to_zotero._existing_lookup_failed is True
    assert "could not read" in capsys.readouterr().out.lower()


def test_a_failed_lookup_in_a_real_run_still_raises() -> None:
    """A real run that cannot see the library would create duplicates of
    everything already in it — the exact incident `_fetch_existing_items`
    was written to prevent. Failing loudly is the only safe answer."""
    zot = MagicMock()
    zot.cloud_journal_articles.side_effect = RuntimeError("403 Forbidden")
    try:
        import_to_zotero._fetch_existing_items(zot, dry_run=False)
    except RuntimeError:
        return
    raise AssertionError("a failed lookup in a real run must not be swallowed")


def test_the_summary_says_not_checked_when_the_lookup_failed(capsys) -> None:
    import_to_zotero._existing_lookup_failed = True
    try:
        import_to_zotero._print_existing_summary(n_to_add=0, n_to_create=602)
        out = capsys.readouterr().out
        assert "not checked" in out.lower()
        assert "0" not in out.split("create")[0].split(":")[-1]
    finally:
        import_to_zotero._existing_lookup_failed = False


def test_the_summary_prints_the_count_when_the_lookup_succeeded(capsys) -> None:
    import_to_zotero._existing_lookup_failed = False
    import_to_zotero._print_existing_summary(n_to_add=480, n_to_create=122)
    out = capsys.readouterr().out
    assert "480" in out
    assert "not checked" not in out.lower()
