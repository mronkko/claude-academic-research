# Changelog — editorial-tools

Versioned independently from the `academic-research` plugin in this
same marketplace. See the root [CHANGELOG.md](../CHANGELOG.md) for
that plugin's history.

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
