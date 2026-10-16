"""Build Zotero (Word plugin) citation fields for use with redline.Doc.

A Zotero citation in Word is a complex field:
    begin | instrText " ADDIN ZOTERO_ITEM CSL_CITATION {json} " | separate | result text | end
The JSON holds citationID, properties.formattedCitation/plainCitation, and
citationItems[] with id, uris (Zotero web URI of the item), itemData (CSL-JSON)
and optional locator/label/prefix/suppress-author. On "Refresh", the plugin looks
items up by URI and regenerates the visible text and the bibliography
(ADDIN ZOTERO_BIBL), so the formatted text written here is a placeholder.

Verified: this structure matches fields created by the Zotero Word plugin in
real manuscripts. Assumed (not verified against plugin source): the numeric
`id` of new items can be any unique number; `suppress-author` is honoured.
Always test a refresh on a copy before relying on inserted fields.
"""
import json
import random
import re
import string

SCHEMA = 'https://github.com/citation-style-language/schema/raw/master/csl-citation.json'

_TYPE_MAP = {'journalArticle': 'article-journal', 'bookSection': 'chapter', 'book': 'book',
             'report': 'report', 'webpage': 'webpage', 'thesis': 'thesis',
             'conferencePaper': 'paper-conference', 'document': 'document'}


def item_uri(key, library_id, library_type='groups'):
    """library_type: 'groups' or 'users'."""
    return f'http://zotero.org/{library_type}/{library_id}/items/{key}'


def existing_items(document_xml_text):
    """URI -> (id, itemData) for every item already cited in the document."""
    out = {}
    for m in re.finditer(r'ADDIN ZOTERO_ITEM CSL_CITATION (\{.*?\}) </w:instrText>', document_xml_text, re.S):
        try:
            js = json.loads(m.group(1))
        except ValueError:
            continue
        for ci in js.get('citationItems', []):
            for u in ci.get('uris', []):
                out[u] = (ci.get('id'), ci.get('itemData'))
    return out


def zotero_to_csl(z):
    """Minimal conversion of a Zotero item's `data` (JSON API / MCP json) to CSL-JSON.
    Respects creatorType (author vs editor)."""
    d = {'type': _TYPE_MAP.get(z.get('itemType'), 'article'), 'title': z.get('title', '')}
    for zk, ck in [('publicationTitle', 'container-title'), ('bookTitle', 'container-title'),
                   ('volume', 'volume'), ('issue', 'issue'), ('pages', 'page'), ('DOI', 'DOI'),
                   ('publisher', 'publisher'), ('place', 'publisher-place'), ('URL', 'URL'),
                   ('ISBN', 'ISBN'), ('ISSN', 'ISSN')]:
        if z.get(zk):
            d[ck] = z[zk]
    for c in z.get('creators', []):
        role = 'editor' if c.get('creatorType') == 'editor' else 'author'
        if 'lastName' in c:
            d.setdefault(role, []).append({'family': c['lastName'], 'given': c.get('firstName', '')})
        else:
            d.setdefault(role, []).append({'literal': c.get('name', '')})
    m = re.search(r'(\d{4})', z.get('date', ''))
    if m:
        d['issued'] = {'date-parts': [[m.group(1)]]}
    return d


class Citer:
    """Creates citation pieces for redline.Doc.

    citer = Citer(open(doc_xml, encoding='utf8').read(), library_id='1234567', library_type='groups')
    citer.add_item('ABCD1234', zotero_json_data)   # only for items not yet cited
    piece = citer.cite('(Smith, 2020, p. 5)', citer.item('ABCD1234', locator='5'))
    """

    def __init__(self, document_xml_text, library_id, library_type='groups'):
        self.library_id, self.library_type = library_id, library_type
        self.known = existing_items(document_xml_text)
        self._new = {}
        self._next_id = 900000 + random.randint(0, 9999)

    def add_item(self, key, zotero_data):
        self._next_id += 1
        d = zotero_to_csl(zotero_data)
        d['id'] = self._next_id
        self._new[key] = (self._next_id, d)

    def item(self, key, locator=None, label='page', prefix=None, suppress_author=False):
        uri = item_uri(key, self.library_id, self.library_type)
        if uri in self.known:
            iid, data = self.known[uri]
        elif key in self._new:
            iid, data = self._new[key]
        else:
            hits = [(u, v) for u, v in self.known.items() if u.endswith('/items/' + key)]
            if not hits:
                raise KeyError(f'item {key} not cited in document and not added with add_item()')
            uri, (iid, data) = hits[0]
        ci = {'id': iid, 'uris': [uri], 'itemData': data}
        if locator:
            ci['locator'], ci['label'] = locator, label
        if prefix:
            ci['prefix'] = prefix
        if suppress_author:
            ci['suppress-author'] = True
        return ci

    @staticmethod
    def cite(formatted, *items):
        """Return a ('cite', (instr, result)) piece. `formatted` is placeholder text in the
        document's citation style; Zotero rewrites it on refresh."""
        js = {'citationID': ''.join(random.choice(string.ascii_letters) for _ in range(8)),
              'properties': {'formattedCitation': formatted, 'plainCitation': formatted, 'noteIndex': 0},
              'citationItems': list(items), 'schema': SCHEMA}
        return ('cite', (' ADDIN ZOTERO_ITEM CSL_CITATION ' + json.dumps(js, ensure_ascii=False) + ' ', formatted))
