# Zotero citation fields in Word documents

## Structure (verified against fields created by the Zotero Word plugin)

A citation is a complex field spread over five runs:

```
<w:r><w:fldChar w:fldCharType="begin"/></w:r>
<w:r><w:instrText xml:space="preserve"> ADDIN ZOTERO_ITEM CSL_CITATION {JSON} </w:instrText></w:r>
<w:r><w:fldChar w:fldCharType="separate"/></w:r>
<w:r><w:t>(Author, 2020)</w:t></w:r>
<w:r><w:fldChar w:fldCharType="end"/></w:r>
```

JSON keys observed:
- `citationID`: random string, unique per citation.
- `properties`: `formattedCitation`, `plainCitation` and `noteIndex`.
- `citationItems[]`, each with:
  - `id`: the local numeric item ID;
  - `uris`: e.g. `http://zotero.org/groups/<id>/items/<KEY>` or `http://zotero.org/users/<id>/items/<KEY>`;
  - `itemData`: CSL-JSON;
  - optional `locator` and `label`.
- `schema`.

The bibliography is a single `ADDIN ZOTERO_BIBL` field, regenerated on refresh. A document may also contain `ADDIN ZOTERO_TEMP` fields, which are not yet processed.

## Assumptions (not verified against plugin source)
- On refresh, the plugin resolves items by `uris`. The numeric `id` written for new items (e.g. 900000+) is only a local key.
- CSL `suppress-author` and `prefix` on citation items are honoured, which allows "Author et al. (2020)" style citations.
- The formatted text written into the field result is a placeholder that refresh replaces.
- Behaviour of refresh when fields sit inside tracked insertions or deletions has not been tested. Advise refreshing a copy first and, if problems appear, accepting the tracked changes on a copy before refreshing.

## Practical consequences
- Errors in Zotero item metadata reappear after refresh; fix the records first. Common ones: editors stored as authors (check `creatorType` in the JSON view), online-first dates, item type (chapter vs article), and pages fields holding article IDs.
- Duplicate Zotero records cited in different places produce duplicate bibliography entries. Merge them in Zotero.
- Moving a paragraph copies its fields. `move_paragraph` gives copies new `citationID`s so the plugin does not see two citations with the same ID.
