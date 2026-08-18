"""The handler probe is a debugging tool, so its contract is small.

It exists because four rounds of APA fixes were each paid for with a
full `enrich_pdfs.py` startup — a Zotero fetch of the whole key list, an
attachment scan, and a resolver pre-flight over the residual — before
the browser opened. What is pinned here is only what would make the
probe lie: driving something other than the real handler, or quietly
resolving a name to the wrong class.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = (Path(__file__).resolve().parents[2]
         / "scripts" / "dev" / "probe_browser_handler.py")


def _module():
    spec = importlib.util.spec_from_file_location("probe_browser_handler", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_probe_exists_and_imports() -> None:
    assert _PATH.is_file()
    assert _module() is not None


@pytest.mark.parametrize("name", ["apa", "sage", "tandf", "springer", "wiley"])
def test_it_resolves_registry_handlers_to_the_real_class(name: str) -> None:
    """The probe must drive the shipped handler. A stand-in would make
    every result meaningless."""
    mod = _module()
    handler = mod._handler_by_name(name)
    assert handler.name == name
    assert hasattr(handler, "download")


def test_it_reaches_ebsco_which_is_kept_out_of_the_registry() -> None:
    """`EbscoHandler` is deliberately absent from `all_handlers()` —
    nothing routes to it by DOI — which makes it precisely the handler a
    human most needs to drive by hand."""
    assert _module()._handler_by_name("ebsco").name == "ebsco"


def test_an_unknown_handler_lists_the_real_options() -> None:
    mod = _module()
    with pytest.raises(SystemExit) as excinfo:
        mod._handler_by_name("nope")
    message = str(excinfo.value)
    assert "nope" in message
    assert "apa" in message and "ebsco" in message


def test_it_declares_playwright_in_its_pep723_block() -> None:
    """`uv run` builds the environment from this block; without
    playwright the probe dies at import, which is a confusing way to
    learn you mistyped a dependency."""
    head = _PATH.read_text().split("# ///")[1]
    assert "playwright" in head


def test_it_offers_a_way_to_defeat_its_own_cache() -> None:
    """A probe whose second run is a cache hit cannot answer "did the fix
    work". Live: two DOIs were fetched, then the identical command
    reported both from cache and exercised nothing — while printing
    "0 ok, 0 failed", which read like a failure.
    """
    src = _PATH.read_text()
    assert '"--fresh"' in src
    assert '"--fresh-profile"' in src


def test_a_cache_hit_is_not_counted_as_a_fetch() -> None:
    """The summary must distinguish "the handler did this" from "a file
    was already on disk", or a green run means nothing."""
    src = _PATH.read_text()
    assert "served from cache" in src
    assert "exercised nothing" in src
