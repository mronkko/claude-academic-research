---
name: suggesting-reviewers
description: Use when an editor needs to identify peer reviewers OR an associate editor (AE) to handle a manuscript from its abstract — matching the paper's topics and methodology to editorial-board members, associate editors, and external experts and excluding ineligible people. Trigger phrases "suggest reviewers", "find reviewers for this paper", "who could review this", "recommend referees", "reviewer candidates", "suggest an AE", "assign an associate editor", "who should handle this paper", "recommend an AE", "select an associate editor". Do NOT use for writing or revising the manuscript — use `academic-research:critic-loop` / `manuscript-revision`. Do NOT use to verify a draft's citations — use `academic-research:fact-check`.
---

# Suggesting reviewers or associate editors

Given a manuscript abstract, propose either **peer reviewers** or an
**associate editor (AE)** to handle the paper, and explain each candidate's
fit. Default journal: *Organizational Research Methods* (ORM).

## Mode — reviewer vs. AE

Detect the mode from the request and announce which one you are running.

- **Reviewer mode** (default — "suggest reviewers", "find referees", …). Two
  pools: **editorial-board members** (filtered by eligibility, matched against
  pre-built profiles) and **external experts** (found live from the abstract's
  topics).
- **AE mode** ("suggest an AE", "assign an associate editor", "who should
  handle this paper", …). One pool: the journal's **current associate
  editors**. No external search — an AE must be a sitting board officer.

The two modes share everything below except the two steps that explicitly
branch (Step 2 eligibility, Step 4 candidate lists). If the request is
ambiguous, ask which is wanted before proceeding.

## Core principle — combine knowledge with retrieval

Training knowledge is a useful starting point: it maps the field and knows
established scholars. But it has two blind spots — **lesser-known / junior
researchers** and **very recent work** — and those are exactly where this
skill adds value. So **every run must augment training recall with
retrieval**: the pre-built board profiles plus live OpenAlex / Semantic
Scholar searches that deliberately reach for recent and less-visible names.

Ground each final suggestion in a retrieved publication record wherever one
exists — and *especially* for the less-famous names, where training recall is
least reliable. Never put a junior or unfamiliar person in the output on
memory alone.

## Step 1 — Load the roster and check staleness

The bundled ORM data lives under the plugin root:

```
${CLAUDE_PLUGIN_ROOT}/skills/suggesting-reviewers/rosters/orm/
  index.md            # membership + role + eligible: yes/no, with snapshot_date
  profiles/<surname>.md
```

Read `index.md`. Report to the user the `snapshot_date` (membership) and the
oldest profile `checked` date. If **either is older than 3 months**, warn with
the age and **ask whether to refresh before continuing** — do not silently
re-fetch. (Refresh procedure: Step 5.)

For a journal other than ORM, ask the user to point at (or build) the
equivalent `rosters/<journal>/` directory.

## Step 2 — Apply eligibility (deterministic)

**Reviewer mode.**

- **Exclude** every member tagged `eligible: no` — current Editor(s)-in-Chief
  and Associate Editors. They assign reviews; they are never suggested.
- **Eligible pool** = every member tagged `eligible: yes` (past editors still
  listed, Editorial Review Board members, advisory roles).

If an excluded editor is the obvious topical match, do **not** suggest them —
note the exclusion in a transparency line at the end instead.

**AE mode.** The pool inverts: candidates are exactly the members whose
`role` is **Associate Editor**. Editors-in-Chief, the Outreach Officer,
past/founding editors, and plain Editorial Board members are **not** AE
candidates — only sitting AEs assign and handle papers. (If a specific AE is
the submitting author or is otherwise conflicted and the user supplied that
information, drop them and note it; otherwise leave conflicts to the assigner.)

## Step 3 — Read the abstract → methodology + topics

Classify the paper's methodological orientation — **quantitative**,
**qualitative**, **mixed**, or **methods-development** — and extract its
substantive topics. This drives a **hard methods filter**:

- A clearly **quantitative** paper surfaces no `qualitative`-only scholar.
- A clearly **qualitative** paper surfaces no `quantitative`-only scholar.
- `mixed-or-methodologist` profiles match either.

When the paper itself is genuinely mixed, both orientations are in scope.

## Step 4 — Build the candidate list(s)

**AE mode — read this first.** Build a **single** list from the AE pool of
Step 2 and **skip the external search entirely** (an AE must be a sitting
board officer). Match and ground each AE exactly as Step 4 describes for board
reviewers below — methods filter (Step 3), topical fit against the profile's
topics / representative method papers / `## ORM publications`, drilling into
`mcp__openalex__get_author_works` only when a profile is thin or stale. AE
profiles are built the **same way as ERB profiles** (see "Full profile build"
under Step 5/Refresh); if a sitting AE has no profile yet, build one on first
use and commit it. Then go to Step 5 (AE output). The career-stage tilt and
the rest of this step apply to **reviewer mode**.

**Career-stage tilt (both pools):** rank credible **early-to-mid-career**
scholars first — these are the people the editor is least likely to already
know — but still include a couple of senior options. **Label every
candidate's career stage.** Apply a competence floor: roughly **≥3 relevant
papers in the last ~5 years** so the person can actually referee the work.

**Board reviewers.** From the eligible, methods-matched pool, rank by topical
fit against each profile's topics, its representative **method papers**, and its
`## ORM publications` list (the member's own first-party ORM articles — strong
evidence they referee that journal's style well). The profile is the grounding;
drill into `mcp__openalex__get_author_works` or
`mcp__semantic-scholar__authors-search` only if a profile looks thin or stale.

**External reviewers (live).** From the abstract's topics, search:

- `mcp__openalex__search_authors_by_expertise` — experts by research area.
- `mcp__openalex__search_works` (`sort=publication_year:desc`, `from_year`
  recent) — then extract authors, to catch **very recent** relevant work.
- `mcp__semantic-scholar__authors-search` — cross-check / fill gaps.

Favor lower-seniority but topically prolific authors (lower `works_count` /
more recent first publication, still active). If the user supplied the
paper's authors, do not suggest them; otherwise no COI screening (the editor
handles conflicts).

## Step 5 — Output

**Reviewer mode.** Two ranked markdown tables — **Editorial board** and
**External** — columns:

| Name | Affiliation | ERB role / External | Career stage | Methods | Representative method paper(s) | Fit |

`Fit` ties the suggestion to **specific themes in the abstract**. Rank by fit
within the junior tilt. After the tables, add any transparency note for a
strong-but-ineligible board match, and state explicitly which suggestions are
grounded in a retrieved record vs. flagged as training-recall only (there
should be none of the latter for junior/unfamiliar names).

**AE mode.** One ranked markdown table — **Associate editors** — same columns
(the role column reads "Associate Editor"), ranked by topical/methods fit. No
junior tilt and no external/transparency lines for ineligible board matches
(every candidate is a sitting AE). Still state which suggestions are grounded
in a retrieved record.

## Refreshing the roster and profiles

Profiles are a **hybrid**: training knowledge about each scholar, *augmented
and corrected* by retrieved OpenAlex data. Refresh is committed to the repo
so both machines (and any ORM AEs) share the same curated data.

**Membership diff** — WebFetch `source_url` from `index.md`, diff against the
member list: new eligible members → full profile build (below); departed
members → remove their profile; role/eligibility changes → edit `index.md`.
Update `snapshot_date`.

**Primary: one ORM-journal sweep (cheap, every refresh).** Collect every ORM
article authored by any current member in a *single* call:

```
mcp__openalex__collect_works(
    author_ids = [<all eligible-member openalex_id values>],   # any length; chunked by 100 internally
    source_ids = ["S133599136"],                               # ORM
    sort       = "publication_year:desc",
    max_results = 2000)                                          # verify meta.truncated == false
```

Each returned work carries author OpenAlex IDs, so **ID-join** to members
locally (no name matching). For each member, rewrite the `## ORM publications`
section (keep `article`/`review`, drop `editorial`/`paratext`), set
`orm_works_count`, and bump `orm_swept`. New ORM papers since the last sweep are
the cheap signal that a profile's representative papers / orientation may need a
look. This one call replaces the old per-member ORM polling.

**Occasional: per-member all-venue deep refresh.** The ORM sweep only sees ORM
papers; a member's defining **method papers often sit in other journals**. When
a profile looks thin or stale (or its ORM count jumped), refresh that member's
broader record: `mcp__openalex__get_author_works(author_id, from_year=<year of
works_through>, sort=publication_year, per_page=200)` returns only works since
the watermark. If the slice has new method-relevant papers, refresh the
representative papers / orientation / specialties / stage and set a new
`works_through`; otherwise just bump `checked`. Do this for a rotating subset,
not all 129 every time.

**Full profile build (per member).**
1. Resolve to an OpenAlex author: `mcp__openalex__search_authors(query=name)`.
   Do **not** combine the `institution` and `exact_phrase` parameters — that
   returns HTTP 400; pass the name alone and disambiguate from the results by
   `last_known_institutions` + topical plausibility + ORCID. Pick the entry
   whose affiliation matches the board listing and that has a real publication
   record (not a 1–3 work near-empty namesake). Store `openalex_id`/`orcid`.
   If ambiguous, flag for manual confirmation — never guess an ID.
2. `mcp__openalex__get_author_profile(author_id, top_works_count=10,
   recent_works_count=8)` → h-index, citations, main topics, top + recent
   works.
3. Select representative **method papers** — those showing a methodological
   contribution (technique, measurement, analytic critique), **regardless of
   journal** (a method paper in a substantive journal counts). **Ignore
   non-article clutter** in `recent_works`: figshare/OSF "Additional file",
   datasets, IRB materials, and other `type: other`/repository entries are not
   papers — filter to real `article`/`review`/`book-chapter` items with a
   journal source.
4. Classify `methods_orientation`; derive `career_stage` from earliest
   affiliation/work year + `works_count` + h-index, augmented with training
   knowledge.
5. Write the profile with `built`, `works_through` (newest real publication),
   `checked` dates.

## Profile file shape

```yaml
name: <Full Name>
openalex_id: A123456789
orcid: 0000-0000-0000-0000      # if available
role: <ERB role from index.md>
eligible: yes
methods_orientation: quantitative | qualitative | mixed-or-methodologist
specialties: [SEM, measurement invariance, ...]
career_stage: early | mid | senior
career_signals: { first_pub_year: YYYY, works_count: N, h_index: H }
built: YYYY-MM-DD
works_through: YYYY-MM-DD        # newest all-venue publication considered (deep-refresh watermark)
checked: YYYY-MM-DD              # last refresh check, even if nothing changed
orm_works_count: N              # first-party ORM articles found in the last journal sweep
orm_swept: YYYY-MM-DD           # date of that ORM-journal collect_works sweep
```

Body: representative method papers (one line each on the contribution), the
topics the member would referee well, short training-knowledge notes the data
doesn't capture, and a `## ORM publications` section (the member's own ORM
articles, newest first) written by the journal sweep.

## Red flags — stop

- **Reviewer mode:** about to suggest a current EIC/AE (`eligible: no`) → drop;
  they're excluded.
- **AE mode:** about to suggest a non-AE (EIC, Outreach Officer, past/founding
  editor, plain Editorial Board member) → drop; only sitting AEs qualify.
- **AE mode:** running an external search or applying the junior tilt → stop;
  AE mode is the AE pool only, ranked by fit.
- A junior/unfamiliar name with no retrieved record behind it → drop or ground.
- An opposite-orientation scholar slipping past the methods filter → remove.
- A candidate below the competence floor → remove.
- Relying on training recall without running the augmenting searches → go back
  to Step 4 and search.
