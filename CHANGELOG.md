# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **Wiley TDM now runs in the default cascade.** It was excluded on the
  reasoning that it "requires a specific auth contract", but
  `WileySource.fetch_pdf` already returns `None` on a non-Wiley prefix,
  a missing token, a missing `wiley_tdm` import, or any exception — it
  self-disables exactly as safely as ScienceDirect, which is token-gated
  too and was never excluded. The asymmetry failed silently: `--all`
  builds its Pass 1 from the same default list, so the documented
  "run everything" invocation skipped Wiley outright, and only a reader
  who found `--sources wiley` in a table deep in the skill would run it.
  Measured on a live 1,895-item library pass: 248 Wiley-prefix items,
  39 found by the cascade, and **47 more** recovered by a separate
  `--sources wiley` run that no default invocation would have made.
  Ranked at stage 1, above the OA aggregators, because TDM returns the
  publisher's file and they often return an author manuscript whose
  pagination does not match. `--sources wiley` still selects it alone.

  Expect current content only: on that pass the token answered for ~20%
  of 1990-onward articles and **none** older. That is a back-file
  entitlement gap, not a failure — pre-1990 Wiley is a browser/EBSCO job.

### Fixed

- **`--plan` attached PDFs.** It is documented as "classify items and
  print the publisher queue — then exit without opening a browser", and
  it does stop before the browser. But the Pass 2 API retry runs first,
  and that retry downloads and attaches: it checked `--dry-run` and
  never checked `--plan`. Caught by running `--plan` over a 1,251-item
  queue and watching it write Wiley files into a real library. The
  resolver lookups `--plan` performs are deliberate and stay — they are
  what answers "what will this ask of me" — but a preview that mutates
  is not a preview, and users are told to run this one first.

- **`--publisher` paid the resolver bill for every other publisher.**
  The filter on `items_by_pub` runs *after* the classification loop, so
  a single-publisher run still asked the link resolver about every
  handler-matched item and discarded all but one publisher's. On a live
  1,251-item queue that was ~830 items at two Alma round-trips each to
  keep 162 — and running the ten publisher blocks in sequence paid it
  ten times over. The skip now happens before `lookup_dual`, which is
  safe precisely because the post-loop filter drops those items anyway.

- **The resolver pre-flight looked like a hang.** It printed one
  "Checking library access via …" header and then nothing while it made
  up to two sequential network calls per item — many minutes of silence
  on a large queue, with no way to tell work from a stall. It now says
  how many items it will check up front and prints a progress line with
  a rate every 50 resolver queries.

- **Legacy imprint DOI prefixes matched no handler.** Publishers absorb
  each other and the acquired prefixes keep resolving on the new owner's
  platform, under the same URL shape the handler already builds — so one
  missing line sends an item to the resolver route instead of straight
  to its publisher. Added `10.1023` (Kluwer → Springer, 12 items on the
  live pass), `10.4324` / `10.1300` / `10.1207` (Routledge / Haworth /
  Lawrence Erlbaum → Taylor & Francis, 24), `10.2190` (Baywood → Sage,
  7), and `10.1348` (British Psychological Society → Wiley, 2) to both
  the Wiley API source and its browser handler. A test now pins that the
  two Wiley prefix lists stay equal, since the handler is the fallback
  for exactly the DOIs the TDM route could not serve.

- **EBSCO's download budget was sized for typeset PDFs, not scans.** The
  signed-CDN fetch inherited `RequestHandler`'s 60 s timeout, which is
  right for a few hundred KB of typeset text and wrong for the population
  this handler exists to reach: EBSCO's pre-1997 back-file is page
  images. `10.1287/orsc.11.4.367.14601` (2000, 27 MB) timed out on one
  run and attached on the next — the failing attempt sustained under
  ~460 KB/s, so 60 s buys ~27 MB and the largest file we have met sits
  exactly on the line. Now `EbscoHandler.download_timeout_ms`, 240 s,
  which buys ~108 MB at that rate. Flat rather than size-aware on
  purpose: Playwright buffers the whole body before `resp.body()`
  returns, so there is no length to read and no stall to measure without
  replacing the fetch. Costs one of four lanes idling up to 4 min on a
  download that was going to fail anyway, and never the pass —
  `is_download_timeout` keeps this out of the outage breaker.

- **Declining the Connector erased a verdict another pass had earned.**
  Whatever the EBSCOhost pass fails to attach is handed to the Zotero
  Connector as a second chance. Five Connector pre-flights can then stop
  before trying anything, and each wrote one row per item saying why *the
  pass* stopped — so two items carrying real EBSCO verdicts (an
  unconfirmed no-match, and a positively located `unique_record`) landed
  in `pdf_attach_log.csv` as `connector_setup_failed`, whose advertised
  lever is "re-run the Connector pass". Read alone the row says the item
  was never looked at; the finding survived only in `pdf_fetch_log.csv`
  under `cause`, so the two logs disagreed and the run report printed the
  less informed one. The retry bucket now carries the earlier pass's
  answer on the item, and the bail-out row keeps it in `detail` —
  `status` stays honest about the Connector, which really did not run.
  Items reaching the queue fresh get no `detail`, because for them the
  status is the whole story.

## [0.13.0] — 2026-08-18

A retrieval release. Everything in it came out of running the pipeline
against real corpora — a 914-item run that recovered 161 PDFs, its
655-item residual, a 97-item Springer run — and most of it is either a
route the pipeline could not previously take, or a verdict it was
reaching without evidence.

### Added

- **An EBSCOhost handler, driven from the link resolver.** Not a publisher
  handler: EBSCOhost hosts many publishers, so nothing selects it by DOI
  or host — the resolver saying "your licensed route is EBSCOhost" does.
  It therefore stays out of `all_handlers()` alongside
  `ZoteroConnectorHandler`, and a unit test pins that, because leaking
  into the registry would offer it to Pass 1 where no resolver target
  exists and it could only fail.

  It matters because EBSCOhost is the platform Alma routes to most, and
  its holdings reach much further back than the publishers': from **1982**
  for the journals in one 97-item run, where FinELib SpringerLink starts
  at 1997. So it is the route to exactly the pre-1997 population the
  coverage guard diverts away from Springer.

  Retrieval, measured rather than assumed. Navigating the Alma
  `resolution_url` produces six redirects — EZproxy, EBSCO OpenURL, an
  OAuth handshake that succeeds on **institutional IP with no login** —
  and lands on a JS results page that is inert to any HTTP client: zero
  occurrences of "pdf", `__NEXT_DATA__` only. That page self-redirects to
  a single-article PDF viewer, which then fetches
  `research.ebsco.com/api/researcher-edge-aggregator/…/fulltext/pdf` for a
  signed URL and pulls the bytes from
  `content.ebscohost.com/cds/retrieve?content=<token>`.

  **That signed URL works from a plain HTTP client** — verified, no
  cookies, no session. So the handler uses the browser only to *observe*
  it and hands the download to `ctx.request`. It intercepts a response
  rather than clicking the viewer's Download button or awaiting a download
  event: the button works, but serialises everything through the page and
  leaves a file to locate. Nothing here is derivable from a DOI, so no URL
  template is possible.

  Likely more reliable than the Connector for this platform, for a reason
  `connector.py` already documents: EBSCO OpenURL links land on an
  "intermediate list page" where Zotero's translator shows a picker. This
  drives the viewer directly and never sees that page.

  Verified end to end on the three items a 97-item Springer run could not
  reach — all pre-1997, all previously logged `UNAVAILABLE` and then
  `connector_setup_failed`. All three now attach from EBSCOhost (741 KB,
  1,207 KB, 3,326 KB). Page counts are asserted, not just the `%PDF-`
  header, because a few hundred KB of `application/pdf` can still be a
  one-page preview.

- `pypdf` in the dev dependency group. The live page-count assertion sat
  behind an `importorskip` and silently never ran, hiding the exact
  preview trap it exists to catch — the same failure mode that put
  `reportlab`, `pybliometrics`, `wiley-tdm` and `playwright` in that group.

- **Parallel browser retrieval — `--browser-workers N`.** The browser
  passes drove one page at a time, which put an unattended EBSCOhost run
  of 401 items at roughly two hours. N lanes now share **one** persistent
  Chromium context, and that choice is the whole design: the profile
  directory holds the Cloudflare clearance and the institutional SSO /
  EZproxy session, Chromium locks that directory, so N separate browsers
  would mean N profiles and every one of those logins solved again. Tabs
  in one context inherit them, and Chromium already gives each tab its
  own renderer process, so the parallelism is real.

  Two ceilings, smaller wins: `--browser-workers` is the user's for the
  run, `handler.concurrency` is the publisher's. The latter was declared
  on every handler from the start and never read by any driver;
  `effective_lanes` now reads it, and a request the handler will not
  honour is reported rather than quietly reduced. **1 stays the default
  for every publisher-direct handler** — those modules record measured
  limits (Sage resets sessions above ~30 requests/minute; T&F and Wiley
  reject `ctx.request` outright), and N parallel requests from one IP is
  the shape bot detection looks for, where the cost of guessing is the
  publisher for the run *plus* the shared profile's clearance.
  `EbscoHandler` is raised to 4, on evidence: IP auth with no
  interstitial, most of its ~20 s per item spent waiting on a six-hop
  redirect and a JS boot, and the bytes fetched from a CDN rather than
  through the page. The Zotero Connector pass is pinned to 1 regardless —
  one Zotero desktop, one translator, a human per host.

  Three pieces of state that were locals in the serial loop are now
  shared through a `LaneCoordinator`: the Option-4 answer, the outage
  breaker's count, and whether the prompt has fired. It adds a gate with
  no serial counterpart — while a prompt is open every other lane parks
  *before* claiming its next item, so "skip the rest" cannot arrive after
  three more tabs have opened against a publisher just declined. Each
  lane gets its own handler instance, because `last_error` is per-
  download state and a shared one would let a lane read another's reason
  and file a lost connection as a missing article. On an outage the lanes
  stop claiming rather than being cancelled, so nothing is abandoned
  mid-download and un-attempted items stay unlogged, hence re-runnable.

- **`[library] openurl_base` takes a list — query several libraries and
  merge their routes.** A reader with two affiliations has two sets of
  entitlements, and neither institution's resolver knows the other's.
  The case that forced it: the configured Alma tenant returned no route
  at all for nine *Nursing Standard* articles, so the pipeline called
  them "no licensed route" and sent them to ILL — while the reader's
  second institution served the same journal through Journals@Ovid and
  ProQuest Central, full text one click away. Modelling one library made
  a second library's holdings unrepresentable, and the resulting verdict
  was confidently wrong. With both configured, all 26 previously
  unroutable items resolved.

  The first entry stays the primary: it keeps the existing cache keys
  and breaks ranking ties. `/setup` can now widen the scalar you already
  have into a list (`promote_scalar`), which matters because a
  `permissions.deny` rule blocks the Read tool on `config.toml`, so
  hand-editing is not a fallback an agent can offer.

- **"Your library has this publisher, but not this year" now works on
  Alma.** Three items in a 97-item run each burned a 30-second download
  timeout on a paywall; all three were pre-1997 articles whose journal
  Alma lists under SpringerLink from 1997. The resolver had the answer
  and nothing read it. That verdict used to be derived from an SFX query
  parameter Alma ignores; `resolvers/coverage.py` now parses Alma's
  per-package coverage statements (`Available from 01.01.1997 volume: 16
  issue: 1.`) into year windows instead. The grammar was sampled from 23
  real statements across six DOIs.

- **EBSCOhost re-queries by DOI when it answers with a search page.**
  Alma hands EBSCO an OpenURL carrying journal, year and title, and
  EBSCO turns that into `(SO <journal>)AND(DT <year>)AND(TI <title>)` —
  a query that can exclude the very article sitting in the database.
  Measured on `10.1287/mnsc.2017.2869`: Crossref and the DOI say 2017
  (online-first), EBSCO holds it as May 2019, so `DT 2017` returned zero
  and EBSCO fell back to fuzzy SmartText matching. `DI "<doi>"` returns
  exactly one record. On a seven-item live batch this took retrieval
  from 0 to 5.

  Results from SmartText are **never** used, and a result count on a
  page carrying it is never read as a count: those hits answer a
  different question from the one asked, and attaching the wrong paper
  to a citation is worse than attaching nothing. A DOI search returning
  more than one record is likewise left alone. Zero records is reported
  as what it is — the library's resolver advertising a route EBSCO
  cannot honour for this tenant — and never as "no full text exists".

### Fixed

- **A duplicate record no longer inherits its sibling's "done" status.**
  `enrich_abstracts.py` and `enrich_pdfs.py` both keyed their resume set
  on the DOI, so enriching one copy of an article permanently excluded
  every other copy. One real library held `10.1037/0882-7974.9.3.391`
  three times: two carried the abstract, the third did not, and the third
  was the copy consumers resolved to. The content was in the library and
  structurally invisible — 229 duplicate-DOI groups, roughly 298 items in
  that position.

  The DOI key bought nothing in exchange, which is what makes this a
  plain defect rather than a trade-off. An item whose update succeeded
  carries an `abstractNote` afterwards and an item whose PDF attached is
  caught by `pdf_map()`, so each was already excluded by its own per-item
  gate; the DOI key could only ever exclude *other* items. Both now key
  on `item_key`. `enrich_abstracts` additionally groups the work by DOI
  so duplicates share one cascade — three copies are three Zotero writes
  but a single lookup — and `enrich_pdfs` needs no equivalent, since its
  PDF cache is already keyed on the DOI and the sibling attaches from
  disk.

- **`needs_interactive_solve = False` now actually skips the setup
  prompt.** It only changed the queue message; the driver still called
  `setup()` unconditionally, so the EBSCOhost handler — which
  authenticates silently on institutional IP — opened a "can you see the
  PDF?" question with nothing to solve. Under `--control-file` that stalls
  an unattended run until the timeout. Found by running the handler for
  real, not by reading the code.

- **A Springer browser handler.** Springer contributed 0 of 98 Springer
  DOIs to a 914-item run, and the reason was not access.
  `link.springer.com/content/pdf/<doi>.pdf` answers any HTTP client with
  a byte-identical ~3 KB HTML page titled `Client Challenge` — an Imperva
  JavaScript interstitial. Measured from an on-campus IP
  (`*.aalto.fi`) across ten DOIs including *Journal of Business Ethics*,
  which the institution licenses, and unchanged by a complete browser
  header set. The challenge is served *before* entitlement is evaluated,
  so a VPN makes no difference, and Crossref's TDM record points at the
  same URL and fails identically.

  No Springer API fixes this: Meta/Metadata return metadata only, the
  Open Access API covers only OA content, and the TDM API returns
  full-text **XML** (its official client has `save_xml()` and no PDF
  method) behind a TDM agreement. So the fix is a real browser, where the
  JS runs and the challenge clears.

  `SpringerHandler` is a `PageNavigationHandler`. It was written as a
  `RequestHandler` first, on the theory that Playwright's request client
  inherits the browser context's cookies the way Sage and Emerald do for
  Cloudflare. Measured, and false: with the article page fully rendered
  and the PDF reachable by hand, `ctx.request.get()` still returned the
  3038-byte challenge. Imperva binds clearance to more than the cookie,
  so navigation plus a download event is the only route. Confirmed
  end-to-end afterwards — a 13-page, 982 KB *Journal of Business Ethics*
  PDF paginated 565–577, i.e. the version of record.

  `RequestHandler`'s failure diagnostics learned to name this: a 3038-byte
  Imperva page used to be reported as the useless `other (3038B)`, which
  read like a broken publisher rather than a bot wall.

  Two tests asserted the opposite premise and were corrected, not
  patched: `test_springer_doi_has_no_browser_handler` claimed "the 15
  Springer items really were unreachable", and the registry count was
  pinned at nine handlers.

- **`UNAVAILABLE` must be earned.** It is the one failure cause that
  licenses a full-text exclusion (FE6), and the pipeline was arriving at
  it by default from five different directions, in every case without
  having asked anyone about the article:

  - A lost network connection. One run dropped its network for four
    minutes and shredded 193 items at ~1.2 s each, recording every one
    as a failed fetch. Consecutive transport errors now stop the pass
    instead, leaving un-attempted items unlogged and re-runnable.
  - A publisher the plain-HTTP cascade structurally cannot reach —
    silence from a route that was never viable is not evidence.
  - A 60-second timeout downloading a PDF that turned out to be 27 MB
    and attached fine on the next attempt.
  - An article whose record had been positively located, and which
    failed only at the last hop to its viewer.
  - A "no exact match" page the pipeline had itself recorded as
    *unconfirmed*.

  Browser failures now classify from what actually happened, and only
  genuine silence falls through to the shared classifier. The timeout
  check is deliberately kept out of the transport-error list that trips
  the outage breaker, so a merely slow publisher cannot abort a queue.

- **The browser pass no longer opens a publisher the resolver says you
  cannot reach.** Pass 1 matched a handler by DOI prefix, asked the link
  resolver whether that publisher was worth opening, then opened it
  anyway in the case that mattered most. On Alma the platform table was
  silently doing double duty as the identity map, and it listed nine
  entries against ten handlers — so five handlers could never satisfy
  the guard at all.

- **Imported rows are no longer all `journalArticle`.** Scopus and Web
  of Science return book chapters too, and the row's `source` column —
  the book's title — went into `publicationTitle`, producing a journal
  article published in *The Judiciary, the Legislature and the EU
  Internal Market*. The cost is not cosmetic: a mis-typed chapter passes
  the journal-article filter, is routed to article-only PDF handlers and
  cannot succeed there. Five such items in one corpus each burned a
  browser slot to produce an unexplained failure, and would have done so
  again on every future run. The type now comes from Crossref, asked
  once per distinct DOI, under `--dry-run` too.

- **Two counts that described a different run than the one printed.**
  The skipped-key note counted attachment children as skipped keys, so
  it *grew as retrieval succeeded* — announcing 11 keys skipped
  immediately after attaching 11 PDFs, having skipped none. The
  end-of-run summary counted the whole cumulative log rather than this
  run's items, so a 14-item run that attached nothing announced "393 of
  14"; fixing that exposed a second bug underneath, where the summary
  re-read its own still-buffered log and reported "0 of 17" for a run
  that had attached 5.

- **The EBSCOhost login prompt asked the wrong question, then opened the
  wrong page.** It tested whether the *handler* needs an interactive
  solve rather than whether *this queue* does — so once a second library
  put EZproxy-wrapped routes into the mix, no login was ever offered and
  the items died silently on a SAML page. The fix then pointed the solve
  at a hook name `setup()` never calls, leaving the base implementation
  in charge and presenting a blank page to sign in on, which is worse
  than not prompting because the prompt looks answerable.

- **Parallel lanes no longer burst, or race each other through one
  login.** Each lane slept before its own download, which spaces that
  lane and nothing else: lanes started together sleep in lockstep and
  fire simultaneously — the exact shape a rate limiter looks for.
  Pacing is now reserved across the run. Separately, four lanes opening
  cold hit the institutional login at the same instant and each
  invalidated the others' session handshake, which a human cannot
  resolve; lanes now wait for the first to complete one item.

### Changed

- **SFX and Alma are now peer link-resolver dialects.** The resolver
  parsed both shapes but everything built on top of the parse assumed
  SFX's, and on Alma the difference was silent rather than loud. Measured
  live against an Alma tenant for a DOI with **15** licensed routes
  including EBSCOhost, JSTOR and three ProQuest packages:

  ```
  _effective_host(target)        -> <tenant>.alma.exlibrisgroup.com
  _platform_rank(target, …)      -> len(priority)   i.e. unranked
  required_domains=ebscohost.com -> no route found
  ```

  Every Alma `resolution_url` points at the Alma redirector, never a
  publisher, so any decision keyed on hostname was blind. `required_domains`
  returned the module's own documented "library has no licensed route"
  verdict for an article with fifteen, and `SFX_PLATFORM_PRIORITY` — the
  reasoned preference for EBSCOhost over JSTOR over ProQuest, which
  sometimes serves scanned images — did nothing at all there.

  `fetchers/resolvers/` now holds a `LibraryResolver` ABC with
  `SfxResolver` and `AlmaResolver` as equal implementations, selected by
  `[library] resolver` (`auto` by default) and registered most-specific
  first. The fix is not an Alma special case: targets became a structured
  `FulltextTarget` carrying the provider names Alma already sends
  (`package_public_name` / `interface_name`, previously discarded with the
  `<keys>` element), and **`rank_key` and `matches_domains` live on the
  base class and match host *or* name**. SFX keeps matching by domain,
  Alma starts matching by name, and a third dialect would get both free.

  Alma also declares `supports_date_threshold = False`, so `lookup_dual`
  makes **one** request instead of two and reports
  `date_filtering_available=False`. Live testing found Alma returns
  identical results for correct, wrong and absent `rft.date`/`rft.volume`,
  and `sfx.ignore_date_threshold` is an SFX parameter it ignores — so the
  second query bought nothing, and diffing two identical answers into a
  coverage verdict was worse than nothing. `enrich_pdfs.py` Pass 1 no
  longer attempts that classification when the dialect cannot support it.

  Verified live that Alma returns the same 15 services with and without
  the `sfx.*` parameters, so each dialect now sends only its own vendor
  namespace.

  `[library] platform_priority` is now implemented; it was documented in a
  comment but nothing had ever read it.

  Renamed with it, since every call site is in-repo: `SfxCache` →
  `ResolverCache`, `SFX_PLATFORM_PRIORITY` → `PLATFORM_PRIORITY`,
  `sfx_lookup_dual` → `lookup_dual`, `SfxDualResult` → `DualResult`,
  `sfx_target_url` → `resolver_target_url`. The cache file is
  `resolver_cache.json`; the old `sfx_cache.json` is ignored rather than
  migrated, because a bare URL list cannot answer the platform question
  and importing it would rank every entry as unranked indefinitely. Cost
  is one resolver round-trip per DOI on the first run after upgrading.

  Preserved deliberately, each having been a real incident: fail-open
  `query_ok` semantics, positive-only caching (an empty answer once
  turned a soft DOI-keying miss permanent for 15 articles the user could
  read), and `has_fulltext_access`.

  SFX parsing is now covered by inline-XML tests as well as the
  gitignored institution-specific fixtures. Those fixtures skip on a
  fresh checkout, so the dialect with weaker automated coverage was the
  one most likely to rot unnoticed — itself a form of unequal standing.

- **The PDF cascade is now ranked by version quality first, cost second
  — and it asks before spending.** The paid OpenAlex Content API used to
  be tried *first* inside a combined OpenAlex fetcher that itself sat
  ahead of Unpaywall, Semantic Scholar and CORE. Anyone with
  `OPENALEX_API_KEY` configured was therefore billed $0.01 per PDF for
  articles the free tiers would have served moments later, with no way to
  express a preference and nothing in the wizard that had ever asked
  whether to spend at all.

  `OpenAlexSource` is split in two. `openalex` is now the free OA
  metadata tier only; `openalex_content` is the paid Content API. That
  split is what lets the cascade state the priority properly:

  ```
  Stage 1  free version of record       ScienceDirect → Springer
                                        → Crossref TDM → PMC
  Stage 2  paid version of record       OpenAlex Content ($0.01, opt-in)
  Stage 3  open access, often author    OpenAlex OA → Unpaywall
           accepted manuscript          → Semantic Scholar → CORE
  Stage 4  browser handlers             APA, Sage, AOM, T&F, OUP, …
  Stage 5  Zotero Connector             via the library link resolver
  ```

  The paid tier ranks *above* the free open-access sources rather than
  last, which is the one place cost does not win: it serves the
  publisher's own file, so its pagination matches the published article,
  whereas the OA aggregators frequently hold an author manuscript whose
  page numbers do not. A cent is the right price for a citable version of
  record. It remains the only per-item cost anywhere in the sequence.

- **`/setup` now asks whether to spend on the OpenAlex Content API**, and
  explains why the API route is preferred over the browser at all: APIs
  are much faster and far less error prone, since nothing depends on a
  page layout, a Cloudflare challenge, or the user being at the keyboard.
  The question is skipped when no OpenAlex key is configured — asking
  about a tier that cannot run is noise.

  The opt-in is deliberately tri-state. An absent setting reads as
  enabled, so no existing install silently loses a working tier on
  upgrade; only an explicit `[openalex] use_paid_content_api = false`
  (or `OPENALEX_USE_PAID_CONTENT_API=off`) turns it off. The coercion
  that decides this is one shared function, because a plain `bool()` cast
  reads the string `"false"` as true.

  The switch also gates OpenAlex's *abstract* route, which goes through
  the same paid Content API for GROBID TEI XML — opting out means opting
  out everywhere, not just for PDFs.

- The retrieval sequence is documented for users in
  `skills/systematic-review/SKILL.md` and
  `skills/zotero-operations/SKILL.md`, with `fetchers.pdf_sources`'s
  docstring as the single authoritative ordering both point at.

## [0.12.0] — 2026-08-17

### Added

- **`gateway` — screen against your institution's own OpenAI-compatible
  LLM endpoint.** Many universities now run one: a single address
  serving open-weight models on hardware the institution already owns,
  usually at no per-paper cost to the researcher. Reaching it previously
  meant pointing `OPENAI_BASE_URL` at it, which worked but misreported
  everything downstream — the run showed as `openai`, borrowed OpenAI's
  tier hints for model IDs that carry a parameter count rather than a
  vendor's tier word, and priced the review at OpenAI's list rates.

  `gateway` is the first provider the plugin ships **no address** for,
  because "your university's endpoint" has no guessable value and
  inventing one would send a user's abstracts to a host they did not
  choose. That makes "selected but not yet configured" a normal starting
  state rather than an exotic one, so `ProviderSpec` gained a
  `byo_endpoint` flag and the three surfaces that build a URL — model
  listing, the health probe, and the client itself — now name
  `[gateway] base_url` immediately, instead of handing `urllib` the
  string `/v1/models` and retrying the resulting `ValueError` three
  times across eight seconds.

  **It is also the first provider with no environment variable.** Every
  other one's is an ecosystem convention its own SDK already reads, so
  naming it costs the user nothing; a gateway has no such convention,
  and any name invented here would collide with whatever a user already
  calls theirs. Both settings live in `config.toml` under `[gateway]`,
  and anyone who prefers an environment variable declares their own name
  via `[gateway] api_key_env` / `base_url_env`.

  **Cost is reported as unknown, not free.** The shipped catalogue
  carries no `[gateway]` section deliberately: an institution may
  recharge internally and the plugin cannot see that. "Free" is the one
  wrong answer that cannot be walked back after 5,000 abstracts.

- **Screen and code on a GPU cluster instead of an LLM API.** Screening
  has always required a provider that answers *while the script waits*.
  That rules out the cheapest compute many universities have: a GPU node
  behind a batch scheduler, where work is submitted and collected
  minutes or hours later. Both screening stages now split in two, and
  the halves are joined by a file rather than by a running process:

      abstract_screen.py --emit-manifest requests.jsonl
      ... executed anywhere ...
      abstract_screen.py --apply-responses responses.jsonl \
                         --manifest requests.jsonl

  A manifest is **self-contained**: every request carries its own
  rendered system and user message, the prompt's SHA-256, the prompt
  version, and (for coding) the frozen `coding_fields`. Whatever runs it
  needs no Zotero, no `screening_config.py`, no plugin checkout and no
  credential — and emit and apply need no LLM provider at all.

  Shipped alongside: `scripts/cluster/run_batch.py`, a vLLM runner that
  **imports nothing from this repository** — a cluster gets three files
  copied to it, never a clone, because `scripts` is an ordinary
  directory name that a site's own software stack very likely already
  owns and `PYTHONPATH` does not reliably settle the argument — plus
  `run_batch.sbatch`, a SLURM wrapper with no site-specific value in it.
  Everything a site needs to say about itself goes in a `SITE_ENV` shell
  snippet the user writes; `tests/unit/test_cluster_is_generic.py` fails
  the build if a hostname, partition, module or model ID is ever
  shipped.

  Two login-node checks come before any of it, and neither needs a GPU,
  a queue slot or an allocation: `--check-imports` answers "can this
  environment run vLLM at all?", and `--dry-run` answers "will this
  manifest run?". The first exists because the second imports nothing —
  which is what makes `--dry-run` free, and also what makes it blind to
  a module stack that cannot `import vllm`. Such a stack passes the
  pre-flight and then fails *inside* the allocation, after the queue
  wait, which is the most expensive moment to learn it. Importing vLLM
  needs no GPU, so the answer costs seconds where the user already is.

  The new **`cluster-screening` skill** covers the round trip, and the
  new `[cluster] automation` setting — `manual` (default) / `confirm` /
  `auto`, overridable per run — governs how much of it an assistant may
  drive. **The default is `manual`**, which prints the `ssh` / `sbatch`
  commands and stops: `confirm`'s safety depends on somebody being there
  to answer a permission prompt, and a level whose safety needs a TTY is
  not a safe default for a shared facility account the plugin does not
  own. `manual` is also the only level that works where reaching the
  cluster needs VPN, 2FA or Kerberos.

  **`confirm` is a claim about the permission system, not about this
  plugin**, so the plugin never allow-lists `ssh`, `scp`, `rsync` or
  `sbatch` — absent an allow rule the prompt *is* the approval, and a
  guard test fails the build if the wizard ever adds one.
  `check_cluster_config.py` reads the settings files and reports drift
  in both directions: `confirm` with an allow rule in place (one "don't
  ask again" click, possibly months ago, possibly in another project)
  confirms nothing, and `auto` without one prompts on every call, which
  in a headless session reads as a hang. `set_cluster_automation.py`
  writes the level and re-reports the effect through the same code, so
  the two cannot disagree.

  Four things stop a run rather than being worked around: a **degenerate
  run record** (mean output at or below two tokens — the failure that
  looks healthy, since every row reads `call_status=ok` and nothing is
  in any of them), a `run_id` mismatch, an unknown `schema_version`, and
  a coding manifest whose `coding_fields` no longer match the config.
  Over-long requests go to a `.skipped.json` sidecar as
  `too_long_for_context` at emit time and are refused by token count in
  the runner, rather than failing inside the serving stack in ways that
  read as model failure after the allocation is spent. The runner never
  retries and never writes placeholder text: a failed generation is
  recorded as one, the item stays untagged, and a re-run picks it up.

- **`[elsevier] render_xml_to_pdf` — Elsevier full-text recovery is now
  opt-in, and asked about in the setup wizard.** When ScienceDirect
  returns a first-page preview, the fetcher can pull the entitled XML
  body and render it to a text-only PDF. What that produces is a file
  the tool *generates* — no figures, no layout, tables flattened — and
  filing one of those in someone's Zotero library unasked is surprising:
  it sits next to real articles, looks like one, and nothing about a PDF
  icon says "reconstructed".

  **Default off.** When off, the XML endpoint is not called at all, so no
  Elsevier quota is spent retrieving text the run would then decline to
  write; a log line names the config key so the capability is
  discoverable rather than hidden. Recovered files keep their
  `-tdm-recovered` filename marker. The wizard defaults to no, preserves
  an existing choice across re-runs, and never flips the setting
  non-interactively. Only an explicit truthy token enables it, so a typo
  cannot switch it on.

### Fixed

- **APA PsycNET reported failure for PDFs it could reach, and in one case
  attached the wrong article.** A live 0.11.0 run failed both APA items
  with `Download button not found` on articles the operator could
  download by hand from the same browser profile. Three defects, of which
  the middle one is the serious kind: the accession number was read off
  the *previous* item's DOM, because `goto()` returns before the page's
  view swaps and the first record link on an article page is one of its
  **references** rather than the article. The run therefore fetched a
  cited paper's PDF and attached it to the right Zotero item — not a
  failed download but a **wrong PDF filed under a correct citation**,
  which nothing downstream checks and no error reports.

  Fixed structurally, by blanking the page before anything identifies the
  article, plus three independent agreement checks — the URL must belong
  to the requested DOI, the record link must carry the marker that
  distinguishes the resolved article from ones it cites, and RightsLink
  must agree, or the handler refuses rather than guesses. Alongside them,
  `CHECK ACCESS` no longer waits for a URL PsycNET stopped producing, and
  an entitled session's direct `/record/` route is accepted instead of
  costing a 20-second timeout each. The direct
  `/fulltext/{accession}.pdf` route is now preferred over the click
  chain, removing three of its four failure points.

  Two of the three were invisible to an unentitled session, and so to any
  test written against one — which is why the browser suite was green
  throughout. `playwright` is now declared in the dev dependency group
  for the same reason: every `live_browser` test sits behind an
  `importorskip`, so the whole suite reported success while exercising
  nothing.

- **Page-driven failures now say which page they stopped on.** Any
  browser handler failure records the final URL, title, screenshot and
  HTML under `<cache_dir>/diagnostics/`, and names the page in the
  console line. Previously every cause — wrong page, stale DOM, expired
  session, genuinely absent PDF — reported identically after about 135
  seconds, which is what made the PsycNET defects above take a live
  entitled session to find.

- **One failed download silently discarded a publisher's whole remaining
  queue under `--control-file`.** `_prompt_on_first_failure` decided whether
  it could ask by testing `sys.stdin.isatty()` — the exact coupling
  `fetchers.browser.interaction` exists to undo. Under an agent-driven run
  the user is present and answering other prompts in the conversation, but
  stdin is not a terminal, so this one returned `"skip"` without asking and
  dropped every remaining item for that publisher. Observed live: a
  `reinert_2025_sgr` APA article was never attempted, and the run log made
  it indistinguishable from an article no route existed for.

  The question now goes through the interaction channel like every other
  prompt — TTY, control file, or auto-skip. An explicit
  `--on-first-failure=<value>` still answers without asking, and a channel
  that genuinely cannot reach anyone still skips silently, so unattended
  runs are unchanged.

- **A working Zotero Connector was reported as "extension not found",
  aborting the whole browser stage.** Three defects compounded into one
  failure, in which `--stage browser` exited 0, attached zero PDFs, and
  logged `connector_extension_missing` on every row while the Connector
  sat installed and functioning.

  The wizard stored `[zotero_connector] extension_dir` as the extension's
  *version* subdirectory (`.../<ext-id>/5.0.200_0`). Chrome auto-updates
  the Connector and deletes the superseded folder, so the stored path
  went dead at the next update. `resolve_connector_extension_path()`
  then returned `None` for an unresolvable explicit path **without ever
  probing the platform defaults**, so one stale config value masked a
  perfectly good install one directory up. And the run report's remedy
  named the wrong place — `<cache-dir>/.chrome-profile-connector`, which
  is the Playwright profile the extension is loaded *into*, not where a
  user installs it — contradicting the correct hint printed moments
  earlier.

  Now: the wizard records the version-independent base folder and
  rewrites an older version-pinned value when setup is re-run, so the
  documented cure actually cures; an explicit path that does not resolve
  falls through to the platform defaults instead of failing; and the run
  report points at zotero.org/download/connectors. A resolvable explicit
  path still wins, and a genuinely absent extension still reports
  `None`.

- **Every `--user` run warned that Connector saves would land in the
  wrong library.** The Zotero Desktop library-selection pre-flight
  compared only `groupID`. A personal library never reports one, so
  `matched` could not become True under `--user`: the run printed
  "Zotero Desktop has 'My Library' selected, but the pipeline is working
  on 'group 5591' … every save will land in the wrong place" — on a
  correctly configured setup, where 5591 was the *user* id and My
  Library was exactly the target. Alarming, wrong, and it prompted for
  confirmation on the most common configuration there is.

  The check now branches on `library_type`: for a user target the
  absence of a group ID *is* the match, and a group being selected is
  the real mismatch. Messages render through `describe_library()`
  instead of hardcoding "group `<id>`". Group targets are unchanged,
  including the name-based fallback for older Zotero builds. Extracted
  as `library_selection_matches()` so it is testable rather than buried
  in an async driver; a user target is also no longer matched by a group
  that happens to share its number.

- **A `--filter-keys-file` run no longer enumerates the whole library.**
  `enrich_pdfs.py` fetched every journal article and then filtered in
  Python, so asking for 2,229 specific items on a ~10,000-item library
  cost a full paginated sweep — repeated on every backoff retry. A live
  run was rate-limited (HTTP 429, no `Retry-After`) *during that
  enumeration* and so never reached the retrieval it was invoked for; no
  amount of patience helped, because the walk came first.

  New `ZoteroClient.items_by_keys()` requests exactly the wanted keys in
  batches of 50, so the cost tracks the request rather than the size of
  the library. It also separates two diagnostics the old path conflated:
  a key that does not exist, and a key that resolved to a
  non-`journalArticle` item and was skipped by scope. Both used to print
  as "matched no journal article".

- **`attach_pdf` uploaded nothing when the PDF lived in a cache
  directory.** `attachment_simple` sets the attachment item's `filename`
  to the path it is handed, and the Zotero API rejects a stored-file
  filename containing a directory separator:

      400 Stored-file filename '/abs/path/to/x.pdf' cannot contain a
          directory path

  So the request failed at *item creation*, before a byte moved. pyzotero
  then hid the reason: `_create_prelim` discards the server's `failed`
  map, so the entry never gets a key, lands in the `failure` bucket, and
  the only symptom is a failure entry echoing the payload that was sent.
  A live run over 2,229 items downloaded PDFs correctly and attached
  **zero** of them, logging 38 `upload_failed` rows whose detail was the
  request body.

  `attach_pdf` now drives `Zupload` directly, sending the basename as
  `filename` and the directory as `basedir` — which is what that
  parameter exists for. The local file is still read from the full path;
  the server gets the bare name it requires. Verified against a live
  library.

  The retry tests moved onto the same seam. Mocking `attachment_simple`
  would now assert on a call that never happens — passing while leaving
  the upload path wholly unexercised.

### Added

- **`--remote` on every pipeline entry point.** Reads default to Zotero
  Desktop's local API because it is faster and unmetered, but items
  written through the Web API do not exist locally until the desktop
  client next syncs. A script that adds items and reads them back — or
  one handed a `--filter-keys-file` of freshly created keys — saw
  nothing and reported zero items to process, which reads as "already
  done" rather than "cannot see them yet". Found on a live run that had
  just written ~1,700 items. The flag lives on `add_library_args`, so
  every script gets it, and it overrides a caller's `prefer_local`
  keyword: whoever runs the script knows the client is behind, a
  compiled-in default cannot.

### Changed

- **`enrich_abstracts.py` no longer skips everything that is not a
  `journalArticle`.** A systematic review's included set routinely holds
  book chapters, reports and preprints, which are screened like any other
  record and need abstracts for the same reason. The old
  `journal_articles()` frame excluded them silently — on one live library
  55 items were never examined and nothing said so, which understates
  screening coverage rather than reporting a gap. New
  `ZoteroClient.abstractable_items()` queries the union of the nine
  abstract-bearing types in a single paginated sweep; `--item-types`
  narrows it, and `--item-types journalArticle` restores the old
  behaviour exactly.

### Fixed

- **`not_found` no longer means two different things in the abstract
  log.** `enrich_abstracts.py` wrote `status=not_found, source=none` for
  every item it came away from empty-handed — whether every source had
  answered and none held an abstract, every source had raised, or the
  item carried no DOI so nothing was ever asked. Only the first is
  absence. The exception text went to the terminal and never reached the
  CSV, so once a run finished there was no way to tell the three apart,
  and no way to recover the count afterwards.

  This matters wherever missingness is itself a result. A review
  reporting how many of its records genuinely have no abstract — and so
  cannot be screened by a human or by a model either — would have
  counted every timeout as a confirmed absence and overstated the
  figure.

  `_try_cascade` now returns a `CascadeResult` recording which sources
  answered and which raised, and absence is confirmed only when at least
  one source answered and none raised. The log gains `lookup_failed`
  (unknown, retry) and `no_doi` alongside a narrowed `not_found`
  (confirmed absent), plus a `detail` column carrying the per-source
  reason. `shared_orchestrators.open_log` migrates existing six-column
  logs on open, so older logs keep working — but their historical
  `not_found` rows retain the old, ambiguous meaning and should not be
  pooled with new ones.

## [0.11.0] — 2026-08-13

Five defects the first end-to-end runs on 0.10.0 exposed, four of them
the same shape: the pipeline knew something had gone wrong and named
something else. A missing PDF logged as a coding error. A throttled key
blamed on a missing key. A previous run's leftovers surfacing as a
missing file. In each case the report sent the operator somewhere the
problem was not — which is the failure mode 0.9.0 set out to remove from
PDF retrieval, showing up again in the stages either side of it.

### Changed

- **`no_pdf` is a decision, not a reason string.** "No PDF to read" and
  "coding blew up" were both logged as `decision=error` and told apart
  afterwards by matching the literal `reason` text, so the CSV disagreed
  with the summary printed beside it — one run showed `error: 0,
  no_pdf: 2` over two rows that both said `error` — and `verify`
  reported *"2 items still in error state"* for items whose only problem
  was a missing file. Both states remain equally unresolved: neither
  gets a stage tag, both still fail `verify`. Only the label changes,
  because the fix differs — go find the PDF, versus re-run the model.

  `templates/test_systematic_review.py` now names both states. Matching
  only `error` there would have let every missing-PDF item through the
  moment those stopped being logged as errors, inverting the guard this
  change was meant to leave intact. **Projects created before this
  release keep their copied template until it is regenerated**, and an
  old copy paired with a new pipeline is exactly the weakened-guard case
  above — regenerate it.
- **A throttled database no longer discards the ones that succeeded.** A
  Semantic Scholar 429 raised out of `source.run()` and killed the
  process, throwing away completed Scopus (156 rows), WoS (191) and
  OpenAlex (310) queries — and the API quota they had already cost —
  before anything reached disk. Failures are now collected per source,
  so every one is reported at once and sources after the failing one
  still run, with the paid-for rows written to
  `search_results_raw.partial.csv`. Still a hard failure, deliberately:
  a corpus assembled from a subset of its declared databases is not the
  corpus the protocol describes, so neither the dedup CSV nor
  `search_run.json` — whose DOI hash downstream stages read as proof of
  a complete search — is written.

### Fixed

- **The export stage no longer races Zotero's sync.**
  `export_coded_includes.py` selects on tags `fulltext_code.py` writes
  through the Web API, but reads them through a client that prefers the
  local Zotero server. Exporting straight after coding silently wrote a
  short CSV — 0 rows against a real include, with the identical export
  returning the row when re-run minutes later. `stage_code` already
  carried this wait for `abstract:*` tags; the `fulltext:*` half was
  missing. It waits on the include/exclude count from the log, not the
  number of coded items, since `error` and `no_pdf` stay untagged by
  design and waiting on them would hang until the timeout on every run
  with a missing PDF.
- **Semantic Scholar's rate-limit message no longer blames a working
  key.** It said *"Set SEMANTIC_SCHOLAR_API_KEY"* regardless of what the
  request carried — advice that, with a freshly rotated key the API had
  just accepted, sends the operator to `/setup` to rotate a working
  credential while the real answer is to wait or paginate less. The
  bulk endpoint throttles per key, and a broad block query can exhaust
  that on its own.
- **`mini_slr.py` refuses to start a new run in a dirty group.**
  `import_to_zotero.py` deduplicates by DOI, so a second run against a
  group still holding an earlier run's items creates nothing at all — it
  re-uses them, stage tags and all. One run lost a full pipeline that
  way: screening found every item already tagged and wrote no log, a
  stale `fulltext:include` inflated the export, and it surfaced as five
  `verify` failures that never mentioned the earlier run. The check runs
  before search spends anything, only for genuinely new runs, and hands
  over the teardown command for whichever run owns the offending items.

### Internal

- `pybliometrics` and `wiley-tdm` join the dev group. Both gate live
  tests behind `pytest.importorskip`, so without them installed the
  Scopus, ScienceDirect and Wiley TDM entitlement tests skipped silently
  while `test_live_coverage.py` still saw a live test covering each key
  — a real entitlement failure would have hidden among sixteen other
  skips. `enrich_pdfs.py` already declared `wiley-tdm` in its PEP 723
  block, so the pipeline could reach Wiley TDM while its test could not.

## [0.10.0] — 2026-08-13

Two arcs, both from the same 244-item review that drove 0.9.0. That
release made the pipeline *report* what it lost. This one changes what
it can reach, and who gets to decide: the model provider becomes a
choice instead of a hardcode, retrieval failures get a cause and a retry
set instead of a shrug, and the browser pass — the route that recovers
the most items — stops requiring the user to leave the conversation.

### Added

- **Provider-agnostic, tier-based model selection.** Plugin code no
  longer carries model versions. `scripts/core/providers.py` knows six
  providers (Anthropic, Google, OpenAI, OpenRouter, Ollama, LM Studio)
  and three tiers (`fast` / `balanced` / `deep`); concrete IDs are
  discovered from the provider's own model-list endpoint at project
  bootstrap and pinned into the project's `screening_config.py` with a
  provenance comment. `/setup` asks which provider **before** any key
  question and then asks for exactly one credential rather than four.
  New scripts: `resolve_models.py` (list and pin), `set_llm_provider.py`
  (switch provider from chat), `check_llm_provider.py` (yes/no probe, so
  a skill can ask without reading `config.toml`, which the Read tool is
  denied on). `templates/model_catalog.toml` is now the only place in
  the repo a version string or a price may live, and two AST guards keep
  it that way.

  **Closes #1** — local models. `ANTHROPIC_BASE_URL` routes screening at
  any Anthropic-compatible endpoint (Open WebUI, LM Studio), and Ollama
  and LM Studio are first-class providers with no API key at all rather
  than the old `"not-required-for-local-endpoint"` placeholder.
- **A cost estimate before the spend.** Both `--dry-run` paths now quote
  the projected cost from the real item count and a token estimate, and
  `/setup` shows unit prices with the arithmetic rather than a headline
  range. Prompt caching is deliberately *not* quoted: the screening and
  coding prompts are ~600 and ~800 tokens against 1024/4096-token
  minimum cacheable prefixes, so neither caches and a cached price would
  be a lie.
- **A retrieval diagnosis, per publisher.** `audit_zotero_library.py`
  now groups PDF failures by **publisher × cause** and writes retry sets
  as `.keys` files (`retry.browser[.<publisher>]`, `retry.ill`,
  `retry.network`, `true_negative`, `out_of_scope`) that feed straight
  into `--filter-keys-file`. This is the table a user had to rebuild by
  hand: of 119 apparent failures, 76 were Sage and Academy of Management
  articles one browser pass recovers.
- **A `BROWSER_REQUIRED` failure cause.** The DOI resolves to a
  publisher this plugin has a handler for, and that handler has not run.
  Its suggestion is not an FE code but a command. Previously these fell
  through to `UNAVAILABLE` → *"FE6, no fulltext available"*, which was
  wrong for 76 of 119 items.
- **Two new PDF sources.** Semantic Scholar's `openAccessPdf` (no new
  credential — the abstract half was already asking about the same DOIs
  and discarding the field), and **CORE** (`CORE_API_KEY`, free and
  self-service), which indexes institutional repositories and so reaches
  author-deposited copies of exactly the Cloudflare-gated articles that
  fail everywhere else. CORE is last in the cascade because it usually
  serves the accepted manuscript rather than the version of record;
  those attachments carry `pdf:repository-copy`.
- **Preprints, opt-in and tagged.** `--allow-preprints` looks for a copy
  on arXiv / SSRN / RePEc before an item is declared unavailable.
  Off by default and unreachable via `--sources`, because what it
  attaches is the manuscript before peer review: hypotheses, samples and
  findings all move between a working paper and the published article,
  and no later stage can see the substitution. Every attachment carries
  `pdf:preprint-version`, `fulltext_code.py` names those items before it
  codes them, and the audit lists them under `preprint_version`.
- **The agent can drive the browser pass.** `--control-file` publishes
  each prompt as JSON and waits for a reply file, so the questions
  travel through the conversation instead of a controlling terminal
  nobody has — the Chromium window still opens on the user's screen and
  the user still solves every challenge. `--auto-publishers` takes the
  item list from the audit's retry set instead of a hand-assembled key
  list. `--progress-json` appends one JSON object per line so a
  background run can be followed without parsing stdout.

### Changed

- **The browser pass stops asking when there is nothing to solve.**
  `setup()` polls for Cloudflare clearance — no challenge visible, plus
  a `cf_clearance` cookie scoped to *this* host — before falling back to
  prompting. The persistent profile means a repeat run usually has
  nothing to ask about, and self-clearing JS challenges pass in seconds.
  The probe can only ever answer "proceed": a timeout, an error, or a
  handler declaring a `setup_hint` (AoM's sign-in, Emerald's banner) all
  reach the user, because a wrong "proceed" costs one failed download
  before the existing failure prompt asks again with evidence, while a
  wrong "skip" would lose a publisher silently.
- **Diagnosis before exclusion is now mandatory in the skills.** An item
  may not be tagged `fulltext:unavailable` until the retrieval report
  gives its cause as `UNAVAILABLE`. The tag itself is new — an agent in
  a real run invented `fulltext-unavailable`, a spelling that exists
  nowhere in this repo, because the skill said "surface the residual
  list" and stopped. `zotero-operations` gained the full escalation
  ladder, and a guard test now fails the build if a skill names a tag or
  omits a cause the code can emit.
- **Model selection is proposed, never automatic.** An earlier pass in
  this arc auto-picked a model per tier from the provider's listing; it
  was deleted after live runs resolved OpenRouter's fast tier to a Batch
  API variant and Google's deep tier to a preview model whose date
  parsed as a version number. Fixing it meant substring blocklists that
  go stale exactly as fast as the hardcoded IDs this design exists to
  remove — and every caller is a skill, with an agent and a user
  present. `resolve_models.py` lists; the agent proposes; the user
  confirms.
- **Failure records keep every attempt.** `pdf_fetch_log` upserts by
  `(item_key, source)` rather than `item_key`, so a retry ladder can
  read its own history, and carries a `publisher` column joined from the
  DOI resolver cache. The browser and Connector paths write to it too —
  previously only the API cascade did, which is why `ACCESS_BLOCKED` was
  unreachable in practice.

### Fixed

- **An unbounded retry loop in the search stage.** `searchers/
  semantic_scholar.py` answered HTTP 429 with `time.sleep(5); continue`
  inside a `while True` — no cap, no jitter, no `Retry-After`. Against a
  throttling unauthenticated tier it spins forever. All searchers now
  route through `http_client.build_session()`, and an AST-based guard
  test fails the build on a new bare `requests.get` anywhere in
  `scripts/pipelines/`.
- **The setup wizard gave up on the first transient failure** during key
  verification. It now backs off exponentially with jitter, honouring
  `Retry-After` — in stdlib only, since `scripts/setup/` is invoked
  without `uv` and cannot import `requests`. A second guard test keeps
  that directory stdlib-only.
- **`--sources elsevier,pmc` exited 2** despite being advertised in both
  the docstring and `--help`; the fetchers are named `sciencedirect` and
  `pubmed_central`. Both spellings are now accepted.
- **`--sources browser,wiley` silently dropped `browser`** — the
  dispatch compared the parsed list against `["browser"]` exactly, so
  any extra name fell through to the API-only path and the browser pass
  never ran, with nothing said about it. Mixing the two is now rejected
  with an explanation.
- **Connector successes were re-queued every run** — `attached_via_
  connector` was missing from the resume status set.
- Two silent Better BibTeX defects, and `enrich_abstracts.py`'s docs
  omitted `wos` from the cascade they listed.

### Internal

- `screening_common.py` extracts the machinery `abstract_screen.py` and
  `fulltext_code.py` had in duplicate; `doi_utils.py` collapses three
  DOI normalisers into one strict form and one lenient one (the split is
  load-bearing — see its docstring); `scripts/setup/` has one definition
  of the zotero-mcp version floor; test dependencies live only in
  `pyproject.toml`'s `[dependency-groups] dev`, guarded by a test.
- CLAUDE.md now requires a paste-ready prompt when handing off to a new
  session, because a cold session otherwise re-derives what the previous
  one already settled.

## [0.9.0] — 2026-08-13

Driven by a real downstream session in which a 244-item review took four
rounds of user pushback to get from 125 to 223 usable full texts. Almost
everything recovered had been retrievable all along — the pipeline never
said so. One theme throughout: silent loss with no end-of-run account.

### Fixed

- **Successfully-downloaded PDFs were lost at the Zotero upload step.**
  `attach_pdf` had no retry and its exception text was printed then
  discarded, so 48 Sage PDFs fetched behind a solved Cloudflare
  challenge became dead ends while the files sat intact in
  `output/pdf_cache/`. Adds retry on 429/5xx/transport (never on a
  reported `failure` payload), a `detail` column carrying the reason on
  every non-success row, and rows in `pdf_fetch_log.csv` under a new
  `UPLOAD_FAILED` cause whose FE suggestion is explicitly *not* an
  exclusion. A tag PATCH after a good upload no longer records the item
  as `upload_failed`.
- **Nothing went back for cached-but-unattached PDFs.** A cache-recovery
  pass now runs before any fetching, and the DOI case-skew that could
  hide a cache hit between the API and browser paths is fixed.
- **Truncated downloads passed validation.** Every HTTP fetcher checked
  only `status_code == 200` and a `%PDF` prefix — which half a PDF
  satisfies. OpenAlex served five permanently-truncated files
  (byte-identical across retries; one declared its xref at offset
  1,744,085 in a 1,608,714-byte file) that were attached as clean
  successes. New `fetchers/_pdf_validate.py` checks `Content-Length`,
  the `%%EOF` trailer and the xref offset; all seven fetchers apply it
  to responses *and* cache reads, so a poisoned cache entry is discarded
  rather than served forever. A corrupt file is never attached —
  attaching one makes the item look permanently complete.
- **The library-resolver pre-flight failed closed**, making a transport
  blip indistinguishable from a real entitlement gap: 16 items were
  skipped against journals the user demonstrably had access to. It now
  fails open on unset / unreachable / unparseable responses, stops
  persisting negative verdicts to `sfx_cache.json`, and falls back to
  the DOI resolver URL. A genuinely empty response still gates.
- **`--no-prompt` did not do what its help text claimed** — it never
  implied `--on-first-failure=skip`, so unattended runs could still
  block on a TTY prompt.
- The no-terminal error suggested `--browser`, a flag that has never
  existed; pasting it failed at the moment the user was already stuck.
- `open_log` now migrates headerless and short-header run-logs instead
  of silently misaligning them.
- **`attach_pdf` accepted a zero-byte file.** Zotero stores it without
  complaint and the resulting attachment still carries an md5 — of
  nothing — which `pdf_map()` reads as "this item has a real PDF". The
  item was then marked complete and skipped by every future run while
  holding an empty attachment. Empty and unreadable files are now
  refused before upload. Found by the new live test.

### Added

- **An end-of-run report** (`pdf_run_report.py`). `--sources browser`
  previously printed no summary at all, and only 3 of 14 statuses were
  ever counted. Every run now ends with each status counted, per-item
  citations for anything still missing a PDF, and a concrete next lever
  per failure bucket. `--report` re-reads an existing log without
  fetching.
- **`--plan`** — prints the browser queue, naming which publishers will
  need an interactive Cloudflare/SSO solve, without opening a browser.
  Handlers declare `needs_interactive_solve`. Previously the queue never
  said which publishers needed a solve, so a user solved Sage and AoM
  and was never told APA was also queued (10 items, zero attempts).
- **`--ignore-library-coverage`** — bypass the resolver gate when it
  false-negatives on journals the library actually holds.
- **`--no-check-text`** — opt out of the post-attach text check. By
  default a structurally intact PDF yielding no text is reported as
  `attached_no_text` rather than counting as a clean success. Note this
  points at re-fetching from a different source, *not* at OCR: of the
  five textless files in the incident, zero were scans and all five came
  back intact from another source.

### Changed

- `scripts/dev/mini_slr.py`'s trim stage samples across all configured
  journals instead of top-N-by-year, which in a single-year corpus kept
  8 rows from one publisher and left the Elsevier and Wiley routes
  untested. Its `verify` stage now asserts the fetch→attach invariant:
  an item logged as attached must really carry a PDF in Zotero.

## [0.8.2] — 2026-08-12

### Added

- **Alma ISSN+date+volume fallback and setup-wizard support for the
  library resolver.** Follow-up to 0.8.1's Alma/Primo fix (BACKLOG.md
  P11). Some Alma deployments link holdings only at journal level and
  return zero `getFullTxt` matches on a DOI-only query even when they
  license the journal; `has_fulltext_access` / `sfx_lookup_dual` /
  `first_fulltext_target_preferred` now accept optional `issn`/
  `pub_date`/`volume` and retry once via `rft.issn`/`rft.date`/
  `rft.volume` when a DOI-keyed Alma query comes back empty.
  `enrich_pdfs.py` feeds these from the Zotero item's own metadata.
  Verified live against Aalto with a deliberately-engineered
  reproduction of the failure (a DOI Alma won't link, paired with a
  real licensed ISSN). Separately, `[library] openurl_base` now has a
  setup-wizard prompt (`LIBRARY_OPENURL_BASE`) with the SFX/Alma
  discovery guidance from 0.8.1's docstring update, and its env-var
  override — already expected by existing tests, never actually
  wired — now works.

## [0.8.1] — 2026-08-12

### Fixed

- **Ex Libris Alma/Primo support in the library-resolver pre-flight
  check.** `library_resolver.py` assumed an SFX-style OpenURL
  responder; Alma/Primo institutions (now the majority of academic
  libraries) got two silent failure modes instead of a working
  pre-flight — a missing `svc_dat=CTO` param made Alma serve its HTML
  discovery UI instead of XML (parse failure → fail-open → the check
  became a no-op), and even with valid Alma XML, the parser only
  recognized SFX's `<target>`/`<target_url>` shape, not Alma's
  `<context_service service_type="getFullTxt">`/`<resolution_url>`
  shape (→ confident false negatives, no fail-open safety net).
  `_build_query_url` and `_fulltext_target_urls` now detect and handle
  both shapes; verified against a live Alma instance in
  `tests/live/test_library_resolver_alma.py`. The ISSN+date+volume
  fallback some Alma deployments need (DOI-only queries return no
  matches there) is deferred — see BACKLOG.md P11.

## [0.8.0] — 2026-07-19

### Added

- **`zotero-cli` access tier.** Evaluated the standalone `zotero-cli`
  shipped by `zotero-mcp-server` v0.6.2 (the package the setup wizard
  already installs) as a way to simplify Zotero handling. Adopted for
  agent-initiated one-off writes MCP doesn't cover — documented as
  tier 2 in the Zotero access hierarchy in `zotero-operations/SKILL.md`
  and `systematic-review/SKILL.md`, with a read-only Bash allow-list
  and a setup-wizard PATH check (`_check_zotero_cli`) that also flags
  the stale PyPI `zotero-mcp` (0.1.6) package shadowing the real CLI.
  Rejected for batch pipelines — measured ~1.5–2 s per-call startup,
  no keyed batching, no `--json`, no 412 retry — so `zotero_io.py`
  remains the pipeline-facing layer unchanged. See BACKLOG.md
  House-keeping for the full evaluation writeup.
- **`pilot_analyze.py` wired into `systematic-review/SKILL.md`.** The
  script (year-cutoff, db-overlap, journal-coverage, field-breakdown
  subcommands) existed and was tested but undocumented; it's now in
  the pipeline-scripts table and the pilot-search narrative.

### Fixed

- **README.md** repo-layout diagram described `scripts/publishers/`
  (deleted in v0.6.0) and misdescribed `scripts/sources/` /
  `scripts/core/`; corrected to match the current tree and added
  `fetchers/`, `searchers/`, and `editorial-tools/`. Skill count
  corrected from "eight" to "nine" (plus the `verifying-citations`
  sub-skill).
- Stale in-code comments referencing scripts removed in v0.6.0
  (`fetch_abstracts.py`, `attach_pdfs.py`) in
  `scripts/pipelines/fetchers/__init__.py`,
  `scripts/pipelines/zotero_io.py`, and
  `scripts/pipelines/searchers/openalex.py`.
- Removed the orphaned one-off diagnostic
  `scripts/debug/inspect_scopus_abstract.py`.

## [0.7.0] — 2026-06-13

### Added

- **Setup wizard installs the Zotero MCP `[scite,semantic]` extras** so
  Scite retraction checks and semantic library search are available by
  default instead of silently absent (R9).
- **Antigravity (`agy`) MCP registration.** The wizard now also registers
  the plugin's MCP servers in `~/.gemini/config/mcp_config.json`, so they
  are available to Antigravity users, not just Claude Code.
- **ScienceDirect partial-entitlement handling.** Preview-only PDFs
  (Elsevier `x-els-status: WARNING`) are detected rather than cached as if
  complete; the full text is recovered via the XML endpoint, and the audit
  flags genuinely unrecoverable cases.
- **`abstract_screen.py --tag-batch-size`** batches `abstract:*` stage-tag
  writes into one multi-item PATCH per N decisions (default 50; `1` keeps
  strict per-item writes), cutting API calls and 412-retry pressure during
  steady-state screening.

### Changed

- **critic-loop** gains the Concession Threshold Protocol (a critic MAJOR
  may only be rejected with a verifiable refutation or a user-approved
  scope call), frame-lock detection, an explicit read-only constraint on
  critic subagents, and a Companion-skills section (R1–R3, S3).
- **manuscript-revision** now cross-links `academic-style` as the
  before-the-loop companion (S4).
- **Internal refactors (no behaviour change).** Shared enrich-orchestrator
  run-log helpers (`shared_orchestrators.py`), centralized log-CSV schemas
  (`log_schemas.py`), and a shared searcher credential resolver
  (`resolve_credential`) that replaces `wos.py`'s bare `KeyError` and
  unifies the optional/required key regimes (P1, P5, P7).

### Fixed (`fulltext_code.py --update-fields`, found in production use)

- **Update mode no longer re-adjudicates.** The update prompt now carries a
  no-readjudication override: previously, papers the model would re-decide
  as `exclude` (e.g., human-adjudicated includes) came back with empty
  strings for every coding field per the prompt's exclude rule, and the
  merge silently wrote those blanks into the note while keeping
  `decision=include`.
- **Error rows are no longer merged into notes.** A `_code_one` failure in
  update mode previously overwrote the error reason with
  `[UPDATE-FIELDS:...]` + the old reason and wrote a blank-fielded note;
  the error row is now surfaced as-is in the CSV and the note is left
  untouched for a clean retry.
- **LLM output budget scales with the coding schema**
  (`max(4000, 2000 + 400 * len(fields))`, previously hardcoded 3500,
  which large schemas would truncate).
- **Prevent CSV schema-widening crashes.** Added automatic CSV schema migration
  and pre-flight CSV validation prior to LLM worker execution to prevent API spend.
- **Fix `--update-fields` + `--only-keys` silent no-op.** The script now bypasses
  the normal-mode resume/early-exit calculations when `--update-fields` is requested.
- **Fix misleading dry-run counts in update mode.** A dedicated dry-run check inside
  the update block reports the correct update target counts and prompt override.
- **Prevent stacking reason prefixes.** The update mode now uses regex to strip any
  pre-existing `[UPDATE-FIELDS:...]` prefixes from the reason field before prepending
  the new one.
- **Enable stdout line buffering.** Added `sys.stdout.reconfigure(line_buffering=True)`
  at script startup to prevent invisible progress under piped output streams.

## [0.6.1] — 2026-06-11

### Selective coding updates (`--update-fields`)

`fulltext_code.py` gains a new `--update-fields FIELD1,FIELD2` flag for
updating an in-progress coding run without re-coding everything from scratch.
Two common scenarios it covers:

- **Add a new field mid-run.** After adding an entry to
  `FULLTEXT_CODING_FIELDS` in `screening_config.py`, run
  `--update-fields <new_field>` to populate that field across all
  already-included papers. Existing field values and the original screening
  decision are untouched.
- **Revise coding guidelines for specific fields.** After rewording a
  field's `description`, run `--update-fields <field>` to re-extract only
  those fields under the new prompt. Other fields (including any the
  adjudicator edited directly in Zotero) are preserved.

Behaviour: the flag selects items already tagged `fulltext:include`, calls
the LLM with the full current prompt, then merges only the named fields into
the existing `SLR Coding` child note. The `fulltext:*` tag and all other
coding fields are left unchanged. Items with no existing `SLR Coding` note
fall through to normal note creation. Combine with `--only-keys K1,K2,...`
to limit to a subset.

The existing `--full-recode` flag remains the right tool for major schema
overhauls where every field needs a fresh extraction.

Bump `FULLTEXT_CODING_PROMPT_VERSION` before invoking either flag so the
CSV log records which config version produced the update.

The `systematic-review` skill's *Revision during coding* section and
Pipeline-scripts table are updated to document both revision paths and when
to use each.

## [0.6.0] — 2026-06-10

### Removed — legacy pipeline scripts and `--legacy-browser`

One Playwright workflow, not two. A capability diff confirmed every
behaviour in the pre-v0.3.0 scripts has an equivalent (or better) in
the refactored path, so the rollback copies are gone:

- `scripts/pipelines/legacy/` (`attach_pdfs.py`, `fetch_abstracts.py`,
  `fetch_pdfs_browser.py`, `fetch_pdfs_wiley_tdm.py`) is deleted. The
  `enrich_*.py` orchestrators and `fetchers/` sources are the single
  workflow: Wiley TDM → `enrich_pdfs.py --sources wiley`, browser
  cascade → `enrich_pdfs.py --sources browser`, abstract cascade →
  `enrich_abstracts.py` (GROBID XML caching included).
- **BREAKING**: `enrich_pdfs.py --legacy-browser` (and the legacy-only
  `--publishers-json` override) no longer exist. Scripts that passed
  the flag now exit 2 (unrecognized argument); drop the flag.
- `scripts/publishers/registry.py` is deleted; the handler classes in
  `scripts/pipelines/fetchers/browser/` are the single publisher
  registry. The live-coverage guards now walk the
  `AbstractFetcher` / `PdfFetcher` subclass tree and
  `fetchers.browser.all_handlers()` instead of parsing legacy sources,
  and `tests/live/test_browser_publishers.py` drives the real async
  handler `setup()`/`download()` flows.

### Fixed — browser mode is installable again

- `enrich_pdfs.py` now declares `playwright>=1.40` in its PEP 723
  block. The dependency was lost in the v0.3 browser refactor (only
  the legacy script declared it), so the documented
  `uv run … --sources browser` invocation failed with a circular
  "run via uv run" error.
- Missing-playwright and missing-Chromium errors now print the actual
  remedy (`uvx playwright install chromium`); the wizard pre-approves
  the `uvx` install forms, and the `systematic-review` /
  `zotero-operations` skills document the one-time browser install.
- AAA handler: navigating the bare `/article-pdf/doi/{doi}` path
  returns Silverchair's "action has resulted in an error" page (the
  PDF URL embeds an opaque article ID). AAA now uses the same
  extract-the-PDF-href flow as OUP, generalised into a shared
  `PdfLinkNavigationHandler` base.
- Wiley browser handler: `/doi/pdf/` now lands on Wiley's e-reader
  viewer (an Open button, no download event); switched to
  `/doi/pdfdirect/`, the raw-PDF endpoint wiley-tdm uses.
- Six of the nine browser-publisher test DOIs in `KNOWN_DOIS` were
  unregistered placeholders; replaced with verified DOIs, and a new
  `tests/live/test_known_dois.py` guard checks every entry against
  the doi.org handle API on `-m live` runs.
- New `tests/live/test_zotero_connector.py` (`-m live_browser`)
  pre-flights the Connector fallback end-to-end: extension unpacked
  on disk, Zotero Desktop reachable, the extension's service worker
  booting inside the bundled Chromium, and a full Connector
  save→poll→sync→merge round-trip against an open-access article
  (defaults to My Library; stub item auto-deleted afterwards).
- `ZoteroClient.local` / `.cloud` hardcoded the "group" library type,
  so personal-library clients (`--user`, `for_user_library`) queried
  `/groups/<user_id>/` on both transports and failed. Both now follow
  `library_type`, and local personal-library reads use the `users/0`
  alias Zotero Desktop's API requires (the cloud user ID gets a 400
  locally).

## [0.5.0] — unreleased

### DOI validation and missing-DOI search

A new `scripts/pipelines/enrich_dois.py` closes the two common
metadata-quality gaps that block downstream enrichment: items with
no DOI (silently skipped by `enrich_abstracts.py` /
`enrich_pdfs.py`) and items whose DOI is broken or points to the
wrong paper (wastes cascade attempts, looks like a pipeline failure
when it's really bad metadata).

Two modes, combined by default:

- **`--validate`** — for every item that has a DOI, look it up on
  Crossref and compare the registered title / issue year / first-
  author surname against the Zotero record. Statuses:
  - `validate_ok` — title matches.
  - `validate_title_mismatch` — DOI points to a different paper.
  - `validate_not_in_crossref` — DOI doesn't resolve (broken DOI).
  - `validate_malformed_doi_fixed` (with `--fix-malformed`) — strips
    `https://doi.org/`, `doi:`, whitespace prefixes back to the
    canonical form.
  - `validate_skipped_no_zotero_title` — can't cross-check without
    a title.

- **`--find-missing`** — for every item without a DOI (or whose DOI
  failed validation in the same run), search Crossref by
  `title + first-author + year`. Score each candidate on three
  criteria: title match (via `_title_match.matches`, the existing
  normalised-prefix comparator), issued year within ±1, first-author
  surname case-insensitive equality. Auto-apply 3/3 matches; prompt
  on 2/3 (TTY only; `--no-prompt` skips); report
  `ambiguous_no_clear_match` / `not_found_in_crossref` otherwise.

- **`--all`** (default) — runs both. Items whose existing DOI fails
  validation feed directly into the find-missing queue. With
  `--replace-invalid`, 3/3 matches overwrite broken DOIs
  automatically; without the flag, they're logged as
  `proposed_replacement` for manual review (safer — a wrong
  replacement is worse than a broken DOI).

Write safety:

- Default: auto-apply only 3/3 matches.
- `--dry-run` blocks every write; still reports proposals in the
  CSV log.
- `--no-prompt` skips the 2/3 prompt (non-TTY stdin forces this too).
- `--replace-invalid` opt-in for overwriting existing invalid DOIs.

### Audit integration

`audit_zotero_library.py` gains a `missing_doi` count for
`journalArticle` items and emits an `audit.missing_doi.keys` file
alongside the existing `.missing_abstract.keys`, `.missing_pdf.keys`,
`.empty_stubs.keys`. Non-journal-article types (books, reports)
aren't flagged — they legitimately often lack DOIs. The audit's
"Next steps" output now suggests `enrich_dois.py --find-missing
--filter-keys-file <…>.missing_doi.keys` as the first stage when
missing DOIs exist.

The recommended workflow:

```
audit_zotero_library.py --group <id> --output /tmp/audit.json
enrich_dois.py   --group <id> --filter-keys-file /tmp/audit.missing_doi.keys
enrich_abstracts.py --group <id> --filter-keys-file /tmp/audit.missing_abstract.keys
enrich_pdfs.py --all --group <id> --filter-keys-file /tmp/audit.missing_pdf.keys
```

### New

- `scripts/pipelines/enrich_dois.py` (~500 LOC).
- `tests/unit/test_enrich_dois.py` — 32 tests covering normalisation,
  match scoring, validate / find-missing flows, dry-run,
  non-TTY, replacement with / without `--replace-invalid`.

### Changed

- `DoiResolution` (`fetchers/doi_resolver.py`) gains `title`,
  `author_surnames`, `issued_year` fields. `_extract_resolution`
  populates them from Crossref's `title[0]`, `author[*].family`,
  and `issued.date-parts[0][0]`. Legacy cache entries load cleanly
  with empty defaults for the new fields.
- `resolve_doi` no longer returns `None` when Crossref has the DOI
  but the URL field is empty — it returns a sparse `DoiResolution`
  so validation callers can still access the title. Callers that
  need URL (browser-pipeline routing) already check
  `resolution.url` at the callsite, so their behaviour is unchanged.
- `audit_zotero_library.py`: adds `missing_doi_count` / `missing_doi`
  to the JSON report and writes the corresponding `.keys` file.

### Manuscript-facing stats/tables rename and provenance enforcement

The per-project "stats producer" module and its pandas companion have
both been renamed to make their role explicit. `stats.py` → generic;
`manuscript_stats.py` names the consumer and distinguishes it from
any library statistics code. The JSON it writes and the tables module
follow suit so the three manuscript-facing artefacts read as a matched
set.

**Renames (downstream users need to rename their local copies):**

- `templates/stats.py` → `templates/manuscript_stats.py`.
  Your project's copy: `analysis/stats.py` → `analysis/manuscript_stats.py`.
- `templates/tables.py` → `templates/manuscript_tables.py`.
  Your project's copy: `manuscript/tables.py` → `manuscript/manuscript_tables.py`.
- Generated output: `analysis/results/stats.json` →
  `analysis/results/manuscript_stats.json`. Regenerate via
  `python3 analysis/manuscript_stats.py` after renaming the producer;
  the old JSON can be deleted.
- Manuscript `.qmd` imports update automatically when you re-copy
  `templates/manuscript.qmd`, or manually change `from stats import
  build_stats` to `from manuscript_stats import build_stats` and
  `from tables import …` to `from manuscript_tables import …`.

The function names stay (`build_stats()`, `tbl_methods()`, etc.) — no
API churn for downstream code that already imports them.

### Provenance enforcement for `manuscript_stats.json`

Four layers protect the new JSON from the hallucination attack surface
"Claude hand-edits the JSON to fix a missing key":

- **Skill rules** in `empirical-integrity/SKILL.md`: the *What is never
  acceptable* list and the *Red flags* list now ban Edit/Write on
  `analysis/results/manuscript_stats.json` and ban hardcoded literals
  inside `build_stats()`.
- **Ownership and lifecycle** subsection: makes explicit that
  `manuscript_stats.py` is project-owned, extended by the researcher,
  and that every value in the dict must trace to a pipeline artefact /
  file metadata / subprocess call.
- **Permission deny rules** in `.claude/settings.json`: Bootstrap step
  now merges idempotent `Write` / `Edit` deny rules for
  `//**/analysis/results/**` so the tool layer refuses direct edits.
- **Content-integrity test** in `test_empirical_integrity.py`: the new
  `test_stats_json_matches_build_stats` imports `build_stats()` live
  and diffs the result against the on-disk JSON. Replaces the previous
  mtime-based freshness check — catches both staleness and tampering.
  The inline-resolution test (`test_inline_stats_keys_resolve`) now
  also uses the live `build_stats()` return value, falling back to the
  JSON only when the producer module isn't importable.

### Zotero as ground truth for screening and coding

Screening decisions, coding fields, predatory-journal status, and
adjudication outcomes now live on the Zotero item — as tags and as a
structured child note — rather than only in CSV logs. The CSV logs
remain as run-history (who decided what, when, with which model and
prompt version), but **Zotero is the authoritative source**: the
manuscript, the export script, the regression tests, and the
adjudication UX all read Zotero, not the CSV.

This resolves a long-standing gap: `fulltext_code.py` previously
printed "Deferred to a later plugin release: automatic write-back of
tags and coded-field child notes to Zotero". That deferral is gone.

**Tag writes at screening time:**

- `abstract_screen.py` now writes `abstract:include` /
  `abstract:exclude` / `abstract:borderline` tags after every
  decision, and resumes from Zotero tags rather than the CSV log.
- `fulltext_code.py` now writes `fulltext:include` /
  `fulltext:exclude` tags after every decision (errors stay
  untagged so re-runs retry them), and resumes from Zotero tags.
  `--full-recode` clears the stage tag before re-processing.
- `import_to_zotero.py` runs the existing `sources/predatory.py`
  check at import time and adds a `predatory:flag` tag to items
  whose journal appears on Beall's list — the author sees the
  warning in Zotero. Flag, not exclude.
- Both screening scripts accept a new `--csv-backfill` flag that
  applies tags from existing CSV decisions without any LLM calls —
  the migration path for projects upgrading from the previous
  CSV-only pipeline.

**SLR Coding child note:**

- `fulltext_code.py` writes (or overwrites) an `SLR Coding` child
  note on every included paper. The note has a visible HTML body
  (decision, reason, each coding field as `<h2>` + `<p>`) that the
  adjudicator reads directly in Zotero, plus a trailing
  `<!-- SLR_CODING_DATA: {…} -->` comment carrying the same data as
  machine-parseable JSON for downstream scripts.

**`zotero_io.py` helpers:**

- `update_tags(item_key, add, remove, remove_prefixed)` — atomic
  tag add/remove in a single PATCH, with tenacity retry on HTTP 412.
  Supports stage-tag flips via `remove_prefixed=['abstract:']`.
- `get_tags(item_key)` — read tags for resume checks.
- `items_with_tag(collection, tag)` — enumerate tagged items in a
  collection; used by the export script.
- `upsert_child_note(parent_key, marker, note_html)` — create a new
  note or update an existing one identified by a content marker.
  The marker approach means re-runs don't create duplicate notes
  and the upsert never touches user-authored notes.
- `parse_slr_coding_note(note_html)` — extract the JSON payload
  from an SLR Coding note's comment block.

**`export_coded_includes.py` reads from Zotero:**

The script no longer takes `--log-csv`. It takes `--group` and
`--collection`, queries items tagged `fulltext:include`, fetches
each item's `SLR Coding` note, parses the JSON payload, and merges
it with bibliographic fields from the Zotero item (title, authors,
year, journal, DOI, Better BibTeX key from `extra`). Adjudication
flips propagate automatically because tags are authoritative;
exclusion-code corrections propagate because the note is
authoritative. Missing SLR Coding notes on tagged items are
surfaced as warnings, never silently dropped.

**Regression tests added to `templates/test_systematic_review.py`
(now 14 active tests, up from 12):**

- `fulltext tags consistent with CSV log` — drift check: every
  Zotero `fulltext:*` tag must have a matching CSV decision, and
  every CSV include/exclude must have a matching tag.
- `every fulltext:include item has SLR Coding note` — if an item
  is tagged include but lacks a coded note, the export pipeline
  has nothing to read.

**Skill update** — `skills/systematic-review/SKILL.md` gained a top-
level *Zotero tag and note conventions* section (the consolidated
catalogue of all tags and child notes), reworded the *Core
architecture* principles to name Zotero as ground truth, and
rewrote the adjudication loop so tag flips are atomic with decision
flips.

## [0.4.0] — unreleased

### Two-pass PDF retrieval: API cascade first, browser + Connector on residuals

The browser-mode pipeline is now explicitly a second pass. Pass 1
(the API cascade — Elsevier / Springer / Wiley TDM / Crossref TDM /
PMC / OpenAlex / Unpaywall) stays unchanged: fast, non-interactive,
no per-item DOI resolution. Pass 2 (new in v0.4.0) only processes
items Pass 1 couldn't attach, so DOI resolution costs scale with the
*residual* count rather than total library size.

Pass 2 routing:

1. Resolve the DOI via Crossref (habanero) — cached on disk in
   `<cache-dir>/doi_resolver_cache.json`. Catches prefix-drift
   cases like ETAP's `10.1111/etap.*` DOIs that now live on
   `journals.sagepub.com`.
2. If the resolved host matches an API source Pass 1 would have
   skipped by DOI prefix (Wiley TDM / Elsevier / Springer), retry
   that source once with `bypass_prefix_filter=True`. Catches
   journals that migrated onto one of the big TDM-capable
   publishers without changing their DOI prefix.
3. Otherwise pick the correct browser handler by matching the
   resolved URL host against each handler's `direct_access_domains`.
4. Fall through to the Zotero Connector (via SFX target) when no
   browser handler matches or the matched handler hits Case 2 /
   fails / is in `[library] no_access`.

New `--all` flag on `enrich_pdfs.py` runs both passes in one
invocation: Pass 1 → re-query `zot.pdf_map()` → Pass 2 on what's
left. Equivalent to running the two commands back-to-back, but in
one process. Mutually exclusive with `--sources`.

### Zotero Connector fallback for library-routed PDFs

When the library's SFX resolver offers a full-text route via a
third-party platform we don't have a bespoke handler for (EBSCOhost,
JSTOR, ProQuest, Project MUSE, …), the browser pipeline now delegates
to the Zotero Connector Chrome extension. One generic handler
(`scripts/pipelines/fetchers/browser/connector.py`) covers whatever
Zotero's community-maintained translators cover — no more
one-handler-per-platform maintenance tax.

**Three-pass routing model.**

For every DOI the browser pipeline now runs two SFX queries (default
date-filtered + `sfx.ignore_date_threshold=1`) and classifies each
item into one of three cases:

- **Case 3** — library covers this DOI on the direct publisher's
  domain: run the direct handler (Wiley, AoM, Sage, …).
- **Case 2** — library has the publisher but this DOI's year is out
  of coverage: skip the direct handler (it would paywall); route to
  the Connector via a Query-B target if one exists.
- **Case 1** — library has no relationship with this publisher at
  all: try the direct handler anyway (user might be an individual
  member, e.g. AoM); on failure, fall through to the Connector.

Items with no direct handler (MIS Quarterly, INFORMS without AoM-like
user subscriptions, …) go straight to the Connector upfront bucket.

**Learn-from-runtime failure prompt** replaces wizard enumeration of
publisher access. On the first per-item failure in a run the user
sees a three-way choice:

- `k` — keep trying the direct handler (failures still retry via
  the Connector at end of run);
- `s` — skip remaining direct attempts this run (default on Enter);
- `A` — always skip: appends the publisher to `[library] no_access`
  in `~/.config/academic-research/config.toml` so future runs jump
  straight to the Connector.

Non-TTY runs (CI / piped stdin) take `skip` automatically or obey
the new `--on-first-failure` flag.

**Dedup via vendored zotero-mcp algorithm.** The Connector saves as
a new Zotero item (it doesn't know about the existing DOI item).
`ZoteroClient.merge_duplicate_item` — ported ~60 LOC from
`zotero-mcp` (MIT-licensed, attribution in module docstring) — moves
children into the keeper, unions tags and collections, skips
duplicate attachments by `(contentType, filename, md5, url)`, and
trashes the duplicate parent via `PATCH {"deleted": 1}` (recoverable
from Zotero's Trash, not a permanent delete).

**Setup wizard additions.**

- Detects the Zotero Connector extension at the macOS / Linux /
  Windows Chrome default-profile paths and offers to use it. Install
  hint printed when the extension is absent.
- Shows the current `[library] no_access` list and lets the user
  remove entries (the "undo" path for the runtime "Always skip"
  answer).

### New

- `scripts/pipelines/fetchers/doi_resolver.py` — `resolve_doi(doi, *, crossref, cache)` via habanero.Crossref plus an on-disk `DoiResolverCache`. Called only in Pass 2; never in the API cascade.
- `scripts/pipelines/fetchers/browser/connector.py` — `ZoteroConnectorHandler`
  (Playwright + extension service-worker-eval path, per the POC in
  `temp/open_zotero_browser.py`).
- `scripts/core/config_writer.py` — safe `append_to_list` /
  `remove_from_list` helpers. Used by the failure-prompt "Always
  skip" path and by the wizard's `no_access` editor; preserves mode
  `0600` and the wizard's manual TOML format.
- `ZoteroClient.merge_duplicate_item(target_key, duplicate_key)`.
- `fetchers.library_resolver.sfx_lookup_dual()` and
  `first_fulltext_target_preferred()` with a `SFX_PLATFORM_PRIORITY`
  ranking (EBSCOhost > publisher-direct > JSTOR > ProQuest).
- `PublisherHandler.attaches_directly: bool` — when True, the
  driver calls `download_and_attach(page, ctx, service_worker,
  item, zot, …)` instead of the standard `download()` +
  `zot.attach_pdf()` pipeline.
- `enrich_pdfs.py --sources connector` — Connector-only mode for
  targeted validation runs.
- `enrich_pdfs.py --all` — runs Pass 1 (API cascade) then Pass 2
  (browser + Connector) on residuals in one invocation.
- `enrich_pdfs.py --on-first-failure=keep|skip|always_skip` —
  non-interactive answer for the failure prompt.
- `PdfFetcher.direct_access_domains` class attribute + `bypass_prefix_filter`
  kwarg on `fetch_pdf`. Wiley TDM / Elsevier / Springer declare their
  hosts; Pass 2 uses the flag to invoke them on DOIs whose prefix
  Pass 1 skipped.
- `resolve_by_host(host, handlers)` helper in `fetchers.browser`
  mirroring `resolve_by_doi` but matching on `direct_access_domains`.
- `[zotero_connector] extension_dir` config key; override via
  `ZOTERO_CONNECTOR_DIR` env var.
- `[library] no_access` TOML list config key; wizard-editable at
  setup time, runtime-appendable via the failure prompt.

### Changed

- `fetchers.library_resolver.SfxCache` value shape is now
  `{"urls": [target URLs]}` — raw target list is stored once per
  `(doi, ignore_date_threshold)` and filtered per-caller. Legacy
  `{has_access, targets}` entries from v0.3.x are treated as a
  cache miss and re-queried on the next run (one-time cost).
- `setup/wizard.py:_write_config` now emits TOML list values
  (`no_access = ["aom", "apa"]`) in addition to quoted strings.
  Existing runs are unaffected — the format for scalar keys is
  unchanged.
- `launch_context()` gains an `extensions=[...]` keyword argument
  that maps to Chromium's `--load-extension` + isolates the
  Connector profile from the direct-handler profile.

## [0.3.1] — 2026-04-22

### Move legacy orchestrators under `legacy/` subdirectory

The four pre-v0.3.0 orchestrators that v0.3.0 deliberately retained
as a rollback path are now under `scripts/pipelines/legacy/`:

- `legacy/attach_pdfs.py`
- `legacy/fetch_abstracts.py`
- `legacy/fetch_pdfs_browser.py`
- `legacy/fetch_pdfs_wiley_tdm.py`

Plus `legacy/README.md` documenting the deletion checklist for the
next release.

### Fixed

- The moved scripts add `scripts/pipelines/` to `sys.path` at module
  load so `import zotero_io` still resolves (zotero_io lives one
  level up now). `fetch_pdfs_browser.py` also walks two levels up
  for `SCRIPTS_ROOT` (for the `publishers.registry` import).
- `enrich_pdfs.py --legacy-browser` subprocess path updated to
  `legacy/fetch_pdfs_browser.py`.
- `tests/unit/test_live_coverage.py` reads the source files from
  their new `legacy/` path; the guard still enforces live-test
  coverage for every `fetch_from_*` / `fetch_*_pdf` function in the
  legacy cascade.
- `tests/live/test_browser_publishers.py` loads the legacy fetcher
  from its new path and adds the legacy dir to `sys.path` before
  importing (so the sibling `import attach_pdfs` resolves).
- `audit_zotero_library.py` "next steps" output now suggests the
  refactored `enrich_abstracts.py` / `enrich_pdfs.py` rather than
  the legacy scripts.

## [0.3.0] — 2026-04-22

### Pipeline refactor: pyzotero-backed Zotero I/O, per-provider fetcher classes, library-aware browser flow

Multi-week refactor of the `scripts/pipelines/` tree. The
pre-refactor structure mixed Zotero upload logic, custom HTTP
retry, and per-publisher download flows inside four monolithic
scripts (`attach_pdfs.py`, `fetch_abstracts.py`, `fetch_pdfs_wiley_tdm.py`,
`fetch_pdfs_browser.py`). This release replaces that with:

**New modules.**

- `scripts/pipelines/zotero_io.py` — `ZoteroClient` wrapping
  `pyzotero`. Every script that touches Zotero now routes through it.
  Deletes ~110 lines of custom 3-step S3 upload + manual
  `If-Unmodified-Since-Version` PATCH code; `pyzotero.attachment_simple()`
  and `pyzotero.update_item()` already did this. `@retry` on
  `update_abstract()` re-fetches the item's latest version on HTTP 412
  and re-applies — covers the previously-unhandled version-conflict case.
- `scripts/pipelines/http_client.py` — shared `requests.Session` with
  `urllib3.Retry` (429 / 5xx, exponential backoff) and `tenacity`
  wrappers on `get_json` / `get_bytes`. Replaces hand-rolled `urllib`
  wrappers and ad-hoc `time.sleep(30) + recursion` retries.
- `scripts/pipelines/fetchers/` — one class per provider implementing
  the `AbstractFetcher` / `PdfFetcher` ABC pair. A provider that
  serves both capabilities (Crossref, OpenAlex, ScienceDirect)
  inherits both. Nine abstract-fetchers and eleven PDF-fetchers total,
  each in its own file with live tests.
- `scripts/pipelines/fetchers/wos.py` — new abstract fetcher using the
  WoS Expanded API with a title-search fallback for DOI aliases
  (e.g. AoM Annals `10.5465/…` that WoS indexes under its original
  Routledge/T&F `10.1080/…` prefix). Recovers 2 of 7 abstracts that the
  prior cascade couldn't find on a test library.

**New `fetchers/browser/` sub-package.**

- Nine `PublisherHandler` subclasses (aaa, aom, apa, emerald, informs,
  oup, sage, tandf, wiley) with two intermediate bases
  (`RequestHandler` for sessions that `ctx.request.get()` can use;
  `PageNavigationHandler` for publishers whose Cloudflare rejects
  non-browser requests). Three custom flows ported from the
  SLR-motivation project's working code: INFORMS's epdf→pdfdirect
  rewrite, OUP's JS-extracted PDF href, APA PsycNET's multi-step
  click-through.
- `setup_url_template` per handler — landing-page URL opened during
  the browser-setup phase, distinct from the download URL. Fixes an
  observed bug where opening a `?download=true` PDF URL triggered
  Chromium to auto-download to the profile's download dir and stranded
  the user at `about:blank` before they could solve Cloudflare.
- SFX / OpenURL pre-flight (`library_resolver.py`). When
  `[library] openurl_base` is set in `config.toml`, each DOI is
  checked against the library's link resolver before the browser
  handler runs. Targets are filtered by the handler's
  `direct_access_domains` — a JSTOR / EBSCOhost / ProQuest route
  reported by SFX doesn't count as accessible if our handler only
  knows the direct-publisher URL. Eliminates ~30s-per-item timeouts
  on inaccessible DOIs and surfaces the skip in the CSV log.
- On-disk SFX cache keyed by `(doi, handler-domain-set)` so
  re-running is instant and two handlers querying the same DOI with
  different domain filters don't collide.

**New orchestrators.**

- `scripts/pipelines/enrich_abstracts.py` — replaces
  `fetch_abstracts.py`. Drives the abstract-fetcher cascade
  (Crossref → Semantic Scholar → Scopus → WoS → ScienceDirect →
  OpenAlex GROBID) through a `ThreadPoolExecutor`.
- `scripts/pipelines/enrich_pdfs.py` — replaces `attach_pdfs.py`
  plus the two `fetch_pdfs_*.py` fallbacks. Automated cascade by
  default; `--sources wiley` routes to the Wiley TDM handler;
  `--sources browser` drives the per-publisher browser handlers
  in-process (no more subprocess shell-out). `--legacy-browser`
  keeps the old subprocess path available for rollback.

**Setup-wizard improvements.**

- Per-tier MCP-server check with install / homepage hints for each
  expected server (zotero, openalex, semantic-scholar, scopus,
  paper-search). Wizard offers to run `claude mcp add` after
  confirming the binary's available on PATH.
- Local-Zotero probe against `http://localhost:23119/api/` — prints
  actionable instructions if Zotero desktop isn't running or the
  Better BibTeX local HTTP server isn't enabled.

**UX in browser flow.**

- Setup banner now says "Google Chrome for Testing" (the actual window
  title Playwright produces on macOS); removed the undefined
  "Playwright" jargon in favour of "a separate automated browser used
  only by this script".
- Per-publisher `setup_hint` explaining what institutional access /
  sign-in is needed (AoM's two-gate login, Wiley's Shibboleth flow,
  etc.).
- Yes/no prompt at the end of the setup banner: `y` to proceed, `n`
  to skip the publisher entirely (all items logged as
  `skipped_no_access`, no 30s download timeouts).
- pyzotero's `WheneverDeprecationWarning` silenced at the `zotero_io`
  import level — library-internal, benign, was burying real output.

**Migrated scripts.** Every Zotero-touching script (`abstract_screen`,
`audit_zotero_library`, `fulltext_code`, `import_to_zotero`, plus
the legacy `attach_pdfs`/`fetch_abstracts`/`fetch_pdfs_*`) now uses
`ZoteroClient`. The legacy top-level scripts remain on disk during
this release cycle as a rollback path; next release deletes them
once the new orchestrators have proven themselves on production
libraries.

**Fixed.**

- `ZoteroClient.attach_pdf`: pyzotero's `attachment_simple()` returns
  `{"success": [...], "failure": [...], "unchanged": [...]}` where all
  three are lists of item dicts, not dicts keyed by integer index.
  The first version of the wrapper matched the wrong shape (tests
  mocked the wrong shape too), and crashed on real uploads with
  `'list' object has no attribute 'values'`. Fixed both the wrapper
  and the tests.
- DOI-alias handling in WoS title fallback: a 100-char-truncated
  quoted WoS query silently returned 0 hits (quoted phrase searches
  require exact match). Switched to unquoted `TI=(…)` keyword-AND
  search with a Python-side title normaliser for precision.
- `enrich_pdfs.py --sources browser` opened the PDF URL directly,
  triggering Chromium to auto-download and strand the user at
  about:blank. Every handler with `?download=true` in its URL
  template now has a distinct `setup_url_template` pointing at the
  article landing page.

**New tests.** 219 unit tests pass (from 72 pre-refactor). Coverage
added for the Zotero wrapper, HTTP client, fetcher ABCs, each
provider fetcher, the browser handlers (registry + URL
regressions), and the SFX resolver (parser, domain filter, cache).

## [0.2.4] — 2026-04-19

### test_suite.py template: realign with refactored screening pipeline

The screening scripts no longer define `MODEL` / `PROMPT_VERSION` as
module-level constants — they read the values from the project's
`screening_config.py` via `getattr(mod, "ABSTRACT_SCREENING_MODEL", …)`.
The template's old `test_screening_script_constants_in_log` grepped the
scripts for `^MODEL = "..."`, found nothing, and silently no-op'd.
Drift was invisible.

Fixed in `templates/test_suite.py`:

- New `SCREENING_CONFIG` path constant pointing at the project's
  `screening_config.py` (the canonical source of model + prompt-version
  declarations).
- Renamed `test_screening_script_constants_in_log` →
  `test_screening_config_constants_in_log`. Now greps
  `screening_config.py` for the four `FULLTEXT_CODING_*` /
  `ABSTRACT_SCREENING_*` constants and verifies each log's model /
  prompt_version set is a subset (subset, not equality — so an
  in-progress re-run mid-transition isn't a false alarm).
- `test_temperature_zero_pinned` kept as-is but reworded: silently
  passes when neither script is locally copied, since the plugin's own
  test suite enforces the invariant for plugin-invoked scripts.

Did not add a `VersionConflictError` leakage test — the error is
tenacity-retried internally and does not surface in screening CSV rows
in practice, so it would be speculative.

## [0.2.3] — 2026-04-19

### Manuscript scaffold maturity + `_tables.py` → `tables.py` rename

The manuscript templates are the public API a user's Quarto
manuscript imports from. Leading the file with an underscore
implied "private / implementation detail" — the opposite of intent.
Dropped the underscore and grew the scaffold to cover two common
gaps: PRISMA flow and construct-family grouping.

Templates:

- **`templates/_tables.py` → `templates/tables.py`** (git rename,
  history preserved). The Quarto manuscript imports `from tables
  import …`, so the file name should match the import. All cross-refs
  updated (skill + manuscript).
- **`templates/tables.py`** — added `tbl_construct_families(stats)`
  helper that reads `coding.family.<slug>` keys from `build_stats()`
  output and returns a sorted DataFrame. Empty DataFrame when no
  families configured, so the manuscript can fall back to a
  placeholder comment cleanly.
- **`templates/manuscript.qmd`** — new PRISMA Mermaid code chunk in
  the Methods section, driven entirely by inline `s[...]` lookups
  (no hand-typed counts). New `tbl-families` chunk in Findings that
  renders `tbl_construct_families(s)` when configured, otherwise
  emits a placeholder HTML comment. Updated all imports to `from
  tables import …`.
- **`templates/stats.py`** — expanded the `CONSTRUCT_FAMILIES` comment
  block into a proper worked example explaining how the field name,
  rule tuples, and downstream `tbl_construct_families()` fit together.
  The list still ships empty (feature is opt-in).

Skill update: `systematic-review/SKILL.md` points at `tables.py`
instead of `_tables.py`, and notes the copy-into-project step so
the `.qmd`'s `from tables import …` resolves.

## [0.2.2] — 2026-04-21

### `searchers/` package — one ABC, four implementations

Extracted the per-database search logic from `search.py` /
`search_openalex.py` into a clean abstract-base-class package so
that (a) adding a new database is writing one small file, and (b)
the orchestrator's database loop becomes data-driven.

Mirrors the `fetchers/` package pattern (fetchers = retrieve content
for a known DOI; searchers = discover DOIs matching a query) without
overlapping it.

New package `scripts/pipelines/searchers/`:

- **`base.py`** — `SearchSource` ABC with `name`, `supports_journal_scope`,
  `supports_block_queries` attributes; `run(config, ctx)` method;
  `credentials_error(ctx)` hook. `SearchContext` dataclass carries
  year window, ISSN list, mailto. Common `SEARCH_ROW_FIELDS` schema
  that harmonises Scopus / WoS / OpenAlex / Semantic Scholar outputs
  (union of per-source identifiers + OA metadata where available).
- **`scopus.py`** — `ScopusSearch` using `pybliometrics`.
  `supports_journal_scope=True`, not block queries. Credentials:
  either `~/.config/pybliometrics.cfg` or `SCOPUS_API_KEY`.
- **`wos.py`** — `WosSearch` against the Expanded API with 100-row
  paging. Requires `WOS_API_KEY_EXTENDED` (Starter tier does not
  support `IS=` so is not a substitute). `supports_journal_scope=True`.
- **`openalex.py`** — `OpenAlexSearch` with the block-query pattern
  (run Block A and Block B separately, merge) that preserves recall
  against OpenAlex's relevance-ranked `search=`. Free tier; no key.
  `supports_block_queries=True`.
- **`semantic_scholar.py`** — `SemanticScholarSearch` via the
  graph-API bulk endpoint. New database in the plugin. Same block
  pattern as OpenAlex. `supports_journal_scope=False` — S2 doesn't
  reliably filter server-side, so the source post-filters
  client-side against `ctx.issns`. API key optional but strongly
  recommended (unauthenticated tier is 1 rps shared globally).

Refactored:

- **`search.py`** now reads the registry, picks sources that pass
  `credentials_error()` by default (or respects `--databases scopus,wos`),
  and dispatches each source's `run()`. Existing dedup + metadata +
  integrity-gatekeeper logic unchanged. ~100 lines shorter net.
- **`search_openalex.py`** reduces to a thin shim that re-dispatches
  to `search.py --databases openalex`.

New thin single-DB wrappers for piloting:
`search_scopus.py`, `search_wos.py`, `search_semantic_scholar.py`
(each ≈30 lines, each delegates to `search.py --databases <name>`).

Tests: `tests/unit/test_searchers_base.py` (17 tests) — ABC cannot
be instantiated; every registered source declares `name`,
`supports_*` flags; registry returns fresh instances; empty-row
schema invariants; per-source credential checks.

Skill update: `systematic-review` stage table lists every search
entry point.

165 → 182 default tests. Ruff clean on my additions.

## [0.2.1] — 2026-04-21

### systematic-review skill: QA-evaluator pattern fully documented

The skill already mentioned the three-agent QA step in passing. This
release writes out the full protocol that the reference SLR project
uses, so the plugin can drive the QA loop end-to-end without a
separate external reference.

Added to `skills/systematic-review/SKILL.md`:

- **Three evaluator sketches** (inclusion validator, exclusion
  validator, coding-quality validator) with each evaluator's
  sampling strategy, prompt focus, and severity scheme. Includes the
  default 20 % coding-quality spot-check threshold with tuning
  guidance for smaller / larger corpora.
- **Tag-vocabulary table** listing all seven `qa-*` tags, when each
  is applied, and when each is removed. Closes the ambiguity around
  whether `fulltext:include` / `fulltext:exclude` move alongside
  `qa-adjudicated-*` (they do not — screener verdict vs. reviewer
  process trail are separate records).
- **Human adjudication loop** as a six-step procedure with the
  last-row-wins CSV append pattern for flips, plus the separate
  `qa-wrong-code` path for code corrections that don't flip the
  decision.
- **`screening/qa_review.md` structure** with both sections
  (Scope clarifications + Adjudication log) and the exact line
  format for each. Cross-references the existing example line.
- **Red flag** against silently dropping a `qa-flag`ed item without
  recording a disposition.

No code changes; prose-only. Default tests unchanged (165 pass
today — the other instance's refactor has already added new tests).

## [0.2.0] — 2026-04-21

### Manuscript scaffold — Milestone G, and plugin-v0.2 milestone

Ships the last missing piece of the end-to-end SLR pipeline: the
manuscript scaffold. With this release, a project can go from search
results to a rendered manuscript using only plugin-shipped artifacts
plus the per-project config files.

New templates:

- **`templates/stats.py`** — flat-dict builder that reads every
  pipeline output (`search_metadata.json`, `search_run.json`, the
  two screening CSVs, `coded_papers.csv`) and returns keys like
  `search.unique_dois`, `screen.abstract.n_include`,
  `screen.n_included`, `provenance.fulltext.model`,
  `provenance.fulltext.prompt_version`. Flat dotted keys fail loudly
  on typos in the manuscript, which is the whole point. Also
  demonstrates an optional regex-based family classifier for free-text
  coding fields.
- **`templates/_tables.py`** — pandas-based table helpers that turn
  `coded_papers.csv` into publication-ready tables (methods,
  geographic regions, exclusion reasons, included-papers list).
  Keeps Quarto chunks one-liners.
- **`templates/manuscript.qmd`** — Quarto scaffold with a setup
  chunk importing `build_stats()`, placeholder sections (introduction,
  methods, findings, discussion, limitations, references), and
  example inline expressions showing every methodology number wired
  to `s['key']` rather than hand-typed. The scaffold passes its own
  empirical-integrity check out of the box.

systematic-review skill's "Additional templates" section now lists all
six templates the plugin ships (search_config, screening_config,
test_suite, stats, _tables, manuscript) and what each is for.

### Plugin end-to-end status

The pipeline is now complete for social-sciences SLRs from search
through render. Stages and their shipped scripts:

- Search → `search.py` / `search_openalex.py`
- Import → `import_to_zotero.py`
- Enrich → `fetch_abstracts.py` + `attach_pdfs.py` /
  `fetch_pdfs_wiley_tdm.py` / `fetch_pdfs_browser.py`
- Audit → `audit_zotero_library.py`
- Screen → `abstract_screen.py` + `fulltext_code.py`
- Export → `export_coded_includes.py`
- Bibliography → `generate_bib.py`
- Test → `templates/test_suite.py`
- Render → `templates/manuscript.qmd` + `templates/stats.py` +
  `templates/_tables.py`

### Still deferred (v0.2.x candidates)

- Zotero tag + child-note write-back from `fulltext_code.py` (coded
  decisions currently live only in the CSV log).
- Standalone `search_scopus.py` / `search_wos.py` piloting wrappers.
- INFORMS and OUP custom flows in `fetch_pdfs_browser.py` (caught by
  the `live_browser` test suite as FAIL today).

72 default tests pass; ruff clean.

## [0.1.9] — 2026-04-21

### Screening scripts — Milestone F

Ports `abstract_screen.py` and `fulltext_code.py` from the SLR
reference project with full generalisation: prompts, coding schema,
and model choice all come from a per-project `screening_config.py`,
so the plugin's copies of the scripts are deliberately generic.

New files:

- **`scripts/pipelines/abstract_screen.py`** (~220 lines) — stage-1
  screening. Claude Haiku on title+abstract at temperature=0. Reads
  `ABSTRACT_SCREENING_SYSTEM_PROMPT`, `ABSTRACT_SCREENING_MODEL`, and
  `ABSTRACT_SCREENING_PROMPT_VERSION` from the config. Parallelised
  with `ThreadPoolExecutor` + `threading.Lock` on the CSV log. Append-
  only log with `item_key` as key; re-running skips already-screened
  items. Flags: `--dry-run`, `--sample N`, `--workers N`.
- **`scripts/pipelines/fulltext_code.py`** (~320 lines) — stage-2
  screening + structured coding. Claude Sonnet on full PDF text.
  Reads `FULLTEXT_CODING_SYSTEM_PROMPT` and `FULLTEXT_CODING_FIELDS`
  from the config. **The coding schema is dynamic**: the script
  renders the field list into the system prompt's JSON-schema
  section, and builds the output CSV's columns from the same list —
  add a field to `FULLTEXT_CODING_FIELDS` and both the prompt and
  CSV schema update automatically. Uses `core.llm.extract_pdf_text`
  (pdfplumber + pypdf fallback) and `core.llm.extract_json_from_response`
  for lenient JSON parsing of Sonnet's output. Flags: `--dry-run`,
  `--limit N`, `--only-keys K1,...`, `--workers N`, `--rerun`
  (reprocess error rows), `--full-recode` (backup + rebuild).
- **`templates/screening_config.py`** — minimal-but-runnable template
  for both screening stages. Placeholder research question, inclusion
  criteria, exclusion codes, and three starter coding fields
  (`key_findings`, `sample`, `method`) for the user to extend. Each
  prompt carries a `PROMPT_VERSION` string that lands in every CSV
  row for traceability.

`systematic-review` skill's stage-to-script table now includes both;
the "deferred" section shrinks to just the Quarto manuscript
scaffold.

**Deferred to v0.2.x**: automatic write-back of `fulltext:include` /
`fulltext:exclude` tags and coded-field child notes to Zotero
(currently documented as a post-run reminder in the script output).

72 tests pass (no new unit tests for the screening scripts — they're
thin wrappers over API clients; live smoke testing is the right
approach). Ruff clean.

## [0.1.8] — 2026-04-21

### Search scripts — first half of Milestone E

Ported the two main search scripts from the SLR motivation reference,
plus a template for the per-project search configuration. Pipeline
can now run a real formal search against Scopus / WoS / OpenAlex from
a project's own `search_config.py`.

New files:

- **`scripts/pipelines/search.py`** (~335 lines) — Scopus + Web of
  Science Expanded orchestrator. Reads a per-project
  `search_config.py` by path (via `--config`). Runs each `QUERY_DEFS`
  entry against Scopus (via pybliometrics) and optionally WoS
  (`--wos`). Deduplicates across databases by DOI with a
  title+first-author fallback for no-DOI records; merges abstracts
  when they exist. Writes `search_results_raw.csv`,
  `search_results.csv`, `search_metadata.json`, and a DOI-set hash
  in `search_run.json` (the integrity gatekeeper every downstream
  test reads).
- **`scripts/pipelines/search_openalex.py`** (~250 lines) — free
  alternative using OpenAlex REST API. Runs two block queries
  (`BLOCK_A_TERMS`, `BLOCK_B_TERMS` from `search_config.py`)
  separately and merges, because OpenAlex's relevance-ranked
  `search=` parameter loses recall on combined queries. No API key
  required; uses `CROSSREF_MAILTO` for polite-pool identification.
- **`templates/search_config.py`** — minimal-but-runnable example
  with 5 entrepreneurship journals, two `QUERY_DEFS` entries
  (narrow + broad), and two OpenAlex block-term lists. Comments
  explain per-query Scopus vs. WoS stemming differences and the
  recall reasoning behind the block-query approach.

Updated `systematic-review` skill: the script-invocation table now
lists `search.py` and `search_openalex.py`, and the "deferred"
section drops the search-scripts bullet.

**Still deferred for later milestones:** standalone `search_scopus.py`
/ `search_wos.py` (users can run `search.py` with just one database
today), `abstract_screen.py`, `fulltext_code.py`, Quarto manuscript
scaffold.

No changes to existing plugin code. Default tests unchanged (72 pass).

## [0.1.7] — 2026-04-21

### Live-test fixes after first real run

First real-keys run of the new live suite flushed out three failures.
Root causes were a mix of test-code bugs and wrong test DOIs:

- **`test_scopus_abstract` was genuinely broken.** I used
  `view="META_ABS"` which populates `.description` and leaves
  `.abstract` as `None` — a pybliometrics quirk. The plugin's
  production code at `fetch_abstracts.py` correctly uses
  `view="FULL"`. Test now matches production. (Also dropped a
  one-off debug helper at `scripts/debug/inspect_scopus_abstract.py`
  that surfaces this kind of pybliometrics field-naming oddity for
  future debugging.)
- **`test_crossref_tdm_link_present` had the wrong DOI.** PLOS ONE
  DOIs have no text-mining link on Crossref because they are fully
  open-access and expose full text elsewhere. Switched to an Elsevier
  DOI (verified: 2 text-mining links). On any future DOI that still
  lacks a TDM link, the test skips with an explanation pointing at
  `KNOWN_DOIS['crossref_tdm']`.
- **`test_wiley_tdm_downloads_pdf` had an out-of-scope DOI.** The
  ETP 2010 DOI was not in the institution's Wiley TDM scope (ETP
  moved to Sage in 2022; older issues may or may not be TDM-accessible
  at Wiley). Switched to an SMJ 2024 DOI. On "Unknown Doi" / "not
  entitled" / "forbidden" responses, the test skips with an
  institutional-scope explanation rather than failing.
- **Browser-test `wiley` DOI updated** — same ETP-at-Sage issue; now
  points at the same SMJ DOI.

Test-design principle codified: **PASS when the endpoint works, SKIP
when the test DOI falls outside the provider's coverage, FAIL only
when the endpoint itself is broken.** On your machine today:
18 passed, 5 skipped (all known-legitimate), 0 failed.

### Wizard MCP-server registration

The `/setup` wizard now checks five Model Context Protocol servers
and offers to register any that are missing:

- **Zotero** (required tier — every citation skill depends on it).
- **Scopus / Semantic Scholar / OpenAlex** (search-database tier —
  at least one required for literature search).
- **paper-search** (optional — ArXiv / PubMed PDF retrieval).

Each server has a `McpServerSpec` with a homepage, an install
command, and a free-text install note. The wizard parses
`claude mcp list` output to classify each server as connected /
needs-auth / failed / missing, prints a summary with counts per
tier, and exits with code 4 if the required Zotero server is not
connected. The `setup` skill's error-handling guidance is updated
to cover "command not found" for each underlying MCP binary and
the new exit-code-4 case.

Wizard grew by ~380 lines; tests grew accordingly (72 default tests,
was 59).

### No changes to plugin production-pipeline code.

## [0.1.6] — 2026-04-20

### Test-suite template for SLR projects

Ports the 528-line project-specific test suite from the reference SLR
into a generalised template at
`templates/test_suite.py`. Ship 13 universal tests that check
invariants every SR pipeline must satisfy, plus commented scaffolding
for four project-specific test families the user fills in.

**Universal tests (run out of the box):**

- Pipeline artefacts exist and are non-empty.
- `search_run.json` DOI count matches the deduplicated CSV.
- `search_metadata.json` has required fields (dates, databases, year
  bounds, queries).
- No duplicate DOIs in the deduplicated search output.
- Abstract / full-text decision states match the allowed whitelists.
- PRISMA arithmetic: fulltext-screened items all come from the
  abstract-include+borderline set.
- Coded-papers row count equals fulltext-include count.
- Temperature=0 pinned in every Claude API call (regex across
  `abstract_screen.py` and `fulltext_code.py`).
- Top-level `MODEL` and `PROMPT_VERSION` constants match what the
  logs recorded.
- BBT keys in `coded_papers.csv` are non-empty and unique.
- No `decision=error` rows remaining after `--rerun`.
- No "ghost" keys (items in logs but absent from Zotero) — skipped
  cleanly if `pyzotero` or local Zotero unavailable.

**Project-specific scaffolding (commented out, uncomment to enable):**

- Coding-field completeness — list your schema's required field names.
- Forbidden methodology literals in manuscript prose (model names,
  version strings, hand-typed counts).
- Manuscript `@citekey` resolution against `references.bib`.
- `stats.json` freshness vs. `coded_papers.csv` modification time.

Shared `TestRunner` infrastructure (verbose + concise output, exit
code 0/1, unhandled-exception capture) makes customisation low-effort.
Copy the template, uncomment the tests that apply, run
`uv run scripts/test_suite.py`.

`systematic-review` skill updated to point at the new template.

## [0.1.5] — 2026-04-20

### Two new pipeline scripts — first steps toward end-to-end SLR

Ported from the `SLR motivation` reference project, generalised for
plugin use. Both had been referenced in the `systematic-review` skill
but were missing from the plugin.

- **`scripts/pipelines/import_to_zotero.py`** — read a deduplicated
  search CSV, create or update Zotero items with three-layer dedup
  (DOI match → title+first-author match → within-batch dedup). Accepts
  `--group`, `--collection`, `--input`, `--dry-run`; no project-specific
  defaults. Reads API key via `core.config_loader` so the key stays
  out of Claude's context. Prints an explicit `NEXT STEP — run a
  duplicate check` reminder.
- **`scripts/pipelines/export_coded_includes.py`** — filter a
  full-text-screening CSV to the `decision=include` subset with
  last-row-wins semantics on `item_key` (so adjudication flips via
  appended rows take effect). Configurable output columns and
  decision filter (`--decision exclude` useful for PRISMA reporting).
  Pure stdlib, no external deps.
- **Unit tests** — 5 new tests for the export script's filtering,
  last-row-wins, dry-run, column restriction, and alternative-decision
  behaviours.
- **`systematic-review` skill** — stage-to-script table updated to
  include the two new scripts and call out explicitly which scripts
  are still deferred (search, abstract-screen, fulltext-code,
  test-suite template).

54 → 59 default tests. Ruff clean.

### Still deferred (roadmap)

- Search scripts (Scopus / WoS / OpenAlex variants + `search_config.py`
  template).
- Abstract screening (`abstract_screen.py`, Haiku) and full-text
  coding (`fulltext_code.py`, Sonnet) — the biggest lift because
  both require schema-driven prompt templates.
- `test_suite.py` template.
- QA evaluator pattern documentation in `systematic-review` skill.
- Quarto manuscript scaffold + `stats.py` builder pattern.

## [0.1.4] — 2026-04-20

### Live test suite

New opt-in test suite that probes every external service the plugin
talks to: PDF endpoints, abstract endpoints, authentication
workflows. Runs only when explicitly invoked, never automatically,
never in CI.

- **`pytest -m live`** — 23 direct-HTTP tests: 8 PDF (Crossref TDM
  metadata, PMC, Elsevier/ScienceDirect, OpenAlex Content, Springer
  direct, Unpaywall, OpenAlex OA URLs, Wiley TDM), 5 abstract
  (Crossref, Semantic Scholar, Scopus, ScienceDirect, OpenAlex
  GROBID), 10 auth workflows (one per KeySpec, reusing the wizard's
  `_verify_*` helpers so the test exercises the same path the wizard
  uses at setup).
- **`pytest -m live_browser`** — 9 tests parametrized directly from
  `publishers.registry.DEFAULT_PUBLISHERS`. Opens a shared persistent
  Chromium; user solves CF challenge + institutional SSO once per
  publisher domain; assertions use `%PDF-` magic bytes (catches
  HTML-wrapper responses that masquerade as 200 OK).
- **Coverage guard** at `tests/unit/test_live_coverage.py` (runs on
  every default `pytest` invocation). Asserts every registry entry,
  every `KeySpec`, every `fetch_*_pdf`, and every `fetch_from_*` has
  a matching live test. Failing produces an actionable message
  naming the gap. Enforces the "every new service ships with a
  test" project policy.
- **Dependencies are opt-in.** Tests `pytest.importorskip` the
  Python packages they need (`wiley-tdm`, `playwright`,
  `pybliometrics`), so default contributors don't pay the install
  cost. README at `tests/live/README.md` documents the one-line
  install.
- **Known-stable DOIs** in `tests/live/conftest.py` — best-guess
  starting points. Users may need to edit for journals not covered
  by their institutional subscription.

### Numbers

- Default `pytest`: 54 unit tests (was 50). +4 guard tests.
- `pytest -m live`: 23 tests. Each skips cleanly if its key is
  missing.
- `pytest -m live_browser`: 9 tests. `-x` bails at first failure.
- Total with both markers: 86 tests.

## [0.1.3] — 2026-04-20

### UX polish after first real-pipeline run

- **Audit script writes `.keys` files directly.** After
  `audit_zotero_library.py` runs, `/tmp/zotero_audit.missing_abstract.keys`,
  `.missing_pdf.keys`, and `.empty_stubs.keys` land next to the JSON —
  feedable straight to the next pipeline stage's `--filter-keys-file`
  flag. Eliminates the `jq` extraction step (which triggered a
  permission prompt and invited the `empty_stub` vs `empty_stubs`
  singular/plural typo). The script's "Next steps" output now shows
  the exact command to run for each non-empty category.
- **Browser fetcher announces itself.** `fetch_pdfs_browser.py` prints
  a 20-line banner before launching Chromium: what is about to happen,
  which publishers are queued with counts, what the user may be asked
  to do (solve CF challenge, sign in via SSO). No more surprise
  browser windows.
- **Skill-level narration rule.** `zotero-operations` now instructs
  Claude to announce potentially startling stages to the user *before*
  running them (browser fetches, long attach_pdfs runs, first-run uv
  installs).
- **Canonical workflow prose updated** — the skill's "add missing
  abstracts and PDFs" walkthrough drops the `jq` step and references
  the `.keys` files directly.

## [0.1.2] — 2026-04-20

### Security hardening

- **Canonical scripts replace improvised pipeline code.** When Claude
  was asked to "add missing abstracts and PDFs" it composed a Python
  heredoc that read `config.toml` to extract the API key and run a
  library audit. That approach leaks keys through Claude's tool
  context. The fix ships a real audit script
  (`scripts/pipelines/audit_zotero_library.py`) and hardens the
  `zotero-operations` skill to forbid improvisation.
- **Shared config reader** (`scripts/core/config_loader.py`) — all
  pipeline scripts now have a single canonical path to read
  `~/.config/academic-research/config.toml`. Env vars take precedence;
  `require()` raises a clear error if a required value is missing.
- **Broader `permissions.deny` patterns** — wizard now writes deny
  entries for `cat`/`head`/`tail`/`grep`/`less`/`more`/`awk`/`sed`/
  `od`/`xxd`/`strings`/`bat` against both the absolute and tilde-prefix
  form of the config file. Not exhaustive (Python heredocs still
  slip through), so skill-level red flags are the primary defence.
- **Explicit "never read config" red flag** added to every procedural
  skill (`systematic-review`, `zotero-operations`, `fact-check`,
  `critic-loop`).
- **Explicit "never improvise a pipeline script" red flag** added to
  `zotero-operations` and `systematic-review`.

### New functionality

- `scripts/pipelines/audit_zotero_library.py` — classify a library's
  items into have-PDF / missing-PDF / empty-stub / missing-abstract
  categories. Prints summary, writes JSON. Intended to drive
  `fetch_abstracts.py` and `attach_pdfs.py` via their
  `--filter-keys-file` argument.

### uv + PEP 723 inline dependencies

- All pipeline scripts now declare their runtime deps in a PEP 723
  header. `uv run <script>` auto-installs into an ephemeral venv on
  first run — no more `pip install` before use, no system-Python
  pollution.

### Skill updates

- `zotero-operations` — added a canonical "intent → script" table, a
  step-by-step workflow for "add abstracts and PDFs", and forbids
  directory probing or improvised scripts.
- Systematic-review, fact-check, critic-loop, zotero-operations —
  now each have the two new hard-rule red flags above.

## [0.1.1] — 2026-04-20

### Security fix (breaking UX change)

- **`/setup` now launches a terminal wizard** (`scripts/setup/wizard.py`)
  instead of collecting API keys in chat. The previous design asked
  the user to paste API keys into the Claude chat, which would have
  transmitted them to Anthropic's API as part of the user message.
  The wizard reads keys with `getpass` in the user's terminal — keys
  never enter Claude's context.
- The setup skill now detects TTY and either launches the wizard
  in-process (CLI Claude Code) or instructs the user to open a
  terminal (Desktop / Positron / VSCode / headless).
- Wizard is idempotent; re-run to update or add keys.
- Wizard patches `~/.claude/settings.json` with the plugin's
  permission rules (allow `Bash(... ${CLAUDE_PLUGIN_ROOT}/scripts/**)`,
  deny `Read` on the config file). Backs settings.json up before
  mutating.

## [Unreleased]

### critic-loop extensions (deferred from 2026-04-19 prior-art review)

- **Devil's Advocate** as a 5th parallel critic (forces construction of the
  strongest case *against* the manuscript's position). Revisit after seeing
  4-critic loop performance.
- **Traceability matrix** for iteration 2+ — feed each critic a diff since
  its prior iteration plus its own prior unresolved issues, to verify
  substantive fixes rather than cosmetic rewrites.

### Potential improvements (deferred prior-art)

- **Marker** (GPL-3.0) — LLM-assisted PDF extraction for CID-font garbling.
  Integrate via subprocess CLI only (not import) to preserve MIT licensing.
  Candidate fallback in `scripts/core/pdf_extract.py` when both pdfplumber and
  pypdf fail the quality score.
- **paperscraper** (MIT) — Wiley + Elsevier TDM + bioRxiv + PMC BioC-XML.
  Partial overlap with `scripts/pipelines/attach_pdfs.py`; integration would
  require rewriting the orchestration layer. Defer until we have evidence the
  simplification is worth the churn.
- **grobid-client-python**, **semanticscholar** PyPI, **Europe PMC** — minor
  code-quality wins.
- **`/add-publisher`** scaffold skill — generate `publishers/<name>.py` stub
  from DOI prefix + login-required + CF-required inputs.

## [0.1.0] — TBD

Initial public release. See README for the full feature set.
