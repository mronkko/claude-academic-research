# /// script
# requires-python = ">=3.11"
# dependencies = ["lxml>=5.0"]
# ///
"""List paragraphs of word/document.xml with tracked changes marked inline.

Usage: uv run inspect_docx.py document.xml [FROM [TO]] [--width N] [--fields] [--authors]

Output per paragraph: [index] (style) text, where {+...+} is inserted text,
{-...-} deleted text, and [ZFIELD] marks a Zotero citation field. FROM and TO
are paragraph indices (inclusive, 0-based, the same indexing as
redline.Doc.paras()); without them every paragraph is listed.
--fields prints field counts (begin/separate/end must be equal), Zotero
field-type counts and the Zotero libraries cited (the library id Citer needs).
--authors prints the tracked-change authors exactly as recorded.
"""
import argparse
import re

from lxml import etree

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


def para_text(p):
    out = []
    for el in p.iter():
        if el.tag == W + 't':
            ins = any(a.tag == W + 'ins' for a in el.iterancestors())
            out.append('{+' + (el.text or '') + '+}' if ins else (el.text or ''))
        elif el.tag == W + 'delText':
            out.append('{-' + (el.text or '') + '-}')
        elif el.tag == W + 'instrText' and 'ZOTERO_ITEM' in (el.text or ''):
            out.append('[ZFIELD]')
    return ''.join(out).replace('+}{+', '').replace('-}{-', '')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('xml')
    ap.add_argument('range', nargs='*', type=int)
    ap.add_argument('--width', type=int, default=300)
    ap.add_argument('--fields', action='store_true')
    ap.add_argument('--authors', action='store_true')
    a = ap.parse_args()
    if a.fields or a.authors:
        s = open(a.xml, encoding='utf8').read()
    if a.fields:
        for k in ('begin', 'separate', 'end'):
            print(k, s.count(f'w:fldCharType="{k}"'))
        for k, n in sorted({m: s.count(m) for m in set(re.findall(r'ADDIN ZOTERO_[A-Z_]+', s))}.items()):
            print(k, n)
        for lib in sorted(set(re.findall(r'zotero\.org/(?:groups|users)/\d+', s))):
            print('library', lib)
    if a.authors:
        root = etree.fromstring(s.encode('utf8'))
        counts = {}
        for el in root.iter(W + 'ins', W + 'del'):
            key = (el.get(W + 'author'), el.tag[len(W):])
            counts[key] = counts.get(key, 0) + 1
        for (author, kind), n in sorted(counts.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
            print(f'{author!r} {kind} {n}')
    if a.fields or a.authors:
        return
    lo = a.range[0] if a.range else 0
    hi = a.range[1] if len(a.range) > 1 else 10 ** 9
    body = etree.parse(a.xml).getroot().find(W + 'body')
    for i, p in enumerate(body.iter(W + 'p')):
        if lo <= i <= hi:
            st = p.find(W + 'pPr/' + W + 'pStyle')
            style = st.get(W + 'val') if st is not None else ''
            print(f'[{i}] ({style}) {para_text(p)[:a.width]}')


if __name__ == '__main__':
    main()
