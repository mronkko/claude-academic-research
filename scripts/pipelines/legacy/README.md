# Legacy pipeline scripts

These are the pre-v0.3.0 orchestrators, retained as a rollback path
while the new `enrich_abstracts.py` / `enrich_pdfs.py` orchestrators
are proven on production libraries. **Do not extend them.** New
features go in the refactored modules under
`scripts/pipelines/fetchers/` and the top-level orchestrators.

## What's here

| Legacy | Replacement |
|---|---|
| `attach_pdfs.py` | `../enrich_pdfs.py` (default cascade) |
| `fetch_abstracts.py` | `../enrich_abstracts.py` |
| `fetch_pdfs_wiley_tdm.py` | `../enrich_pdfs.py --sources wiley` |
| `fetch_pdfs_browser.py` | `../enrich_pdfs.py --sources browser` (or `enrich_pdfs.py --sources browser --legacy-browser` to shell out to this script) |

## Why still on disk

Two reasons:

1. **Rollback.** If the refactored path breaks in a way that's hard
   to diagnose, `enrich_pdfs.py --legacy-browser` shells out to
   `fetch_pdfs_browser.py` here — the v0.2.x code path, untouched.
2. **Coverage guards.** `tests/unit/test_live_coverage.py` walks the
   `fetch_from_*` / `fetch_*_pdf` functions in these files to assert
   every source has a matching live test. When the refactored
   source modules under `../fetchers/` are mature and the coverage
   guard has been rewritten to walk them instead, this directory
   can be deleted.

## Deletion checklist

Once ready to remove:

- Remove the `--legacy-browser` flag from `enrich_pdfs.py` and its
  `_run_browser_legacy` helper.
- Rewrite `test_live_coverage.py` to walk the
  `AbstractFetcher` / `PdfFetcher` registries under `../fetchers/`
  instead of the legacy source files.
- Update `tests/live/test_browser_publishers.py` to drive the new
  handlers directly.
- `git rm -r scripts/pipelines/legacy/`.
