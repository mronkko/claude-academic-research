"""Tracked-change editing for the word-zotero-track-changes skill (scripts/track_changes/).

The fixture reproduces the structures that broke naive editing of a real
co-authored manuscript: a foreign tracked insertion, a Zotero field, a run
holding text and field characters together, and a foreign paragraph mark.
"""

import json
import os
import sys
import zipfile
from pathlib import Path

import pytest
from lxml import etree

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "track_changes"))
sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

from redline import Doc, W  # noqa: E402
from redline_docx import build  # noqa: E402
from zotero_fields import Citer, zotero_to_csl  # noqa: E402

AUTHOR = 'Tester, Tina'


@pytest.fixture
def doc(tmp_path):
    xml = build(str(tmp_path / 'fx'))
    return Doc(xml, author=AUTHOR, date='2026-02-02T00:00:00Z')


def reload(d):
    d.save()
    return Doc(d.path, author=AUTHOR, date=d.date)


def fields_balanced(d):
    s = etree.tostring(d.root).decode()
    return len({s.count(f'fldCharType="{k}"') for k in ('begin', 'separate', 'end')}) == 1


def test_replace_plain(doc):
    p = doc.find_para('Alpha beta')
    doc.replace_text(p, 'beta', ['BETA'])
    assert Doc.visible_text(p) == 'Alpha BETA gamma delta.'
    assert p.find('.//' + W + 'del').get(W + 'author') == AUTHOR


def test_replace_inside_foreign_insertion_keeps_reading_order(doc):
    p = doc.find_para('Inserted by other author')
    doc.replace_text(p, 'other author', ['someone'])
    assert Doc.visible_text(p) == 'Inserted by someone, with more words after it.'
    # no nested w:ins
    assert not [i for i in p.iter(W + 'ins') if any(a.tag == W + 'ins' for a in i.iterancestors())]


def test_replace_inside_own_insertion_keeps_new_text(doc):
    p = doc.find_para('Target paragraph')
    new = doc.insert_paragraph_after(p, ['Fresh text here.'])
    doc.replace_text(new, 'text', ['words'])
    assert Doc.visible_text(new) == 'Fresh words here.'
    assert new.find('.//' + W + 'del') is None  # own insertion edited in place


def test_insert_after_field_lands_after_field_end(doc):
    p = doc.find_para('Before (Smith, 2020)')
    doc.insert_text_after(p, 'Before (Smith, 2020)', [' INSERTED'])
    assert Doc.visible_text(p) == 'Before (Smith, 2020) INSERTED. After the citation.'
    assert fields_balanced(doc)
    # the insertion must follow the field's end run
    kinds = [(r.find(W + 'fldChar').get(W + 'fldCharType') if r.find(W + 'fldChar') is not None else
              (r.findtext(W + 't') or '')) for r in p.iter(W + 'r')]
    assert kinds.index('end') < kinds.index(' INSERTED')


def test_merged_run_is_split_before_editing(doc):
    p = doc.find_para('Merged run text')
    doc.replace_text(p, 'tail', ['end'])
    assert Doc.visible_text(p) == 'Merged run text (Smith, 2020) end.'
    assert fields_balanced(doc)


def test_delete_foreign_paragraph_mark_order(doc):
    p = doc.find_para('Foreign paragraph')
    doc.delete_paragraph(p)
    rpr = p.find(W + 'pPr/' + W + 'rPr')
    tags = [c.tag for c in rpr]
    assert tags.index(W + 'ins') < tags.index(W + 'del')  # schema order
    assert Doc.visible_text(p) == ''


def test_delete_field(doc):
    p = doc.find_para('Before (Smith, 2020)')
    doc.delete_field(p, 'Smith, 2020')
    assert Doc.visible_text(p) == 'Before . After the citation.'
    assert p.find('.//' + W + 'delInstrText') is not None
    assert fields_balanced(doc)


def test_move_paragraph_renews_citation_id(doc):
    src = doc.find_para('Before (Smith, 2020)')
    tgt = doc.find_para('Target paragraph')
    new = doc.move_paragraph(src, tgt)
    assert Doc.visible_text(new) == 'Before (Smith, 2020). After the citation.'
    assert Doc.visible_text(src) == ''
    moved_instr = new.find('.//' + W + 'instrText').text
    assert '"citationID":"AbCdEfGh"' not in moved_instr


def test_insert_paragraph_with_citation_roundtrip(doc):
    citer = Citer(etree.tostring(doc.root, encoding='unicode'), library_id='1')
    citer.add_item('BBBB2222', {'itemType': 'journalArticle', 'title': 'New', 'date': '2021',
                                'creators': [{'creatorType': 'author', 'firstName': 'A', 'lastName': 'Jones'},
                                             {'creatorType': 'editor', 'firstName': 'E', 'lastName': 'Ed'}]})
    piece = citer.cite('(Jones, 2021, p. 4; Smith, 2020)',
                       citer.item('BBBB2222', locator='4'), citer.item('AAAA1111'))
    p = doc.find_para('Target paragraph')
    doc.insert_paragraph_after(p, ['As shown ', piece, '.'])
    d2 = reload(doc)
    q = d2.find_para('As shown')
    assert Doc.visible_text(q) == 'As shown (Jones, 2021, p. 4; Smith, 2020).'
    instr = q.find('.//' + W + 'instrText').text
    js = json.loads(instr.split('CSL_CITATION', 1)[1])
    uris = [ci['uris'][0] for ci in js['citationItems']]
    assert uris == ['http://zotero.org/groups/1/items/BBBB2222', 'http://zotero.org/groups/1/items/AAAA1111']
    assert js['citationItems'][0]['locator'] == '4'
    assert fields_balanced(d2)


def test_zotero_to_csl_respects_editors():
    d = zotero_to_csl({'itemType': 'bookSection', 'title': 'C', 'date': '2019',
                       'creators': [{'creatorType': 'editor', 'firstName': 'E', 'lastName': 'Editor'},
                                    {'creatorType': 'author', 'firstName': 'A', 'lastName': 'Author'}]})
    assert d['author'] == [{'family': 'Author', 'given': 'A'}]
    assert d['editor'] == [{'family': 'Editor', 'given': 'E'}]
    assert d['type'] == 'chapter'


def test_unknown_item_raises(doc):
    citer = Citer(etree.tostring(doc.root, encoding='unicode'), library_id='1')
    with pytest.raises(KeyError):
        citer.item('ZZZZ9999')


# ---------------- CLI helpers ----------------

def test_unpack_snapshots_and_refuses_reuse(tmp_path):
    import unpack_docx

    build(str(tmp_path / 'fx'))
    snap, out = unpack_docx.unpack(tmp_path / 'fx.docx', tmp_path / 'work')
    assert snap.read_bytes() == (tmp_path / 'fx.docx').read_bytes()
    assert (out / 'word' / 'document.xml').is_file()
    with pytest.raises(SystemExit):
        unpack_docx.unpack(tmp_path / 'fx.docx', tmp_path / 'work')


def test_check_docx_flags_unbalanced_fields(tmp_path, monkeypatch, capsys):
    import check_docx

    build(str(tmp_path / 'fx'))
    xml = tmp_path / 'fx' / 'word' / 'document.xml'
    xml.write_text(xml.read_text(encoding='utf8').replace(
        '<w:fldChar w:fldCharType="end"/>', '', 1), encoding='utf8')
    monkeypatch.setattr(check_docx, 'find_validator', lambda: None)
    monkeypatch.setattr(sys, 'argv', ['check_docx.py', str(tmp_path / 'fx'),
                                      str(tmp_path / 'out.docx'),
                                      '--original', str(tmp_path / 'fx.docx'),
                                      '--author', AUTHOR])
    with pytest.raises(SystemExit) as e:
        check_docx.main()
    assert e.value.code == 1
    assert 'UNBALANCED' in capsys.readouterr().out
    # the re-zipped package still opens and leads with [Content_Types].xml
    assert zipfile.ZipFile(tmp_path / 'out.docx').namelist()[0] == '[Content_Types].xml'


def test_find_validator_skips_staging_copies(tmp_path):
    import check_docx

    rel = Path('docx', 'scripts', 'office', 'validate.py')
    staged = tmp_path / '.claude' / 'skills' / 'synced' / 'x' / '.staging' / '1' / rel
    real = tmp_path / '.claude' / 'skills' / 'synced' / 'x' / rel
    for p in (staged, real):
        p.parent.mkdir(parents=True)
        p.write_text('')
    assert check_docx.find_validator(home=tmp_path) == real
    real.unlink()
    assert check_docx.find_validator(home=tmp_path) is None


def test_run_edits_makes_library_importable(tmp_path, monkeypatch):
    import run_edits

    build(str(tmp_path / 'fx'))
    edits = tmp_path / 'edits.py'
    edits.write_text(
        'from redline import Doc\n'
        f"d = Doc({str(tmp_path / 'fx' / 'word' / 'document.xml')!r}, author='T, T')\n"
        "d.replace_text(d.find_para('Alpha'), 'beta', ['BETA'])\n"
        'd.save()\n')
    monkeypatch.setattr(sys, 'argv', ['run_edits.py', str(edits)])
    run_edits.main()
    assert 'BETA' in (tmp_path / 'fx' / 'word' / 'document.xml').read_text(encoding='utf8')


def test_find_para_refuses_ambiguous_match(doc):
    # 'Smith' is in two paragraphs' field results; taking the first would
    # put the edit in whichever happens to come earlier.
    with pytest.raises(ValueError, match='ambiguous'):
        doc.find_para('(Smith, 2020)')
    assert doc.find_para('(Smith, 2020)', start=3) is doc.paras()[3]
    assert doc.find_para('(Smith, 2020)', occurrence=2) is doc.paras()[3]
    with pytest.raises(ValueError, match='occurrence 3 of 2'):
        doc.find_para('(Smith, 2020)', occurrence=3)


def test_duplicate_citation_ids_reported(doc):
    import check_docx

    xml = etree.tostring(doc.root, encoding='unicode')
    # the fixture's two fields share one citationID
    assert check_docx.duplicate_citation_ids(xml) == ['AbCdEfGh']
    doc.move_paragraph(doc.find_para('Before (Smith'), doc.find_para('Target paragraph'))
    assert check_docx.duplicate_citation_ids(etree.tostring(doc.root, encoding='unicode')) == [
        'AbCdEfGh']  # the move renewed the copy's ID instead of adding a third


def test_inspect_lists_authors_and_libraries(tmp_path, monkeypatch, capsys):
    import inspect_docx

    xml = build(str(tmp_path / 'fx'))
    monkeypatch.setattr(sys, 'argv', ['inspect_docx.py', xml, '--fields', '--authors'])
    inspect_docx.main()
    out = capsys.readouterr().out
    assert 'library zotero.org/groups/1' in out
    assert "'Other, Author' ins 3" in out


def test_inspect_open_ended_range(tmp_path, monkeypatch, capsys):
    import inspect_docx

    xml = build(str(tmp_path / 'fx'))
    monkeypatch.setattr(sys, 'argv', ['inspect_docx.py', xml, '4'])
    inspect_docx.main()
    lines = capsys.readouterr().out.splitlines()
    assert [ln.split(']')[0] for ln in lines] == ['[4', '[5']


# ---------------- validator baseline ----------------

# Stands in for the docx skill's validate.py, which is not in the repo. The
# original "has" an unreferenced part; MODE controls what the edited run adds.
FAKE_VALIDATOR = """
import os, sys
mode = os.environ.get('FAKE_MODE', 'clean')
edited = '--author' in sys.argv
if edited and mode == 'crash':
    sys.exit('Traceback: ModuleNotFoundError')
print('FAILED - Found 1 relationship validation errors:')
print('  Unreferenced file: [trash]/0000.dat')
if edited:
    print('Paragraphs: 6 \u2192 7 (+1)')
    if mode == 'new':
        print('  Unreferenced file: word/media/new.png')
    if mode == 'untracked':
        print('Differences:')
        print('- Alpha beta')
sys.exit(1)
"""


@pytest.mark.parametrize('mode, ok', [('clean', True), ('new', False),
                                      ('untracked', False), ('crash', False)])
def test_validator_errors_shared_with_original_are_ignored(tmp_path, monkeypatch, mode, ok):
    import check_docx

    fake = tmp_path / 'validate.py'
    fake.write_text(FAKE_VALIDATOR)
    monkeypatch.setenv('FAKE_MODE', mode)
    valid, report = check_docx.run_validator(fake, 'out.docx', 'orig.docx', AUTHOR)
    assert valid is ok
    text = '\n'.join(report)
    if mode != 'crash':
        assert 'ignored (also reported for the original):' in text
        assert '[trash]/0000.dat' in text
    if mode == 'new':
        assert 'NEW relative to the original:\n  Unreferenced file: word/media/new.png' in text


def test_real_validator_tolerates_preexisting_unreferenced_part(tmp_path, monkeypatch, capsys):
    """The real docx-skill validator, on an original that already fails it."""
    import check_docx

    pytest.importorskip('defusedxml')
    # conftest points HOME at an empty temp dir; the validator is a real,
    # read-only install, so look it up in the account's actual home.
    pwd = pytest.importorskip('pwd')
    validator = check_docx.find_validator(home=pwd.getpwuid(os.getuid()).pw_dir)
    if validator is None:
        pytest.skip('Anthropic docx skill (validate.py) not installed under ~/.claude')
    build(str(tmp_path / 'fx'))
    (tmp_path / 'fx' / '[trash]').mkdir()
    (tmp_path / 'fx' / '[trash]' / '0000.dat').write_bytes(b'x')
    orig = tmp_path / 'orig.docx'
    check_docx.zip_dir(str(tmp_path / 'fx'), str(orig))
    d = Doc(str(tmp_path / 'fx' / 'word' / 'document.xml'), author=AUTHOR)
    d.replace_text(d.find_para('Alpha'), 'beta', ['BETA'])
    d.save()
    monkeypatch.setattr(sys, 'argv', ['check_docx.py', str(tmp_path / 'fx'),
                                      str(tmp_path / 'out.docx'), '--original', str(orig),
                                      '--author', AUTHOR, '--validator', str(validator)])
    with pytest.raises(SystemExit) as e:
        check_docx.main()
    out = capsys.readouterr().out
    assert e.value.code == 0, out
    assert '[trash]/0000.dat' in out.split('ignored (also reported for the original):')[1]
