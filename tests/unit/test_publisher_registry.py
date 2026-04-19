"""Schema checks on the Publisher registry."""

from __future__ import annotations

from publishers.registry import DEFAULT_PUBLISHERS

REQUIRED_KEYS = {"match", "url", "name", "concurrency", "delay_s"}


def test_every_publisher_has_required_keys() -> None:
    for slug, entry in DEFAULT_PUBLISHERS.items():
        missing = REQUIRED_KEYS - entry.keys()
        assert not missing, f"publisher '{slug}' missing keys: {sorted(missing)}"


def test_publisher_url_templates_include_doi_placeholder() -> None:
    for slug, entry in DEFAULT_PUBLISHERS.items():
        assert "{doi}" in entry["url"], (
            f"publisher '{slug}' URL template must contain '{{doi}}' placeholder"
        )


def test_publisher_match_prefixes_non_empty() -> None:
    for slug, entry in DEFAULT_PUBLISHERS.items():
        assert entry["match"], f"publisher '{slug}' has empty match list"
        for prefix in entry["match"]:
            assert prefix.startswith("10."), f"publisher '{slug}' non-DOI prefix: {prefix!r}"


def test_no_duplicate_doi_prefixes_across_publishers() -> None:
    seen: dict[str, str] = {}
    for slug, entry in DEFAULT_PUBLISHERS.items():
        for prefix in entry["match"]:
            assert prefix not in seen, (
                f"DOI prefix {prefix} claimed by both '{seen[prefix]}' and '{slug}'"
            )
            seen[prefix] = slug


def test_concurrency_is_positive_int() -> None:
    for slug, entry in DEFAULT_PUBLISHERS.items():
        c = entry["concurrency"]
        assert isinstance(c, int) and c >= 1, f"publisher '{slug}' concurrency invalid: {c!r}"


def test_aom_publisher_is_registered() -> None:
    """AoM is listed in the plan as the canonical login-required publisher."""
    assert "aom" in DEFAULT_PUBLISHERS
    assert "10.5465/" in DEFAULT_PUBLISHERS["aom"]["match"]
