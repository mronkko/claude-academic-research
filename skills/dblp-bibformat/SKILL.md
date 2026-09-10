---
name: dblp-bibformat
description: Use when creating, editing, or normalising a BibTeX `.bib` file so every entry follows the canonical DBLP format — DBLP citation keys (e.g. `DBLP:conf/venue/AuthorYY`), DBLP field set, and DBLP-verified metadata. Trigger phrases "use dblp format", "dblp bib", "normalise the .bib to dblp", "fetch this reference from dblp", "fix these bibtex entries to dblp". Do NOT use for Zotero library housekeeping or BBT key edits inside Zotero — use `zotero-operations`. Do NOT use for verifying a source actually supports a claim — use `fact-check`.
---

# dblp-bibformat

> **Glossary:** unfamiliar with **DBLP**, **BibTeX**, **DOI**, **BBT**?
> See [skills/_glossary.md](../_glossary.md) for one-line definitions.
> **DBLP** — the *dblp computer science bibliography* (`https://dblp.org`),
> a curated open database of CS publications that serves canonical
> BibTeX for every indexed paper.

## Doctrine

Every entry in a project `.bib` file **must** be the canonical BibTeX
record served by DBLP — never hand-typed, never scraped from a
publisher page, never left as a Google Scholar export. DBLP is the
single source of truth for:

1. **Citation key** — DBLP's own key, of the form
   `DBLP:conf/<venue>/<AuthorYY>` (proceedings) or
   `DBLP:journals/<venue>/<AuthorYY>` (journal). Keep the `DBLP:`
   prefix so the provenance is visible in the `.tex`/`.qmd` source.
2. **Field set** — exactly the fields DBLP emits (`author`, `title`,
   `booktitle`/`journal`, `year`, `pages`, `publisher`, `doi`,
   `biburl`, `bibsource`, `timestamp`). Do not add or delete fields.
3. **Author strings** — DBLP's disambiguated names (may carry a
   trailing `0001`-style suffix). Preserve them verbatim.

If DBLP does not index a work (books, tech reports, most non-CS
sources), **stop** and tell the user: this skill does not apply, and
the entry should come from Zotero/BBT via `zotero-operations` instead.
Do not fabricate a `DBLP:` key for an unindexed work.

## Procedure

### 1. Resolve each entry against DBLP

For every entry (or every citation key in the manuscript), find the
DBLP record:

```bash
# Search DBLP by title (returns JSON with the dblp key + BibTeX url)
curl -s "https://dblp.org/search/publ/api?q=$(python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))' "PAPER TITLE HERE")&format=json&h=5"
```

Prefer matching by **DOI** when the existing entry has one — it is
unambiguous. Confirm author list + year before accepting a hit; a
title-only match on a common title can be wrong.

### 2. Pull the canonical BibTeX

Each DBLP record exposes a stable BibTeX URL. Fetch the **condensed**
form (`?param=1`) unless the project already standardised on the
standard form:

```bash
# <KEY> is the dblp key, e.g. conf/icse/SmithJ21
curl -s "https://dblp.org/rec/<KEY>.bib?param=1"
```

- `param=0` — crossref/condensed (compact, uses a shared `@proceedings`)
- `param=1` — condensed, self-contained (recommended default)
- `param=2` — standard (verbose)

Pick one mode and apply it to the **whole file** — do not mix modes.

### 3. Normalise and write

- Replace the old entry wholesale with the DBLP BibTeX. Do not merge
  hand-edited fields back in.
- Keep the `DBLP:` prefix on the key. If the manuscript cites an old
  key, update every `\cite{...}` / `@key` reference to match, or add a
  one-line comment mapping old→new so nothing dangles.
- Sort entries by key for stable diffs.

### 4. Verify no citation dangles

After rewriting, every citation in the manuscript must resolve:

```bash
# list keys the manuscript cites vs keys present in the .bib
grep -rohE '\\cite[a-zA-Z]*\{[^}]*\}' *.tex 2>/dev/null | grep -oE '\{[^}]*\}' | tr -d '{}' | tr ',' '\n' | sort -u
grep -oE '^@[a-zA-Z]+\{[^,]+' references.bib | sed 's/^@[a-zA-Z]*{//' | sort -u
```

Report any key cited but missing from the `.bib`, or present but
uncited.

## Guardrails

- **Never invent a DBLP key.** If DBLP has no record, say so and route
  to `zotero-operations`.
- **Never mix** DBLP entries with hand-typed BibTeX in the same file
  without flagging it — the whole point is provenance uniformity.
- One `param=` mode per file.
- Do not strip DBLP's `biburl` / `bibsource` / `timestamp` fields;
  they are the audit trail.
