# Live test suite

Opt-in tests that probe every external service the plugin talks to:
PDF endpoints, abstract endpoints, and authentication workflows. They
run only when explicitly invoked — never automatically, never in CI.

## When to run

- After rotating any API key — confirm the new credentials work end
  to end.
- After a plugin upgrade — check that a dependency bump did not break
  a publisher flow.
- When a user reports a pipeline failure — isolate whether the
  upstream service is the problem.
- Before starting a systematic review — confirm the infrastructure
  your SR will depend on is healthy.
- After changing any pipeline script under `scripts/pipelines/` — `live_slr`
  is the only test that chains search → import → enrich → screen → code
  → export in one run, so it catches defects the per-stage unit tests
  can't (a stage writing a field name the next stage reads differently,
  a path convention two scripts disagree on, and so on).

## Markers

Three opt-in markers, all deselected by default:

| Marker | What | Runtime | Needs a human? |
|---|---|---|---|
| `live` | Direct-HTTP PDF + abstract + auth tests (20 tests) | ~30s | No |
| `live_browser` | Cloudflare-gated publishers via Playwright (9 tests) | 5–15 min | Yes — click CF / SSO once per publisher |
| `live_slr` | Full mini systematic review through a real Zotero group (1 test) | ~5–12 min | No (after one-time group setup below) |

## Commands

```bash
# Default run — unchanged behaviour, unit tests only.
pytest

# Run the direct-HTTP live tests.
pytest -m live

# Run the browser-based tests (opens Chromium; you click through).
pytest -m live_browser

# Run the live mini end-to-end SLR (real API spend — see below).
pytest -m live_slr

# Everything.
pytest -m "live or live_browser or live_slr"

# Stop at the first failure (useful for browser tests).
pytest -m live_browser -x
```

## Configuration

Tests read keys from `~/.config/academic-research/config.toml` (the
file the `/setup` wizard writes) or the corresponding environment
variable — env takes precedence. If neither is set, the test skips
with an actionable message.

Keys read per test set:

- PDF: `CROSSREF_MAILTO`, `ELSEVIER_API_KEY`, `OPENALEX_API_KEY`,
  `WILEY_TDM_TOKEN`.
- Abstracts: `CROSSREF_MAILTO`, `SEMANTIC_SCHOLAR_API_KEY`,
  `SCOPUS_API_KEY`, `ELSEVIER_API_KEY`, `OPENALEX_API_KEY`.
- Auth: all eight KeySpecs in the wizard
  (`ZOTERO_API_KEY`, `ANTHROPIC_API_KEY`, `WOS_API_KEY_EXTENDED`,
  `WOS_API_KEY`, `ELSEVIER_API_KEY`, `SCOPUS_API_KEY`,
  `SEMANTIC_SCHOLAR_API_KEY`, `CROSSREF_MAILTO`). Plus placeholder
  tests for `WILEY_TDM_TOKEN` and `OPENALEX_API_KEY` that skip with
  explanations (no cheap auth-only probe exists for those two).

## `live_slr` one-time setup

`live_slr` drives `scripts/dev/mini_slr.py` — a resumable stage
driver that runs the whole systematic-review pipeline (search, Zotero
import, enrichment, abstract screening, full-text coding, export, and a
verify pass) against real APIs and a small, disposable Zotero **group**.

It targets a group named exactly `academic-research-e2e`, resolved by
name — never by env var, config section, or `--group` flag (My Library
already carries live SLR tags from a real review; importing test data
into it would corrupt that record). The Zotero Web API cannot create
groups, so this one-time step is manual:

1. Create a group at <https://www.zotero.org/groups/new> named exactly
   `academic-research-e2e` (Private membership is fine — this is scratch
   space, not something to share).
2. Open Zotero Desktop and let it sync (Preferences → Sync, or just wait
   a minute) so the group appears in the local Zotero API
   (`localhost:23119`) — every pipeline script reads locally, not from
   the cloud, so this step is required, not optional.
3. Leave the group otherwise empty. `test_mini_slr_end_to_end` tears
   down everything it creates after each run (delete-by-recorded-key,
   never by tag match, so it never touches anything else that might end
   up in the group); a fresh run works from an empty group either way.

After that, `pytest -m live_slr` needs only `ZOTERO_API_KEY` +
`ZOTERO_USER_ID` (written automatically by `/setup`) plus whichever
search/enrichment/screening keys you want exercised — same
`config.toml` / env-var precedence as every other live test. Databases
or PDF sources without usable credentials are skipped by the pipeline
scripts themselves (see `search.py`'s "no database has usable
credentials" message), not by this test.

Runs are resumable. If a run fails partway, `mini_slr.py` prints its
`--run-id`; re-invoke a single stage directly instead of re-running the
whole pytest test:

```bash
uv run scripts/dev/mini_slr.py --stage screen --run-id 20260812T140000Z
uv run scripts/dev/mini_slr.py --stage teardown --run-id 20260812T140000Z
```

Set `MINI_SLR_KEEP=1` before `pytest -m live_slr` to skip teardown on
success and inspect the run's Zotero items/collection and
`output/e2e/<run-id>/` artefacts afterwards (you're then responsible for
running the `teardown` stage yourself once done).

## Dependencies

Tests `pytest.importorskip` the Python packages they need, so a
missing package produces a clean skip, not an error. To run the full
suite:

```bash
uv pip install wiley-tdm playwright pybliometrics
playwright install chromium
```

## Test DOIs

Hard-coded in `conftest.py` as `KNOWN_DOIS`. They must be:

- Stable (published > 3 years ago, unlikely to be retracted or moved).
- Covered by the target publisher's DOI prefix.
- Accessible under your institutional subscription (especially for
  the browser tests — SSO fails otherwise).

The checked-in DOIs are best-guess starting points. **If your
institution does not subscribe to a journal whose DOI appears there,
edit `conftest.py` before running.** The test will fail cleanly (no
SSO session → no download event) if it cannot reach the content.

## Interpreting failures

| Symptom | Meaning |
|---|---|
| `skipped: X — ...KEY not set` | The key is not in config.toml or env. Run `/setup` or `export KEY=...`. |
| `assert status == 200 ... got 401` | The key is rejected. Rotate it. |
| `assert status == 200 ... got 404` | The DOI is not in that provider's index. Update `KNOWN_DOIS`. |
| `status == 0` | Network error or DNS failure — check connectivity. |
| `"Cloudflare challenge page"` | Browser test: you didn't solve the CF challenge; re-run and click through. |
| `"access denied / no subscription"` | Browser test: your institution does not subscribe to that journal. Pick a different DOI. |
| `did not return a PDF ... HTML response` | Publisher returned an HTML wrapper page. Likely the `download_via_*` flow is broken or outdated. |
| `skipped: no Zotero group named 'academic-research-e2e' ...` | `live_slr` only — do the one-time group setup above. |
| `ERROR: local Zotero sync timed out ...` | `live_slr` only — Zotero Desktop isn't running, isn't synced, or Better BibTeX's local API (`localhost:23119`) is unreachable. |

## Coverage guard

`tests/unit/test_live_coverage.py` is a regular (default-run) unit
test that asserts every service the plugin touches has a matching
live test. When it fails, the message names the exact thing that's
missing:

> `Registry publishers without a KNOWN_DOIS entry: ['newpub']. Add DOIs
> to tests/live/conftest.py so test_browser_publishers.py can exercise
> them.`

Adding a publisher / KeySpec / source without a matching test will
break CI. The guard is load-bearing for the "every new service ships
with a test" project rule.

## Out of scope

- Headless / CI execution of `live_browser`. A CI runner cannot click
  a Cloudflare challenge. If you want headless browser coverage, that
  is a separate plan (hosted CF-solver or pre-authenticated cookie
  vault).
- Deep `wiley-tdm` auth flow testing — we rely on the package and
  only verify our orchestration returns bytes.
- Periodic scheduled runs. These are genuinely opt-in by design.
