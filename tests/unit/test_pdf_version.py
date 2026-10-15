"""`pdf:version:*` — which version of the article an attached PDF is.

Step 1 of the PDF retrieval redesign (`~/.claude/plans/pdf-retrieval.md`).
The rule under test throughout is "never over-claim": the planned
`[pdf] min_version = published` gate will trust this tag, so a manuscript
tagged `published` is the failure that matters, and every uncertain case
must land on `accepted` (or `submitted`), never higher.

No network: fetchers get MagicMock sessions.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import enrich_pdfs
import fetchers
import pytest
from fetchers import pdf_version as pv
from fetchers.browser.connector import PendingMerges
from fetchers.resolvers.base import FulltextTarget


def _pdf(marker: bytes = b"body") -> bytes:
    return b"%PDF-1.4\n" + marker + b"\n" + b"0" * 3000 + b"\nstartxref\n12\n%%EOF\n"


def _resp(content: bytes = b"", json_data=None, status: int = 200):
    r = MagicMock()
    r.status_code = status
    r.content = content
    r.headers = {}
    r.json.return_value = json_data
    return r


# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------

def test_three_tags_in_one_global_family() -> None:
    assert pv.VERSION_TAGS == (
        "pdf:version:published", "pdf:version:accepted", "pdf:version:submitted",
    )
    with pytest.raises(ValueError):
        pv.tag_for("publishedVersion")


def test_version_tags_are_provenance_so_a_swap_clears_them() -> None:
    """`--replace` drops provenance tags the new file does not earn; a
    version tag left from the old copy would be a false statement."""
    assert set(pv.VERSION_TAGS) <= set(enrich_pdfs._PROVENANCE_TAGS)


def test_version_tag_op_leaves_exactly_one() -> None:
    add, remove = enrich_pdfs._version_tag_op(pv.ACCEPTED)
    assert add == ["pdf:version:accepted"]
    assert set(remove) == {"pdf:version:published", "pdf:version:submitted"}


# ---------------------------------------------------------------------------
# Source → version
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name, version", [
    ("sciencedirect", pv.PUBLISHED), ("springer", pv.PUBLISHED),
    ("wiley", pv.PUBLISHED), ("openalex_content", pv.PUBLISHED),
    ("browser", pv.PUBLISHED),
    ("arxiv", pv.SUBMITTED), ("preprint", pv.SUBMITTED),
    ("core", pv.ACCEPTED), ("openaire", pv.ACCEPTED), ("base", pv.ACCEPTED),
])
def test_static_source_versions(name, version) -> None:
    assert pv.STATIC_SOURCE_VERSION[name] == version


def test_every_pdf_source_is_classified() -> None:
    """A new fetcher must say how its version is decided — otherwise it
    silently falls to the `accepted` default, which may be wrong in
    either direction."""
    names = {s.name for s in fetchers.pdf_sources(MagicMock(), allow_preprints=True)}
    names |= {"browser"}
    unclassified = names - set(pv.STATIC_SOURCE_VERSION) - pv.API_FIELD_SOURCES
    assert not unclassified, f"PDF sources with no version rule: {unclassified}"


def test_every_browser_handler_goes_through_the_published_settle() -> None:
    """All handlers drive a publisher platform (EBSCOhost included); the
    browser lane settles them with `BROWSER_HANDLER_VERSION`."""
    from fetchers.browser import EbscoHandler, all_handlers

    names = {h.name for h in all_handlers()} | {EbscoHandler.name}
    assert "connector" not in names
    assert pv.BROWSER_HANDLER_VERSION == pv.PUBLISHED
    src = inspect.getsource(enrich_pdfs._drive_handler)
    assert "static=pdf_version.BROWSER_HANDLER_VERSION" in src


# ---------------------------------------------------------------------------
# API fields → version (the measured rule)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("version, host_type, url, expected", [
    # The one combination the measurement found reliable (17/17).
    ("publishedVersion", "publisher", "https://www.nature.com/a.pdf", pv.PUBLISHED),
    ("publishedVersion", "journal", "https://www.nature.com/a.pdf", pv.PUBLISHED),
    # publishedVersion on a repository: 0/1 — an arXiv copy.
    ("publishedVersion", "repository", "https://pure.uva.nl/x.pdf", pv.ACCEPTED),
    ("publishedVersion", "repository", "https://arxiv.org/pdf/2106.00307", pv.SUBMITTED),
    # acceptedVersion: trusted only as a floor, never as `published`.
    ("acceptedVersion", "repository", "https://eprints.whiterose.ac.uk/x.pdf", pv.ACCEPTED),
    # ...and not at all on a preprint server (5/5 wrong in the sample).
    ("acceptedVersion", "repository",
     "https://www.researchsquare.com/article/rs-1/v1.pdf", pv.SUBMITTED),
    ("acceptedVersion", "repository",
     "https://www.biorxiv.org/content/x.full.pdf", pv.SUBMITTED),
    # submittedVersion is the default label for repository copies (18 of
    # 40 were the typeset PDF): not believed, so the floor applies.
    ("submittedVersion", "repository", "https://discovery.ucl.ac.uk/x.pdf", pv.ACCEPTED),
    ("submittedVersion", "repository", "https://arxiv.org/pdf/2007.05202", pv.SUBMITTED),
    (None, None, "https://example.org/x.pdf", pv.ACCEPTED),
    ("publishedVersion", None, "https://example.org/x.pdf", pv.ACCEPTED),
])
def test_from_oa_location(version, host_type, url, expected) -> None:
    assert pv.from_oa_location(version=version, host_type=host_type, url=url) == expected


def test_openalex_reads_host_type_from_the_source() -> None:
    loc = {"version": "publishedVersion", "source": {"type": "journal"}}
    assert pv.from_openalex_location(loc, "https://x.org/a.pdf") == pv.PUBLISHED
    assert pv.from_openalex_location(None, "https://x.org/a.pdf") == pv.ACCEPTED


def test_preprint_url_matches_by_host_suffix_only() -> None:
    assert pv.is_preprint_url("https://export.arxiv.org/pdf/1")
    assert not pv.is_preprint_url("https://arxiv.org.example.com/pdf/1")
    assert not pv.is_preprint_url("not a url")


def test_bioRxiv_prefix_is_not_a_preprint_doi() -> None:
    """Cold Spring Harbor's peer-reviewed journals share 10.1101."""
    assert pv.is_preprint_doi("10.48550/arXiv.2101.00001")
    assert pv.is_preprint_doi("10.2139/ssrn.3820062")
    assert not pv.is_preprint_doi("10.1101/gr.123456.111")


@pytest.mark.parametrize("cv, expected", [
    ("am", pv.ACCEPTED), ("AM", pv.ACCEPTED),
    ("vor", pv.PUBLISHED), ("unspecified", pv.PUBLISHED), (None, pv.PUBLISHED),
])
def test_from_crossref_link(cv, expected) -> None:
    assert pv.from_crossref_link(cv) == expected


@pytest.mark.parametrize("record, expected", [
    # Real shapes from the NCBI ID converter, versions=yes (2026-10-15).
    ({"pmcid": "PMC9997839", "versions": [
        {"pmcid": "PMC9997839.1", "mid": "NIHMS1865295", "current": True}]},
     pv.ACCEPTED),
    ({"pmcid": "PMC7113368", "versions": [
        {"pmcid": "PMC7113368.1", "current": True}]}, pv.PUBLISHED),
    # The current version decides, not an older one.
    ({"pmcid": "PMC1", "versions": [
        {"pmcid": "PMC1.1", "mid": "NIHMS1"},
        {"pmcid": "PMC1.2", "current": True}]}, pv.PUBLISHED),
    # No versions list: unknown, so the floor.
    ({"pmcid": "PMC2"}, pv.ACCEPTED),
    ({"pmcid": "PMC3", "mid": "NIHMS3"}, pv.ACCEPTED),
])
def test_from_pmc_record(record, expected) -> None:
    assert pv.from_pmc_record(record) == expected


def test_semantic_scholar_has_no_version_to_trust() -> None:
    assert pv.from_semantic_scholar("https://arxiv.org/pdf/1") == pv.SUBMITTED
    assert pv.from_semantic_scholar("https://www.nature.com/a.pdf") == pv.ACCEPTED


# ---------------------------------------------------------------------------
# The sidecar and `settle`
# ---------------------------------------------------------------------------

def test_sidecar_round_trip(tmp_path) -> None:
    pdf = tmp_path / "10.1_x.pdf"
    pdf.write_bytes(_pdf())
    pv.record(pdf, pv.PUBLISHED, source="sciencedirect")
    assert pv.sidecar_path(pdf).name == "10.1_x.pdf.version.json"
    assert pv.recorded(pdf) == pv.PUBLISHED


def test_a_sidecar_for_other_bytes_is_ignored(tmp_path) -> None:
    """Most sources share `<doi>.pdf`: when one overwrites another's file,
    the old sidecar must not describe the new bytes."""
    pdf = tmp_path / "10.1_x.pdf"
    pdf.write_bytes(_pdf(b"vor"))
    pv.record(pdf, pv.PUBLISHED)
    pdf.write_bytes(_pdf(b"manuscript"))
    assert pv.recorded(pdf) is None
    assert pv.version_of(pdf) == pv.ACCEPTED


def test_unknown_provenance_is_accepted_except_an_arxiv_doi(tmp_path) -> None:
    pdf = tmp_path / "legacy.pdf"
    pdf.write_bytes(_pdf())
    assert pv.version_of(pdf, doi="10.1016/j.x") == pv.ACCEPTED
    assert pv.version_of(pdf, doi="10.48550/arxiv.2101.1") == pv.SUBMITTED


@pytest.mark.parametrize("suffix, expected", [
    ("-repository-copy", pv.ACCEPTED),
    ("-preprint", pv.SUBMITTED),
    ("-tdm-recovered", pv.PUBLISHED),
])
def test_provenance_suffix_outranks_the_sidecar(tmp_path, suffix, expected) -> None:
    pdf = tmp_path / f"10.1_x{suffix}.pdf"
    pdf.write_bytes(_pdf())
    other = next(v for v in pv.VERSIONS if v != expected)
    pv.record(pdf, other)
    assert pv.version_of(pdf) == expected


def test_settle_fresh_static_source_records_and_overwrites(tmp_path) -> None:
    pdf = tmp_path / "10.1_x.pdf"
    pdf.write_bytes(_pdf())
    pv.record(pdf, pv.ACCEPTED, source="unpaywall")     # same bytes, stale claim
    got = pv.settle(pdf, source="sciencedirect", source_url="https://api.elsevier.com/x")
    assert got == pv.PUBLISHED
    assert pv.recorded(pdf) == pv.PUBLISHED


def test_settle_cache_hit_keeps_the_writers_version(tmp_path) -> None:
    """A ScienceDirect cache hit on a file Unpaywall downloaded is still
    Unpaywall's manuscript."""
    pdf = tmp_path / "10.1_x.pdf"
    pdf.write_bytes(_pdf())
    pv.record(pdf, pv.ACCEPTED, source="unpaywall")
    got = pv.settle(pdf, source="sciencedirect", source_url=f"cache://{pdf}")
    assert got == pv.ACCEPTED


def test_settle_cache_hit_without_sidecar_does_not_trust_the_source(tmp_path) -> None:
    pdf = tmp_path / "10.1_x.pdf"
    pdf.write_bytes(_pdf())
    got = pv.settle(pdf, source="sciencedirect", source_url=f"cache://{pdf}")
    assert got == pv.ACCEPTED
    assert pv.recorded(pdf) is None              # a guess is not recorded


def test_settle_browser_handler_static(tmp_path) -> None:
    pdf = tmp_path / "10.1177_x.pdf"
    pdf.write_bytes(_pdf())
    got = pv.settle(
        pdf, source="sage", source_url="https://journals.sagepub.com/doi/pdf/x",
        static=pv.BROWSER_HANDLER_VERSION,
    )
    assert got == pv.PUBLISHED
    # A later cache hit (the attach-recovery pass) reads it back.
    assert pv.settle(pdf, source="sage", source_url=f"cache://{pdf}") == pv.PUBLISHED


def test_settle_api_field_source_uses_its_own_record(tmp_path) -> None:
    pdf = tmp_path / "10.1_x.pdf"
    pdf.write_bytes(_pdf())
    pv.record(pdf, pv.PUBLISHED, source="unpaywall")
    assert pv.settle(pdf, source="unpaywall", source_url="https://x.org/a.pdf") == pv.PUBLISHED


# ---------------------------------------------------------------------------
# Fetchers record the version from their API's answer
# ---------------------------------------------------------------------------

def test_unpaywall_records_the_chosen_locations_version(tmp_path, monkeypatch) -> None:
    from fetchers.unpaywall import UnpaywallSource

    monkeypatch.setenv("CROSSREF_MAILTO", "a@b.c")
    src = UnpaywallSource(MagicMock())
    meta = {
        "best_oa_location": {
            "url_for_pdf": "https://www.nature.com/a.pdf",
            "version": "publishedVersion", "host_type": "publisher",
        },
    }
    src.http.get.side_effect = [_resp(json_data=meta), _resp(_pdf())]
    path, url = src.fetch_pdf("10.1038/x", cache_dir=str(tmp_path))
    assert pv.recorded(path) == pv.PUBLISHED

    # Cache round trip: the second run serves from disk and the version
    # comes back with it.
    src.http.get.side_effect = AssertionError("must not hit the network")
    path2, url2 = src.fetch_pdf("10.1038/x", cache_dir=str(tmp_path))
    assert url2.startswith("cache://")
    assert pv.settle(path2, source="unpaywall", source_url=url2) == pv.PUBLISHED


def test_unpaywall_fallback_location_carries_its_own_version(tmp_path, monkeypatch) -> None:
    from fetchers.unpaywall import UnpaywallSource

    monkeypatch.setenv("CROSSREF_MAILTO", "a@b.c")
    src = UnpaywallSource(MagicMock())
    meta = {
        "best_oa_location": {"version": "publishedVersion", "host_type": "publisher"},
        "oa_locations": [{
            "url_for_pdf": "https://discovery.ucl.ac.uk/1/am.pdf",
            "version": "submittedVersion", "host_type": "repository",
        }],
    }
    src.http.get.side_effect = [_resp(json_data=meta), _resp(_pdf())]
    path, _ = src.fetch_pdf("10.1037/x", cache_dir=str(tmp_path))
    assert pv.recorded(path) == pv.ACCEPTED


def test_openalex_oa_records_the_best_location(tmp_path) -> None:
    from fetchers.openalex import OpenAlexSource

    src = OpenAlexSource(MagicMock(), config=MagicMock(crossref_mailto="a@b.c"))
    src._ensure_configured = lambda: None
    url = "https://arxiv.org/pdf/2007.11630"
    src._work = lambda doi, **kw: {
        "open_access": {"oa_url": url},
        "best_oa_location": {"pdf_url": url, "version": "publishedVersion",
                             "source": {"type": "repository"}},
    }
    src.http.get.return_value = _resp(_pdf())
    path, _ = src.fetch_pdf("10.1103/x", cache_dir=str(tmp_path))
    assert pv.recorded(path) == pv.SUBMITTED


def test_openalex_oa_url_not_matching_best_location_is_not_trusted(tmp_path) -> None:
    from fetchers.openalex import OpenAlexSource

    src = OpenAlexSource(MagicMock(), config=MagicMock(crossref_mailto="a@b.c"))
    src._ensure_configured = lambda: None
    src._work = lambda doi, **kw: {
        "open_access": {"oa_url": "https://pure.x.nl/a.pdf"},
        "best_oa_location": {"pdf_url": "https://www.nature.com/a.pdf",
                             "version": "publishedVersion",
                             "source": {"type": "journal"}},
    }
    src.http.get.return_value = _resp(_pdf())
    path, _ = src.fetch_pdf("10.1038/x", cache_dir=str(tmp_path))
    assert pv.recorded(path) == pv.ACCEPTED


def test_semantic_scholar_records_accepted(tmp_path) -> None:
    from fetchers.semantic_scholar import SemanticScholarSource

    src = SemanticScholarSource(MagicMock(), config=MagicMock())
    src._open_access_pdf_url = lambda doi: "https://www.nature.com/a.pdf"
    src.http.get.return_value = _resp(_pdf())
    path, _ = src.fetch_pdf("10.1038/x", cache_dir=str(tmp_path))
    assert pv.recorded(path) == pv.ACCEPTED


@pytest.mark.parametrize("cv, expected", [("am", pv.ACCEPTED), ("vor", pv.PUBLISHED)])
def test_crossref_records_the_links_content_version(tmp_path, cv, expected) -> None:
    from fetchers.crossref import CrossrefSource

    src = CrossrefSource(MagicMock())
    cr = MagicMock()
    cr.works.return_value = {"message": {"link": [{
        "URL": "https://pub.example/tdm.pdf", "content-type": "application/pdf",
        "intended-application": "text-mining", "content-version": cv,
    }]}}
    src._cr = cr                      # the lazy `cr` property returns it
    src.http.get.return_value = _resp(_pdf())
    path, _ = src.fetch_pdf("10.1016/x", cache_dir=str(tmp_path))
    assert pv.recorded(path) == expected


def test_pmc_asks_for_versions_and_records_a_manuscript(tmp_path) -> None:
    from fetchers.pmc import PmcSource

    src = PmcSource(MagicMock())
    conv = {"records": [{"pmcid": "PMC9997839", "versions": [
        {"pmcid": "PMC9997839.1", "mid": "NIHMS1865295", "current": True}]}]}
    src.http.get.side_effect = [_resp(json_data=conv), _resp(_pdf())]
    path, _ = src.fetch_pdf("10.31487/j.cdm.2019.01.01", cache_dir=str(tmp_path))
    assert "versions=yes" in src.http.get.call_args_list[0].args[0]
    assert pv.recorded(path) == pv.ACCEPTED


# ---------------------------------------------------------------------------
# Tags written on attach
# ---------------------------------------------------------------------------

def _zot():
    zot = MagicMock()
    zot.attach_pdf.return_value = "NEWATT"
    return zot


def _added(zot) -> set[str]:
    return {t for c in zot.update_tags.call_args_list for t in c.kwargs.get("add", ())}


def test_attach_tags_the_recorded_version(tmp_path) -> None:
    pdf = tmp_path / "10.1_x.pdf"
    pdf.write_bytes(_pdf())
    pv.record(pdf, pv.PUBLISHED)
    zot = _zot()
    assert enrich_pdfs._attach_and_log(
        zot, MagicMock(), run_date="d", item_key="K", doi="10.1/x", title="T",
        source="sciencedirect", pdf_path=pdf, check_text=False,
    )
    assert _added(zot) == {"pdf:version:published"}


def test_repository_copy_keeps_its_old_tag_and_gains_accepted(tmp_path) -> None:
    pdf = tmp_path / "10.1_x-repository-copy.pdf"
    pdf.write_bytes(_pdf())
    zot = _zot()
    enrich_pdfs._attach_and_log(
        zot, MagicMock(), run_date="d", item_key="K", doi="10.1/x", title="T",
        source="core", pdf_path=pdf, check_text=False,
    )
    assert _added(zot) == {"pdf:repository-copy", "pdf:version:accepted"}


def test_cascade_then_cache_recovery_tag_the_same_version(tmp_path) -> None:
    """The version decided during the cascade survives to a later run
    whose only evidence is the cached file (`_attach_from_cache`)."""
    import argparse

    pdf = tmp_path / "10.1016_j.x.pdf"
    pdf.write_bytes(_pdf())
    source = MagicMock()
    source.name = "sciencedirect"
    source.fetch_pdf.return_value = (pdf, "https://api.elsevier.com/x")
    item = {"key": "K", "data": {"DOI": "10.1016/j.x", "title": "T"}}
    path, name = enrich_pdfs._try_cascade(item, [source], str(tmp_path))
    assert name == "sciencedirect"

    zot = _zot()
    args = argparse.Namespace(
        cache_dir=str(tmp_path), failure_log_csv=None, no_check_text=True,
    )
    done = enrich_pdfs._attach_from_cache([item], zot, MagicMock(), args, "d")
    assert done == [item]
    assert _added(zot) == {"pdf:version:published"}


# ---------------------------------------------------------------------------
# The Zotero Connector
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("target, url, doi, expected", [
    # Fell open to the DOI landing page: the publisher's.
    (None, "https://doi.org/10.1177/x", "10.1177/x", pv.PUBLISHED),
    # A preprint server's DOI lands on the preprint.
    (None, "https://doi.org/10.2139/ssrn.1", "10.2139/ssrn.1", pv.SUBMITTED),
    (None, "https://repo.example.edu/1", "10.1/x", pv.ACCEPTED),
    # Alma: the redirector host names nothing; the package name does.
    (FulltextTarget(url="https://x.alma.exlibrisgroup.com/view/uresolver/1",
                    package_name="EBSCOhost Business Source Ultimate"),
     "https://x.alma.exlibrisgroup.com/view/uresolver/1", "10.1/x", pv.PUBLISHED),
    # EZproxy wrapper around a publisher host.
    (FulltextTarget(url="https://ezproxy.u.fi/login?url=https://journals.sagepub.com/doi/x"),
     "https://ezproxy.u.fi/login?url=https://journals.sagepub.com/doi/x",
     "10.1177/x", pv.PUBLISHED),
    # EZproxy hostname rewriting.
    (FulltextTarget(url="https://research-ebsco-com.ezproxy.jyu.fi/linkprocessor/x"),
     "https://research-ebsco-com.ezproxy.jyu.fi/linkprocessor/x", "10.1/x", pv.PUBLISHED),
    # A free-list / repository route: whatever the page offers.
    (FulltextTarget(url="https://eprints.example.ac.uk/1/", is_free=True),
     "https://eprints.example.ac.uk/1/", "10.1/x", pv.ACCEPTED),
])
def test_for_connector_target(target, url, doi, expected) -> None:
    assert pv.for_connector_target(target, url, doi) == expected


def test_pass3_puts_the_version_on_the_connector_item() -> None:
    src = inspect.getsource(enrich_pdfs)
    assert '"pdf_version": pdf_version.for_connector_target(' in src


def test_connector_version_survives_the_pending_queue(tmp_path) -> None:
    q = PendingMerges(tmp_path)
    q.add(keeper="K", new_key="N", doi="10.1/x", pdf_version=pv.PUBLISHED)
    q.add(keeper="K2", new_key="N2", doi="10.1/y")         # an old-style row
    rows = {r["new_key"]: r for r in PendingMerges(tmp_path).rows()}
    assert enrich_pdfs._connector_version(rows["N"]) == pv.PUBLISHED
    # Queued before rows carried a version: unknown route, so the floor.
    assert enrich_pdfs._connector_version(rows["N2"]) == pv.ACCEPTED


def test_connector_handler_queues_the_items_version() -> None:
    from fetchers.browser import connector

    src = inspect.getsource(connector.ZoteroConnectorHandler)
    adds = src.count("self.pending.add(")
    assert adds and src.count('pdf_version=item.get("pdf_version", "")') == adds
    assert '"pdf_version": item.get("pdf_version", "")' in src   # background row


def test_finish_connector_attach_tags_and_finishes_a_swap(monkeypatch) -> None:
    zot = MagicMock()
    monkeypatch.setitem(enrich_pdfs._REPLACE_TARGETS, "K", ["OLD"])
    enrich_pdfs._finish_connector_attach(zot, "K", {"NEW"}, pv.PUBLISHED)
    zot.trash_item.assert_called_once_with("OLD")
    last = zot.update_tags.call_args_list[-1].kwargs
    assert last["add"] == ["pdf:version:published"]
    assert set(last["remove"]) == {"pdf:version:accepted", "pdf:version:submitted"}
    # The swap's stale-tag sweep must not remove the tag being added.
    first_remove = set(zot.update_tags.call_args_list[0].kwargs["remove"])
    assert "pdf:version:published" not in first_remove


def test_every_connector_completion_tags_the_version() -> None:
    """Foreground, background and the pending-queue sweep all attach a
    PDF; none of them may skip the tag."""
    loop = inspect.getsource(enrich_pdfs._connector_item_loop)
    driver = inspect.getsource(enrich_pdfs._drive_connector)
    assert "_finish_connector_attach(" in loop
    assert "_connector_version(row)" in driver
    run = inspect.getsource(enrich_pdfs)
    assert run.count("_finish_connector_attach(") >= 4    # def + 3 call sites
