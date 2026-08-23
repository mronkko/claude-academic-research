# Why these PDFs were not retrieved — diagnosis notes

**Library:** user library 5591, collection `Analyzed systematic reviews`
(`4R6JI5BR`, parent *AI Literature Review Study*) + 60 subcollections.
**Investigated:** 2026-08-23, plugin 0.15.0, Aalto VPN active.
**Evidence base:** `output/pdf_fetch_log.csv` (655 rows, 2026-08-17) and a live
census of the collection subtree.

---

## Census

| Measure | Count |
|---|---|
| journalArticle items in subtree | 9,457 |
| …without a real PDF | 1,821 |
| …**never attempted** (no row in the fetch log) | **1,651 (91%)** |
| non-journalArticle items missing a PDF (invisible to the pipeline) | 125 |

`enrich_pdfs.py:3678` calls `zot.journal_articles()`, which is
`z.items(itemType="journalArticle")`. Books, book sections, reports, webpages,
preprints and datasets are therefore never enumerated at all — a separate gap
from `DEFAULT_OUT_OF_SCOPE_TYPES`, which only *labels* a failure after the fact.

### Missing PDFs by routing

| Route | Count |
|---|---|
| **No browser handler for the DOI prefix** | **818** |
| tandf | 177 |
| sage | 147 |
| wiley | 146 |
| aom | 137 |
| apa | 134 |
| springer | 109 |
| emerald | 69 |
| informs | 55 |
| oup | 28 |
| no DOI at all | 1 |

### Unhandled prefixes (top 10)

| Prefix | Count | Publisher | Note |
|---|---|---|---|
| 10.2307 | 106 | JSTOR | platform, no handler |
| 10.1016 | 82 | Elsevier | *not* unrouted — `sciencedirect.py` handles it by API; failing on the known TDM entitlement gap |
| 10.1136 | 68 | BMJ | no handler |
| 10.1017 | 61 | Cambridge UP | no handler |
| 10.1109 | 50 | IEEE | no handler |
| 10.1097 | 41 | Lippincott | no handler |
| 10.7748 | 30 | RCNi | no handler |
| 10.1353 | 29 | Project MUSE | no handler |
| 10.1515 | 14 | De Gruyter | no handler; much of it is OA |
| 10.1145 | 10 | ACM | no handler; ToS forbids scripted download |

The 818 figure counts *browser* handlers only. API-side fetchers
(`sciencedirect.py`, `preprint.py` for 10.48550 arXiv) cover some of it.

---

## The twelve citations

| # | DOI | Zotero key | Handler | OA | Tried? | Diagnosis |
|---|---|---|---|---|---|---|
| 1 | 10.1287/deca.2020.eb.v1701 | QVNPHKZ5 | informs | **yes** | never | **Handler works.** Verified live: 167 KB in 3.4 s. Only ever missing because it was never queued. |
| 2 | 10.1108/sd-08-2014-0104 | 8AX93PWX | emerald | no | never | Handler exists; never queued. |
| 3 | 10.1109/tnn.2004.842673 | 6NR667DA | **none** | no | never | IEEE — no handler. |
| 4 | 10.1080/09585192.2011.543629 | D3BX5AVH | tandf | no | never | Handler exists; never queued. |
| 5 | 10.5465/ambpp.2005.18778663 | WT9BHNRT | aom | no | never | Handler exists; never queued. |
| 6 | 10.1109/tcsvt.2016.2615518 | NX7HDGSD | **none** | no | never | IEEE — no handler. |
| 7 | 10.1287/mnsc.2022.4424 | FRU5HVCN | informs | no | never | No INFORMS subscription. Correct route is resolver → EBSCO (see below). |
| 8 | 10.1515/erj-2014-0005 | GGSRJUVX | **none** | **yes** | never | **Open access.** Direct publisher PDF at degruyter.com. Needs no handler, just an attempt. |
| 9 | 10.1145/3534585 | IIAEP36Q | **none** | no | tried | ACM. Logged `BROWSER_REQUIRED` with an empty `untried_handler` — meaning the *Connector* pass, not a publisher handler. |
| 10 | 10.1017/s0305741023001467 | XT2642KN | **none** | no | never | Cambridge UP — no handler. |
| 11 | 10.3233/faia200389 | JFX226PH | **none** | **yes** | tried | IOS Press FAIA (ECAI proceedings). OA, but Unpaywall exposes only the DOI URL, no direct PDF, so the cascade could not resolve it. Currently `journalArticle`; the Connector would import it as `bookSection`, which would make it invisible to `enrich_pdfs.py` entirely. |
| 12 | 10.5465/amj.2010.52814593 | JM2AVBTW | aom | no | never | Handler exists; never queued. |

Ten of twelve were never attempted. The two that were both logged
`BROWSER_REQUIRED` with no publisher handler named.

---

## Root causes, in order of size

1. **Scope.** The pipeline has only ever run over a 655-item slice. 1,651 of the
   1,821 gaps are simply unqueued work.
2. **No handler.** 818 items route nowhere on the browser side.
3. **Item type.** 125 non-journalArticle items are invisible to
   `enrich_pdfs.py` before any routing decision is made.
4. **Publisher subscription.** INFORMS (55) is not subscribed, but EBSCO carries
   it. The architecture already handles this: `classify_direct_route` asks the
   Alma resolver whether the publisher's own platform holds the item and, on
   `1b-no-entitlement`, skips the direct handler so Pass 3 routes to
   `fetchers/browser/ebsco.py`. Untested here only because the items were never
   queued.

## Publisher API notes

- **IEEE** — four APIs at developer.ieee.org: Metadata Search, DOI (25/call),
  Open Access, Full-Text Access. Could not confirm whether a standard
  institutional Xplore subscription unlocks Full-Text, or whether it needs a
  separate agreement like Elsevier TDM. A browser handler against
  `ieeexplore.ieee.org` (`/stamp/stamp.jsp?arnumber=N`) is the lower-risk build.
- **ACM** — no public full-text API. TDM is case-by-case via permissions@acm.org.
  ACM's terms name scripted downloading as a "serious violation" that terminates
  download rights, and enforcement would land on Aalto's IP range. Only 10 items;
  check with the library before building anything.

## Operational gotchas found

- Headed Playwright dies under the Bash tool sandbox
  (`Target page, context or browser has been closed` ~3 s in). Browser probes
  need the sandbox disabled.
- `ZOTERO_API_KEY` is exported in the shell environment, which routes around the
  `permissions.deny` rule on `config.toml` that exists to keep keys out of
  conversations.

---

# Follow-up: handler build (2026-08-23)

Before writing any handler, each candidate publisher was probed live for
(a) whether a PDF route exists on the page and (b) whether it downloads from
this institution's IP. That distinction reordered the build list — three of the
five biggest "no handler" buckets turned out to be **access** gaps, where a
handler would not have helped.

| Publisher | Items | Probe result | Verdict |
|---|---|---|---|
| **IEEE** | 50 | `stamp.jsp` is an HTML viewer; its iframe `getPDF.jsp` fires the download | **built** |
| **Cambridge UP** | 61 | landing page exposes a direct `.pdf` href that downloads | **built** |
| BMJ | 68 | `.full.pdf` and `/content/oemed/...full.pdf` both redirect back to the abstract | no access — skip |
| Ovid / Lippincott | 41 | "Download PDF" href redirects back to the fulltext page | no access — skip |
| JSTOR | 106 | landing page exposes no PDF anchor at all; ToS also prohibits scripted download | skip |

## What was added

- `scripts/pipelines/fetchers/browser/ieee.py` — `IeeeHandler`. Two hops:
  `doi.org` → `/document/{arnumber}`, then
  `/stampPDF/getPDF.jsp?tp=&arnumber={N}&ref=`. The article number comes from
  the landing URL, with the toolbar anchor's `arnumber=` query parameter as a
  fallback, and the `stamp.jsp` viewer's iframe `src` as a second fallback if
  the direct URL shape ever moves.
- `scripts/pipelines/fetchers/browser/cambridge.py` — `CambridgeHandler`, a
  `PdfLinkNavigationHandler` with a custom `pdf_link_selector`. The shared
  default does not match: Cambridge's path segment is `/content/view/`, not
  `/pdf/` or `article-pdf`.
- Registered both in `fetchers/browser/__init__.py`.
- Added `ieee` and `cambridge` to `PLATFORM_PRIORITY` in
  `fetchers/resolvers/base.py`. This is not cosmetic — on an Alma library that
  table is the *identity map*, so a platform missing from it is invisible to the
  Case 1/2/3 coverage guard and its handler can only ever be Case 1.
- Added both to `KNOWN_DOIS` in `tests/live/conftest.py`; the live browser test
  is auto-parametrized from the registry.
- Updated `test_registry_has_exactly_ten_handlers` → `..._twelve_handlers`.

## Verification

`ruff check scripts tests` clean; `pytest tests/ -q` → 2,096 passed, 3 skipped.

Live probes, cold-ish profile, Aalto VPN:

```
ieee       3/3   10.1109/tcsvt.2016.2615518 (4404KB)
                 10.1109/tnn.2004.842673      (46KB)   <- citation #3
                 10.1109/te.2021.3101401     (499KB)
cambridge  4/4   10.1017/s0305741023001467   (407KB)   <- citation #10
                 10.1017/als.2015.2          (279KB)
                 10.1017/s0147547918000108   (431KB)
                 10.1017/lap.2019.62         (448KB)
```

Neither handler needed an interactive Cloudflare or SSO solve, so both declare
`needs_interactive_solve = False`.

---

# Results: full-subtree run + JSTOR run (2026-08-23)

## Pass 1 — API cascade over the whole subtree

`enrich_pdfs.py --user --filter-keys-file output/subtree-missing.keys --workers 6 --no-prompt`

1,820 items, 42 minutes, **231 PDFs attached, 0 failures**.

| Source | Attached |
|---|---|
| wiley | 68 |
| openalex_content | 49 |
| crossref | 46 |
| sciencedirect | 32 |
| semantic_scholar | 19 |
| pubmed_central | 12 |
| openalex | 5 |

ScienceDirect contributing 32 is worth noting — the Elsevier route is degraded by
the TDM entitlement gap, not dead.

## JSTOR run — the resolver reroutes most of it away from JSTOR

`enrich_pdfs.py --user --filter-keys-file output/jstor.keys --sources connector --no-prompt`

Pass 3 split the 106 JSTOR-prefixed items:

- **EBSCOhost (resolver-routed): 59 → 59 downloaded, 0 failed.** Unattended, IP
  auth, no JYU EZproxy login involved. 58 attached + 1 `attached_no_text`.
- **Zotero Connector: 47 → not attempted.** `--no-prompt` auto-answered "skip" at
  the Connector's confirmation gate. Not a failure; that pass is interactive by
  design (first item per host may need a login / CAPTCHA solved by hand, and
  JSTOR sometimes raises Zotero's "Select which items" picker).

This corrects the pessimistic read in the section above. Direct jstor.org exposes
no PDF anchor for this institution, but that is not the route the resolver picks
— EBSCO carries most of these titles and reaches them silently.

The run also warned: *"could not determine Zotero Desktop's selected library."*
Connector saves land in whichever library is selected in Zotero Desktop's left
pane, so My Library must be selected before an interactive Connector run.

## Re-census

| Measure | This morning | Now |
|---|---|---|
| journalArticle in subtree | 9,457 | 9,457 |
| **without a PDF** | **1,821** | **1,531** |
| no browser handler | 818 | 557 |
| JSTOR (10.2307) missing | 106 | 47 |

**290 recovered today**, none of it requiring the new handlers.

### Remaining, by route

| Route | Count |
|---|---|
| NO HANDLER | 557 |
| tandf | 172 |
| aom | 131 |
| apa | 128 |
| sage | 116 |
| springer | 101 |
| wiley | 77 |
| emerald | 64 |
| **cambridge (new)** | **61** |
| **ieee (new)** | **50** |
| informs | 47 |
| oup | 26 |

### Remaining unhandled prefixes (top 8)

| Prefix | Count | Note |
|---|---|---|
| 10.1136 | 61 | BMJ — probed, no access |
| 10.1016 | 50 | Elsevier — API route, TDM gap |
| 10.2307 | 47 | JSTOR — awaiting interactive Connector run |
| 10.1097 | 41 | Ovid — probed, no access |
| 10.7748 | 30 | RCNi |
| 10.1353 | 28 | Project MUSE |
| 10.1086 | 13 | Chicago |
| 10.1515 | 12 | De Gruyter |

## Next levers

1. **Browser pass for the two new handlers** — 111 items (cambridge 61 + ieee 50),
   both `needs_interactive_solve = False`, so unattended.
2. **Interactive Connector run** for the 47 remaining JSTOR items, from a real
   terminal (drop `--no-prompt`), with My Library selected in Zotero Desktop.
3. The ~880 items behind existing handlers (tandf/aom/apa/sage/springer/wiley/
   emerald/oup) have never had a browser pass over them.

---

# New-handler browser pass (2026-08-23, Aalto VPN)

111 items queued (`output/ieee-cambridge.keys`). Pass 3 split them:
Cambridge 36 direct, IEEE 50 direct, Connector upfront 25, plus 11 that the
resolver said were licensed but *not* via Cambridge.

## IEEE — 50/50, zero failures

```
publisher_done  ieee  queued=50  ok=50  cached=0  failed=0
```

`10.1109` no longer appears in the remaining-by-route table at all. The
two-hop `getPDF.jsp` flow held for every item: journals, conference
proceedings (EuroS&P, HPEC, ICSTW, TALE), and magazines alike.

## Cambridge — 29/36, in two runs

The first run stopped at **9 of 36**. Item 10 was
`10.1017/9781108610070.037`, a *book chapter* in the Cambridge Handbook of
U.S. Labor Law, whose landing page has no journal PDF anchor. That failure
opened the handler's setup step, and `--no-prompt` answers the setup gate
`skip` — abandoning the publisher.

**`--on-first-failure=keep` does not cover this.** It governs the Option-4
first-failure prompt; the setup gate ("Can you see/reach the PDF from this
page?") is a separate prompt that `--no-prompt` always answers `skip`. For an
unattended run over a publisher whose queue may contain a dud, use
`--control-file` instead, which is the documented agent channel.

The re-run used `--control-file` with an auto-responder answering `Y` to the
setup gate and `k` to the first-failure prompt: **20 more attached, 8 failed.**

Seven of those eight failures are book-style DOIs catalogued as
`journalArticle`:

```
10.1017/9781108610070.031   10.1017/9781108610070.037
10.1017/9781316717653.004   10.1017/9781108610070.025
10.1017/cbo9781139026918.007  10.1017/cbo9781107282018.004
10.1017/9781108610070.025
```

Only `10.1017/s0147547903000231` is a real journal article; it resolves to a
legacy `journals.cambridge.org/abstract_...` page with no modern PDF anchor.

**This is the item-type problem from the first census showing up in a
different place.** These are Cambridge *books*, mis-typed as journal articles
in Zotero, so they pass the `journalArticle` filter and then fail on a handler
that is correctly written for journals. Fixing their item type would move them
out of scope honestly instead.

*Caveat on the re-run:* the auto-responder answered `k` to the Connector
fallback's `Ready to start? [Y]es/[n]o` prompt. `k` is not in that prompt's
vocabulary, so it proceeded rather than skipping; one item was attempted and
logged `connector_save_failed`. Harmless, but the responder should match answer
vocabularies per prompt.

## Running total

| Measure | Start of day | Now |
|---|---|---|
| journalArticle in subtree | 9,457 | 9,457 |
| **without a PDF** | **1,821** | **1,452** |

**369 recovered today:** 231 API cascade + 59 EBSCO (JSTOR queue) + 50 IEEE +
29 Cambridge.

### Remaining by route

| Route | Count |
|---|---|
| NO HANDLER | 557 |
| tandf | 172 |
| aom | 131 |
| apa | 128 |
| sage | 116 |
| springer | 101 |
| wiley | 77 |
| emerald | 64 |
| informs | 47 |
| cambridge | 32 |
| oup | 26 |
