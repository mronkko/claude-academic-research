---
name: word-zotero-track-changes
description: Use when editing an existing Word (.docx) manuscript so the edits appear as tracked changes, especially one with live Zotero citation fields or co-authors' pending changes. Trigger phrases "edit the manuscript with track changes", "redline the docx", "insert citations into the Word file". Do NOT use for a new Word document — use `docx`. Do NOT use for Quarto/Markdown manuscripts — use `manuscript-revision`. Do NOT use for choosing what to cite — use `grounded-citations`.
---

# Word + Zotero track changes

Edit a co-authored Word manuscript so that every change shows up in Word
as an ordinary tracked change by one named author, Zotero citations stay
live fields, and co-authors' pending changes survive untouched.

Word stores a `.docx` as zipped XML (OOXML). The tooling here edits
`word/document.xml` directly with `w:ins` / `w:del` tracked-change
markup. Generic "edit the XML" approaches break on run merging, on
nested insertions and on Zotero fields; the pitfalls below each caused a
real failure.

## Companion skills

- **`grounded-citations`** — every citation you insert follows its
  rules: consult the source first; it must support the claim in context.
- **`zotero-operations`** / the Zotero MCP tools — item keys, and the
  item's Zotero JSON (`format="json"`) for items not yet cited in the
  document.
- **`critic-loop`** — after an editing round, run the critics on the
  accepted-text preview `check_docx.py --preview` writes. Their fixes
  come back through this skill.
- **Anthropic `docx` skill (optional)** — `check_docx.py` runs its
  `validate.py` when installed under `~/.claude`, verifying every change
  is tracked under the author. Without it that check is skipped with a
  warning. Use that skill for new documents, comments and PDF rendering.

## Requirements

`uv` (scripts declare `lxml` / `defusedxml` via PEP 723) and `pandoc`
for the preview. Scripts live in `${CLAUDE_PLUGIN_ROOT}/scripts/track_changes/`;
below, `$R` stands for that path. Paths below are relative to the
manuscript's folder for step 2 and to the work directory `WORK` from
step 3 on — `cd` into it, since the edit script resolves paths against
the current directory.

## Workflow

1. **Pre-flight.**
   - Confirm the author name to record. Use exactly the form already in
     the document's tracked changes (e.g. `Surname, Given`);
     `inspect_docx.py … --authors` lists them after step 2.
   - In a synced folder (OneDrive, Dropbox), check that no `~$` lock file
     exists and no co-author has the file open.
2. **Snapshot and unpack** into a scratch work directory:
   `uv run $R/unpack_docx.py manuscript.docx WORK` writes
   `WORK/00_original.docx` and `WORK/unpacked/`. Do **not** run
   run-merging scripts afterwards (see Pitfalls).
3. **Inspect.** `uv run $R/inspect_docx.py unpacked/word/document.xml [FROM [TO]]`
   lists paragraphs (0-based, inclusive indices) with `{+inserted+}`,
   `{-deleted-}` and `[ZFIELD]` marks. `--fields` prints field balance,
   Zotero field types and the cited library (`zotero.org/groups/<id>` —
   the `library_id` and `library_type` for `Citer`); `--authors` prints
   tracked-change authors.
4. **Edit.** Write an edit script (`edits.py`) in the work directory and
   run it with `uv run $R/run_edits.py edits.py`, which makes the
   library importable:
   ```python
   from redline import Doc
   from zotero_fields import Citer
   d = Doc('unpacked/word/document.xml', author='Surname, Given')
   citer = Citer(open(d.path, encoding='utf8').read(),
                 library_id='<group or user id>', library_type='groups')
   p = d.find_para('unique visible text')
   d.replace_text(p, 'old wording', [
       'new wording ',
       citer.cite('(Author, 2020, p. 5)', citer.item('ITEMKEY', locator='5')),
       '.'])
   d.save()
   ```
   | Method | Notes |
   |---|---|
   | `find_para(text)` | Matches visible (accepted) text. |
   | `replace_text(p, old, pieces)` | `old` must not cross a field boundary; `[]` deletes. |
   | `insert_text_after(p, after, pieces)` | Skips past a field the anchor lands in. |
   | `insert_paragraph_after(anchor, pieces)` | Paragraph mark is tracked too. |
   | `delete_paragraph(p)` | Tracked deletion of content and mark. |
   | `move_paragraph(src, after)` | Delete + insert; renews copied `citationID`s. |
   | `delete_field(p, result_text)` | Removes a whole citation field. |

   - Anchors (`find_para`, `old`, `after`) match the **visible** text,
     citation results like `(Smith, 2020)` included. `find_para` raises
     on several matches — lengthen the text or pass `occurrence=k`
     (1-based); the text methods take `occurrence=` too.
   - Pieces are `'text'`, `('i', 'italic')`, or a `citer.cite(...)` result.
   - The first argument of `cite` is placeholder text that Zotero's
     refresh replaces; write it in the style of the document's existing
     citations, including the locator form (`p. 7`).
   For items not yet cited in the document, first call
   `citer.add_item(key, zotero_json_data)`. Take creators from the JSON:
   the markdown view of an item lists editors as authors.
5. **Check.** `uv run $R/check_docx.py unpacked out.docx --original 00_original.docx --author "Surname, Given" --preview preview.md`
   re-zips, checks field balance and duplicate `citationID`s, runs the
   validator if available and writes the accepted-text preview. Read the
   preview around every edit. Exit code 1 means unbalanced fields, a
   change not tracked under the author, or a validation error the
   original does not already have — stop and fix. Errors the original
   shares are printed as ignored.
6. **Save in stages** (skip if the user wants the output kept separate).
   - Copy `out.docx` over the manuscript only after confirming the file
     on disk still equals the last snapshot; a co-author may have saved.
   - In synced folders, wait 2–3 minutes between saves so each stage
     becomes its own version, and keep an edit log mapping save times to
     content.
7. **Hand over.** Tell the user to run Zotero's Document Preferences /
   Refresh on a **copy** first, after fixing any Zotero metadata problems
   (refresh rewrites citations from the library).

## Rules

- Never retype a citation as plain text in a sentence that has Zotero
  fields; insert a field. A bare page locator after an author already
  named in the sentence ("(p. 12)") may be plain text.
- Never rewrite another author's tracked change. To remove their
  inserted text the library nests your `w:del` inside their `w:ins`;
  their deletions are left alone.
- Leave co-authors' notes and comments unless the user says otherwise.
- Hypotheses, statistics and other empirical content are out of scope
  unless the user explicitly asks; numbers follow `empirical-integrity`.

## Pitfalls (each caused a real failure)

| Symptom | Cause | What the library does |
|---|---|---|
| Citation deleted or broken by a text edit | Run merging fused `w:t`, `w:fldChar`, `w:instrText` into one run | `normalize_runs` splits such runs before every edit; never merge runs |
| New text lands after the rest of a co-author's sentence | `w:ins` cannot nest | Splits their `w:ins` so text stays in reading order |
| Own fresh insertion shows as deleted + reinserted | — | Same author **and** timestamp ⇒ edited in place; earlier own insertions get a visible deletion |
| Schema validation fails on a deleted paragraph | `w:del` before `w:ins` in `w:pPr/w:rPr` | Inserts `w:del` after `w:ins` |
| Inserted text becomes part of a citation | Anchor ended inside a field result | Moves the anchor past the field's `end` run |
| Preview shows empty headings/bullets or drops a paragraph | Pandoc artefact for deleted paragraphs and for runs mixing text and field characters | Nothing to fix — check the XML or Word before "fixing" |
| Validator reports unreferenced parts etc. | Already present in the original | `check_docx.py` validates the original and ignores errors it shares |

## Limits

- `replace_text` cannot span a field boundary: `delete_field`, then
  insert a new field.
- `move_paragraph` is delete + insert, not Word's native move tracking.
- No comment support — use the `docx` skill's comment tooling.
- Zotero refresh behaviour for fields inside tracked changes is
  untested. See `references/zotero-fields.md` for the field structure and
  what is verified versus assumed.
