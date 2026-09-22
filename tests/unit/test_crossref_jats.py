"""Crossref abstracts arrive as JATS XML."""

from __future__ import annotations

from fetchers.crossref import _strip_jats


def test_jats_title_and_entities() -> None:
    raw = (
        "<jats:title>Abstract</jats:title><jats:p>We study labor unions &amp; "
        "wages across firms &lt;50 employees in twelve countries.</jats:p>"
    )
    assert _strip_jats(raw) == (
        "We study labor unions & wages across firms <50 employees in twelve countries."
    )


def test_entities_encoding_tags_are_not_turned_into_tags_and_stripped() -> None:
    """Unescaping before stripping would let `&lt;50` open a fake tag and
    eat the text after it."""
    raw = "<jats:p>Samples of &lt;50 and &gt;10 firms were compared over a long period.</jats:p>"
    assert "<50 and >10 firms" in _strip_jats(raw)
