"""Tracked-change editing of word/document.xml (OOXML) with lxml.

Every edit is wrapped in w:ins / w:del attributed to one author, so the
result passes a tracked-change validator and other authors' pending
changes are preserved.

Typical use:

    from redline import Doc
    d = Doc('unpacked/word/document.xml', author='Surname, Given')
    p = d.find_para('unique visible text of the paragraph')
    d.replace_text(p, 'old wording', ['new wording'])
    d.save()

Pieces passed to the editing methods are a list of:
    'plain text' | ('i', 'italic text') | ('cite', (instr, result))
where ('cite', ...) comes from zotero_fields.Citer.cite().

Design notes (lessons from real manuscripts):
- Do NOT run a run-merging script first: merging can fuse field characters
  (w:fldChar / w:instrText) into the same run as text, after which text
  edits corrupt citation fields. Instead, methods call normalize_runs() on
  the paragraph they touch, which splits multi-content runs.
- "Own" insertions (safe to edit in place) are identified by author AND
  timestamp of the current Doc instance. Earlier insertions by the same
  author are treated like anyone else's (tracked deletion), which keeps a
  visible trail.
- Replacing text inside another author's w:ins splits that w:ins so the
  new insertion lands in reading order (w:ins cannot nest).
- In a paragraph mark's rPr, w:ins must precede w:del (schema order).
- Insertions anchored inside a field result are moved after the field end.
"""
import copy
import datetime
import random
import re
import string

from lxml import etree

WNS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
W = f'{{{WNS}}}'
XMLSPACE = '{http://www.w3.org/XML/1998/namespace}space'


def _in_del(el):
    return any(a.tag == W + 'del' for a in el.iterancestors())


def new_citation_id():
    return ''.join(random.choice(string.ascii_letters) for _ in range(8))


class Doc:
    def __init__(self, path, author, date=None):
        self.path = path
        self.author = author
        self.tree = etree.parse(path)
        self.root = self.tree.getroot()
        self.body = self.root.find(W + 'body')
        ids = [int(v) for e in self.root.iter() for v in [e.get(W + 'id')]
               if v is not None and v.lstrip('-').isdigit()]
        self._next = max(ids + [0]) + 10000
        self.date = date or datetime.datetime.now(datetime.UTC).strftime('%Y-%m-%dT%H:%M:%SZ')

    # ---------------- basics ----------------
    def nid(self):
        self._next += 1
        return str(self._next)

    def save(self, path=None):
        self.tree.write(path or self.path, xml_declaration=True, encoding='UTF-8', standalone=True)

    def mark(self, tag):
        el = etree.Element(W + tag)
        el.set(W + 'id', self.nid())
        el.set(W + 'author', self.author)
        el.set(W + 'date', self.date)
        return el

    def paras(self):
        return list(self.body.iter(W + 'p'))

    @staticmethod
    def visible_text(p):
        """Paragraph text as it reads with all tracked changes accepted."""
        return ''.join(t.text or '' for t in p.iter(W + 't') if not _in_del(t))

    def find_para(self, needle, start=0, occurrence=None):
        """The paragraph at index >= start whose visible text contains `needle`.

        Without `occurrence`, exactly one paragraph must match: silently taking
        the first would put an edit in the wrong paragraph. `occurrence=k`
        (1-based, as in replace_text) picks the k-th match instead.
        """
        hits = [(i, p) for i, p in enumerate(self.paras())
                if i >= start and needle in self.visible_text(p)]
        if not hits:
            raise ValueError('paragraph not found: ' + needle[:80])
        if occurrence is not None:
            if not 1 <= occurrence <= len(hits):
                raise ValueError(f'{needle[:80]!r}: occurrence {occurrence} of {len(hits)}')
            return hits[occurrence - 1][1]
        if len(hits) > 1:
            raise ValueError(f'ambiguous: {needle[:80]!r} matches paragraphs '
                             f'{[i for i, _ in hits]}; lengthen the text or pass occurrence=')
        return hits[0][1]

    def _own(self, r):
        par = r.getparent()
        return (par is not None and par.tag == W + 'ins'
                and par.get(W + 'author') == self.author and par.get(W + 'date') == self.date)

    # ---------------- run builders ----------------
    @staticmethod
    def base_rpr(p):
        for r in p.iter(W + 'r'):
            rpr = r.find(W + 'rPr')
            if rpr is not None:
                rpr = copy.deepcopy(rpr)
                for bad in ('ins', 'del', 'rStyle', 'b', 'i', 'highlight', 'noProof'):
                    for x in rpr.findall(W + bad):
                        rpr.remove(x)
                return rpr
        return etree.Element(W + 'rPr')

    @staticmethod
    def run(text, rpr, italic=False):
        r = etree.Element(W + 'r')
        rp = copy.deepcopy(rpr) if rpr is not None else etree.Element(W + 'rPr')
        if italic:
            rp.insert(0, etree.Element(W + 'i'))
        if len(rp):
            r.append(rp)
        t = etree.SubElement(r, W + 't')
        t.text = text
        t.set(XMLSPACE, 'preserve')
        return r

    def field_runs(self, instr, result, rpr):
        def fc(kind):
            r = etree.Element(W + 'r')
            r.append(copy.deepcopy(rpr))
            f = etree.SubElement(r, W + 'fldChar')
            f.set(W + 'fldCharType', kind)
            return r
        r = etree.Element(W + 'r')
        r.append(copy.deepcopy(rpr))
        it = etree.SubElement(r, W + 'instrText')
        it.set(XMLSPACE, 'preserve')
        it.text = instr
        return [fc('begin'), r, fc('separate'), self.run(result, rpr), fc('end')]

    def ins_runs(self, pieces, rpr):
        rpr = rpr if rpr is not None else etree.Element(W + 'rPr')
        ins = self.mark('ins')
        for pc in pieces:
            if isinstance(pc, str):
                ins.append(self.run(pc, rpr))
            elif pc[0] == 'i':
                ins.append(self.run(pc[1], rpr, italic=True))
            elif pc[0] == 'cite':
                for r in self.field_runs(pc[1][0], pc[1][1], rpr):
                    ins.append(r)
            else:
                raise ValueError(f'unknown piece: {pc!r}')
        return ins

    @staticmethod
    def normalize_runs(p):
        """Split runs holding several content children into one run per child."""
        for r in list(p.iter(W + 'r')):
            kids = [k for k in r if k.tag != W + 'rPr']
            if len(kids) <= 1:
                continue
            rpr = r.find(W + 'rPr')
            for k in kids:
                nr = etree.Element(W + 'r')
                for a, v in r.attrib.items():
                    nr.set(a, v)
                if rpr is not None:
                    nr.append(copy.deepcopy(rpr))
                nr.append(k)
                r.addprevious(nr)
            r.getparent().remove(r)

    # ---------------- low-level tracked operations ----------------
    def _del_run(self, r):
        for t in r.findall(W + 't'):
            t.tag = W + 'delText'
        for t in r.findall(W + 'instrText'):
            t.tag = W + 'delInstrText'
        d = self.mark('del')
        r.addprevious(d)
        d.append(r)

    def _place_after(self, node, ins):
        """Insert `ins` after `node`, splitting a foreign w:ins container if needed."""
        cur = node
        if cur.getparent() is not None and cur.getparent().tag == W + 'del':
            cur = cur.getparent()
        parent = cur.getparent()
        if parent is not None and parent.tag == W + 'ins':
            following = list(cur.itersiblings())
            parent.addnext(ins)
            if following:
                tail = etree.Element(W + 'ins')
                for a, v in parent.attrib.items():
                    tail.set(a, v)
                tail.set(W + 'id', self.nid())
                for f in following:
                    tail.append(f)
                ins.addnext(tail)
        else:
            cur.addnext(ins)

    def _visible_segments(self, p):
        return [t for t in p.iter(W + 't') if not _in_del(t)]

    def _split_at(self, p, start, end):
        """Split runs so that visible chars [start, end) form whole runs; return those runs."""
        segs = self._visible_segments(p)
        offset, targets = 0, []
        for t in segs:
            txt = t.text or ''
            a, b = offset, offset + len(txt)
            offset = b
            if b <= start or a >= end:
                continue
            r = t.getparent()
            lo, hi = max(start, a) - a, min(end, b) - a
            pre, mid, post = txt[:lo], txt[lo:hi], txt[hi:]
            for piece, where in ((pre, 'before'), (post, 'after')):
                if piece:
                    rc = copy.deepcopy(r)
                    rc.find(W + 't').text = piece
                    rc.find(W + 't').set(XMLSPACE, 'preserve')
                    (r.addprevious if where == 'before' else r.addnext)(rc)
            t.text = mid
            t.set(XMLSPACE, 'preserve')
            targets.append(r)
        return targets

    def _find_offset(self, p, needle, occurrence):
        full = ''.join(t.text or '' for t in self._visible_segments(p))
        pos, idx = 0, -1
        for _ in range(occurrence):
            idx = full.find(needle, pos)
            if idx < 0:
                raise ValueError('text not found: ' + needle[:80])
            pos = idx + 1
        return idx

    def _field_end_if_inside(self, p, r):
        runs = [x for x in p.iter(W + 'r') if not _in_del(x)]
        depth, inside = 0, False
        for x in runs:
            fc = x.find(W + 'fldChar')
            if fc is not None and fc.get(W + 'fldCharType') == 'begin':
                depth += 1
            if x is r and depth > 0:
                inside = True
            if fc is not None and fc.get(W + 'fldCharType') == 'end':
                depth -= 1
                if inside and depth == 0:
                    return x
        return r

    # ---------------- public editing API ----------------
    def replace_text(self, p, old, new_pieces, occurrence=1):
        """Tracked replacement of visible text. `old` must not cross a field boundary.
        new_pieces may be [] for a pure deletion."""
        self.normalize_runs(p)
        start = self._find_offset(p, old, occurrence)
        targets = self._split_at(p, start, start + len(old))
        if not targets:
            raise ValueError('nothing to replace')
        rpr = targets[0].find(W + 'rPr')
        rpr = copy.deepcopy(rpr) if rpr is not None else etree.Element(W + 'rPr')
        last = targets[-1]
        if new_pieces:
            ins = self.ins_runs(new_pieces, rpr)
            if self._own(last):
                pos = last
                for child in list(ins):
                    pos.addnext(child)
                    pos = child
            else:
                self._place_after(last, ins)
        for r in targets:
            if self._own(r):
                r.getparent().remove(r)
            else:
                self._del_run(r)

    def insert_text_after(self, p, after, pieces, occurrence=1):
        """Tracked insertion right after visible text `after` (moved past a field end if needed)."""
        self.normalize_runs(p)
        idx = self._find_offset(p, after, occurrence)
        cut = idx + len(after)
        offset = 0
        for t in self._visible_segments(p):
            txt = t.text or ''
            a, b = offset, offset + len(txt)
            offset = b
            if a < cut <= b:
                r = t.getparent()
                if cut < b:
                    rc = copy.deepcopy(r)
                    rc.find(W + 't').text = txt[cut - a:]
                    rc.find(W + 't').set(XMLSPACE, 'preserve')
                    r.addnext(rc)
                    t.text = txt[:cut - a]
                    t.set(XMLSPACE, 'preserve')
                anchor = self._field_end_if_inside(p, r)
                ins = self.ins_runs(pieces, r.find(W + 'rPr'))
                if self._own(anchor):
                    pos = anchor
                    for child in list(ins):
                        pos.addnext(child)
                        pos = child
                else:
                    self._place_after(anchor, ins)
                return ins
        raise ValueError('cut point not located')

    def insert_paragraph_after(self, anchor, pieces, style=None, template=None):
        """Insert a new tracked paragraph after `anchor` (paragraph mark tracked too)."""
        template = template if template is not None else anchor
        p = etree.Element(W + 'p')
        ppr = etree.SubElement(p, W + 'pPr')
        tppr = template.find(W + 'pPr')
        sty = style
        if sty is None and tppr is not None and tppr.find(W + 'pStyle') is not None:
            sty = tppr.find(W + 'pStyle').get(W + 'val')
        if sty:
            etree.SubElement(ppr, W + 'pStyle').set(W + 'val', sty)
        etree.SubElement(ppr, W + 'rPr').append(self.mark('ins'))
        p.append(self.ins_runs(pieces, self.base_rpr(template)))
        anchor.addnext(p)
        return p

    def delete_paragraph(self, p, merge_mark=True):
        """Tracked deletion of all visible content and (optionally) the paragraph mark."""
        self.normalize_runs(p)
        for r in list(p.iter(W + 'r')):
            if _in_del(r):
                continue
            if all(r.find(W + k) is None for k in ('t', 'instrText', 'fldChar', 'tab')):
                continue
            if self._own(r):
                r.getparent().remove(r)
            else:
                self._del_run(r)
        if merge_mark:
            ppr = p.find(W + 'pPr')
            if ppr is None:
                ppr = etree.Element(W + 'pPr')
                p.insert(0, ppr)
            rpr = ppr.find(W + 'rPr')
            if rpr is None:
                rpr = etree.SubElement(ppr, W + 'rPr')
            if rpr.find(W + 'del') is None:
                ins = rpr.find(W + 'ins')
                pos = list(rpr).index(ins) + 1 if ins is not None else 0
                rpr.insert(pos, self.mark('del'))

    def move_paragraph(self, src, after, style=None):
        """Tracked move as delete + insert. Copies visible content (fields included,
        foreign insertions accepted, deletions dropped); copied Zotero fields get new
        citationIDs so the citation processor does not see duplicates."""
        self.normalize_runs(src)
        p = etree.Element(W + 'p')
        sppr = src.find(W + 'pPr')
        ppr = copy.deepcopy(sppr) if sppr is not None else etree.Element(W + 'pPr')
        rpr = ppr.find(W + 'rPr')
        if rpr is None:
            rpr = etree.SubElement(ppr, W + 'rPr')
        for x in list(rpr):
            if x.tag in (W + 'ins', W + 'del'):
                rpr.remove(x)
        rpr.insert(0, self.mark('ins'))
        if style:
            ps = ppr.find(W + 'pStyle')
            if ps is None:
                ps = etree.Element(W + 'pStyle')
                ppr.insert(0, ps)
            ps.set(W + 'val', style)
        p.append(ppr)
        ins = self.mark('ins')
        for r in src.iter(W + 'r'):
            if _in_del(r):
                continue
            rc = copy.deepcopy(r)
            for it in rc.findall(W + 'instrText'):
                if it.text and 'CSL_CITATION' in it.text:
                    it.text = re.sub(r'"citationID":"[^"]+"', f'"citationID":"{new_citation_id()}"', it.text, count=1)
            ins.append(rc)
        p.append(ins)
        after.addnext(p)
        self.delete_paragraph(src)
        return p

    def delete_field(self, p, result_text):
        """Tracked deletion of the whole field (begin..end) whose result contains result_text."""
        self.normalize_runs(p)
        runs = [x for x in p.iter(W + 'r') if not _in_del(x)]
        i = 0
        while i < len(runs):
            fc = runs[i].find(W + 'fldChar')
            if fc is not None and fc.get(W + 'fldCharType') == 'begin':
                j, depth, txt = i, 0, ''
                while j < len(runs):
                    f2 = runs[j].find(W + 'fldChar')
                    if f2 is not None and f2.get(W + 'fldCharType') == 'begin':
                        depth += 1
                    t = runs[j].find(W + 't')
                    if t is not None:
                        txt += t.text or ''
                    if f2 is not None and f2.get(W + 'fldCharType') == 'end':
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                if result_text in txt:
                    for r in runs[i:j + 1]:
                        if self._own(r):
                            r.getparent().remove(r)
                        else:
                            self._del_run(r)
                    return
                i = j + 1
            else:
                i += 1
        raise ValueError('field not found: ' + result_text)
