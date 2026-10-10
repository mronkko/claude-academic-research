# Changelog — editorial-tools

Versioned independently from the `academic-research` plugin in this
same marketplace. See the root [CHANGELOG.md](../CHANGELOG.md) for
that plugin's history.

## [Unreleased]

- Refreshed the ORM roster against the live SAGE board page
  (snapshot 2026-10-10): 15 new Editorial Board members with
  OpenAlex-grounded profiles (Baluch, Bowling, Cornelissen, Elias,
  Harrison, Hoon, Langley, Lee, Lonati, Mees-Buss, Nguyen, Rockmann,
  Rolfing, Sheep, Whittle); 24 departed members' profiles removed;
  the Outreach Officer role is gone from the page and from the index.
  Affiliations updated for J. M. Cortina, Oswald, Schoen and Bliese.
- Re-ran the ORM-journal sweep for all 129 members. Counts drop for
  some senior members because OpenAlex now types their editorials
  and book reviews as such, and the sweep keeps only
  `article`/`review`. Authorships whose OpenAlex author ID went null
  (Hernandez, Hansen, Busenbark, Nguyen) are recovered by exact
  first/last-name match.

## [0.2.0] — 2026-06-16

- Added an associate-editor suggestion mode to `suggesting-reviewers`.
  Reuses the reviewer-matching logic but draws from the journal's
  sitting AEs instead of the editorial board, and skips the external
  live-search step (single list, no outside candidates). Mode is
  detected from the request.
- Built the 9 current ORM associate-editor profiles (Dawson,
  DeSimone, Greckhamer, Krasikova, Lê, Rönkkö, Stanton, Welch,
  Withers), OpenAlex-grounded the same way as the editorial-board
  profiles.

## [0.1.0] — 2026-06-06

- Initial release. Ships `suggesting-reviewers`: given a manuscript
  abstract, proposes peer reviewers from two pools — eligibility-
  filtered editorial-board members matched against pre-built
  profiles, and live-searched external experts — with a hard
  methods-orientation filter and an early-to-mid-career tilt.
- Bundled the ORM (Organizational Research Methods) editorial-board
  roster: 129 eligible members with OpenAlex-enriched profiles.
