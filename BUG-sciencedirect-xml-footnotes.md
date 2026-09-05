# Bug: `_extract_xml_body` splices footnote text into the middle of sentences

**Component:** `scripts/pipelines/fetchers/sciencedirect.py` → `_extract_xml_body()`
**Affects:** every PDF produced by the Elsevier XML fallback
(`[elsevier] render_xml_to_pdf = true`, files ending `-tdm-recovered`)
**Found:** 2026-08-20, plugin 0.14.0, by the AI-literature-review study's Phase 2
**Severity:** high for downstream text analysis — the recovered text is not
readable prose, and nothing downstream can tell

---

## Summary

`_extract_xml_body()` walks the whole `<body>` subtree and concatenates every
text node in document order. Elsevier's XML places each `<ce:footnote>` **at the
point its marker appears**, so the walk splices the entire footnote — citation,
URL, access date — between a clause and its continuation.

For a footnote-heavy article this is not a cosmetic problem. In the article
below, **35% of the extracted text is footnote material interleaved into the
body**, and every affected sentence is broken in two.

## Reproduction

```python
raw = open("wachter_2021_tdm_raw.xml", "rb").read()   # 10.1016/j.clsr.2021.105567
body = _extract_xml_body(raw)                          # 192,450 chars
i = body.find("focus on the four non-discrimination directives")
print(body[i-80:i+420])
```

**Actual** — footnote 41 lands inside the sentence it annotates:

> …In this paper we will focus on the four non-discrimination directives of the
> EU : 41 the Racial Equality Directive (2000/43/EC), **41 European Commission,
> 'Non-Discrimination' ( European Commission - European Commission , 2020) <
> https://ec.europa.eu/info/aid-development-cooperation-fundamental-rights/your-rights-eu/know-your-rights/equality/non-discrimination_en
> > accessed 2 March 2020.** 42 the Gender Equality Directive (recast)
> (2006/54/EC…

**Expected** — the same passage with `<footnote>` subtrees skipped:

> …In this paper we will focus on the four non-discrimination directives of the
> EU : 41 the Racial Equality Directive (2000/43/EC), 42 the Gender Equality
> Directive (recast) (2006/54/EC), 43 the Gender Access Directive (2004/113/EC),
> 44 and the Employment Directive (2000/78/EC). 45 The scope of groups and
> sectors protected varies across these four directives…

## Scale, on one article

| | chars |
|---|---|
| `_extract_xml_body()` today | 192,450 |
| with `<footnote>` subtrees skipped | 124,771 |
| **footnote material interleaved into the body** | **67,679 (35%)** |

Wachter, Mittelstadt & Russell (2021), *Computer Law & Security Review* — a law
review article carrying **302 `<ce:footnote>` elements**. The raw XML is
attached to the report this came from; re-fetch with:

```
curl -H "X-ELS-APIKey: $KEY" -H "Accept: text/xml" \
  https://api.elsevier.com/content/article/doi/10.1016/j.clsr.2021.105567
```

## Cause

```python
    parts: list[str] = []
    for el in body.iter():                 # <-- visits footnote subtrees too
        if el.text and el.text.strip():
            parts.append(el.text.strip())
        if el.tail and el.tail.strip():
            parts.append(el.tail.strip())
    return " ".join(parts)
```

`Element.iter()` is a document-order descendant walk with no structural
filtering. It cannot distinguish body prose from an annotation attached to it.

## Why it is worth fixing rather than tolerating

The structure needed is **already in the XML** — footnotes are explicit
`<ce:footnote>` elements each wrapping a `<ce:note-para>`, not inline text that
would have to be inferred from position or styling. One selector separates them.

It is also invisible downstream. The recovered PDF is well-formed, passes size
and text-length checks, and reads as a normal article to anything that does not
look closely. In the study that found this, **121 of 130 open PDF-identity
questions were `tdm-recovered` files** whose title-similarity scores were
depressed by exactly this — the front matter is polluted before the title check
runs, so a correct file scores like a wrong one. Two articles were classified as
the wrong article on that basis; both were correct.

## Suggested fix

Skip footnote subtrees during the walk, keeping their tails so surrounding prose
stays joined:

```python
def _text_of(el, skip=("footnote",)):
    parts = []
    if el.text and el.text.strip():
        parts.append(el.text.strip())
    for child in el:
        if child.tag not in skip:
            parts.extend(_text_of(child, skip))
        if child.tail and child.tail.strip():   # keep the tail either way
            parts.append(child.tail.strip())
    return parts
```

Three questions worth a deliberate answer rather than a default, since they
change what downstream analysis sees:

1. **Drop, or relocate?** Dropping loses substantive content — in law reviews
   footnotes often carry the argument. Appending them as an endnote block after
   the body keeps the text without breaking sentences, and is probably the
   better default.
2. **What else should be excluded or relocated?** `<ce:bib-reference>` and
   table/figure content have the same shape of problem.
3. **Should the recovered PDF say what was done?** A one-line note on the first
   page ("footnotes moved to the end", "footnotes omitted") would make the
   transformation auditable from the file, which matters when the file is the
   only artifact a later reader has.

Whatever is chosen, a marker in the rendered PDF or the filename would let
downstream consumers tell old recoveries from new ones — re-fetching is the only
remedy for files already produced, and there is currently no way to tell them
apart.

## Downstream note

The AI-literature-review study will re-fetch its `tdm-recovered` corpus once
this lands. It has 121 such items identified and tagged
(`pdf-review:generated-text` in its Zotero library), so a fix can be verified
against a real, footnote-heavy sample rather than a synthetic one.
