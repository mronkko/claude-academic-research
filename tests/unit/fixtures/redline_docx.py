"""Minimal .docx fixture with the structures that broke naive tracked-change editing:
a foreign tracked insertion, a Zotero citation field, a run that holds text and
field characters together, and a paragraph whose mark is a foreign insertion."""
import json
import os
import zipfile

W_NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'

CT = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
      '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
      '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
      '<Default Extension="xml" ContentType="application/xml"/>'
      '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
      '</Types>')
RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        '</Relationships>')
DOC_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>')

CSL = {'citationID': 'AbCdEfGh',
       'properties': {'formattedCitation': '(Smith, 2020)', 'plainCitation': '(Smith, 2020)', 'noteIndex': 0},
       'citationItems': [{'id': 1, 'uris': ['http://zotero.org/groups/1/items/AAAA1111'],
                          'itemData': {'id': 1, 'type': 'article-journal', 'title': 'T',
                                       'author': [{'family': 'Smith', 'given': 'J'}],
                                       'issued': {'date-parts': [['2020']]}}}],
       'schema': 'https://github.com/citation-style-language/schema/raw/master/csl-citation.json'}
INSTR = ' ADDIN ZOTERO_ITEM CSL_CITATION ' + json.dumps(CSL).replace('&', '&amp;').replace('<', '&lt;') + ' '


def r(t):
    return f'<w:r><w:t xml:space="preserve">{t}</w:t></w:r>'


def field():
    return ('<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
            f'<w:r><w:instrText xml:space="preserve">{INSTR}</w:instrText></w:r>'
            '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
            '<w:r><w:t>(Smith, 2020)</w:t></w:r>'
            '<w:r><w:fldChar w:fldCharType="end"/></w:r>')


OTHER = 'w:author="Other, Author" w:date="2026-01-01T00:00:00Z"'

BODY = (
    '<w:p>' + r('Alpha beta gamma delta.') + '</w:p>'
    f'<w:p><w:ins w:id="1" {OTHER}>' + r('Inserted by other author, with more words after it.') + '</w:ins></w:p>'
    '<w:p>' + r('Before ') + field() + r('. After the citation.') + '</w:p>'
    # run that holds text and field characters together (as produced by run merging)
    '<w:p><w:r><w:t xml:space="preserve">Merged run text </w:t><w:fldChar w:fldCharType="begin"/>'
    f'<w:instrText xml:space="preserve">{INSTR}</w:instrText><w:fldChar w:fldCharType="separate"/>'
    '<w:t>(Smith, 2020)</w:t><w:fldChar w:fldCharType="end"/><w:t xml:space="preserve"> tail.</w:t></w:r></w:p>' +
    f'<w:p><w:pPr><w:rPr><w:ins w:id="2" {OTHER}/></w:rPr></w:pPr><w:ins w:id="3" {OTHER}>' + r('Foreign paragraph.') + '</w:ins></w:p>'
    '<w:p>' + r('Target paragraph.') + '</w:p>'
)
DOC = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
       f'<w:document xmlns:w="{W_NS}"><w:body>{BODY}<w:sectPr/></w:body></w:document>')


def build(dirpath):
    """Write the unpacked fixture to dirpath and a zipped copy to dirpath + '.docx'."""
    os.makedirs(os.path.join(dirpath, '_rels'), exist_ok=True)
    os.makedirs(os.path.join(dirpath, 'word', '_rels'), exist_ok=True)
    files = {'[Content_Types].xml': CT, '_rels/.rels': RELS,
             'word/_rels/document.xml.rels': DOC_RELS, 'word/document.xml': DOC}
    for name, content in files.items():
        with open(os.path.join(dirpath, name), 'w', encoding='utf8') as f:
            f.write(content)
    with zipfile.ZipFile(dirpath + '.docx', 'w', zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)
    return os.path.join(dirpath, 'word', 'document.xml')
