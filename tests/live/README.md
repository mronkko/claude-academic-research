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

## Markers

Two opt-in markers, both deselected by default:

| Marker | What | Runtime | Needs a human? |
|---|---|---|---|
| `live` | Direct-HTTP PDF + abstract + auth tests (20 tests) | ~30s | No |
| `live_browser` | Cloudflare-gated publishers via Playwright (9 tests) | 5–15 min | Yes — click CF / SSO once per publisher |

## Commands

```bash
# Default run — unchanged behaviour, unit tests only.
pytest

# Run the direct-HTTP live tests.
pytest -m live

# Run the browser-based tests (opens Chromium; you click through).
pytest -m live_browser

# Everything.
pytest -m "live or live_browser"

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
