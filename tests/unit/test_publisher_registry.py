"""Schema checks on the browser handler registry.

`fetchers/browser/base.py:__init_subclass__` already enforces that
leaf handlers set `name` and `doi_prefixes`; these tests guard the
shape invariants it does not — placeholder syntax in URL templates,
DOI-prefix form, prefix uniqueness across handlers, and sane
concurrency/rate-limit values.
"""

from __future__ import annotations

from fetchers.browser import all_handlers


def test_every_handler_has_required_attrs() -> None:
    for h in all_handlers():
        assert h.name, f"handler {type(h).__name__} has empty name"
        assert h.display_name, f"handler '{h.name}' has empty display_name"
        assert h.doi_prefixes, f"handler '{h.name}' has empty doi_prefixes"


def test_handler_url_templates_include_doi_placeholder() -> None:
    # url_template may be empty for handlers that build URLs dynamically
    # (e.g. OUP reads the PDF href from the landing page) — only check
    # templates that are set.
    for h in all_handlers():
        for attr in ("url_template", "setup_url_template"):
            template = getattr(h, attr)
            if template:
                assert "{doi}" in template, (
                    f"handler '{h.name}' {attr} must contain '{{doi}}' "
                    f"placeholder"
                )


def test_handler_prefixes_are_doi_prefixes() -> None:
    for h in all_handlers():
        for prefix in h.doi_prefixes:
            assert prefix.startswith("10."), (
                f"handler '{h.name}' non-DOI prefix: {prefix!r}"
            )


def test_no_duplicate_doi_prefixes_across_handlers() -> None:
    seen: dict[str, str] = {}
    for h in all_handlers():
        for prefix in h.doi_prefixes:
            assert prefix not in seen, (
                f"DOI prefix {prefix} claimed by both '{seen[prefix]}' "
                f"and '{h.name}'"
            )
            seen[prefix] = h.name


def test_concurrency_and_delay_are_sane() -> None:
    for h in all_handlers():
        assert isinstance(h.concurrency, int) and h.concurrency >= 1, (
            f"handler '{h.name}' concurrency invalid: {h.concurrency!r}"
        )
        assert h.delay_s >= 0, (
            f"handler '{h.name}' delay_s invalid: {h.delay_s!r}"
        )


def test_aom_handler_is_registered() -> None:
    """AoM is the canonical login-required publisher."""
    by_name = {h.name: h for h in all_handlers()}
    assert "aom" in by_name
    assert "10.5465/" in by_name["aom"].doi_prefixes
