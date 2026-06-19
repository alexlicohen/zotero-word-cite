"""Tests for zoterocite.paras.paragraph_text — the run-reassembly primitive.

Pins the accepted-view text model: tracked deletions excluded, and
flow-affecting children (``<w:br>``/``<w:cr>``/``<w:tab>``) rendered as
separators rather than silently dropped (which used to glue adjacent words).
"""
from lxml import etree

from zoterocite.ooxml import qn
from zoterocite.paras import paragraph_text
from zoterocite.views import read_views


def _run(parent, text):
    r = etree.SubElement(parent, qn("w:r"))
    t = etree.SubElement(r, qn("w:t"))
    t.text = text
    return r


def _break(parent, kind=None):
    r = etree.SubElement(parent, qn("w:r"))
    b = etree.SubElement(r, qn("w:br"))
    if kind:
        b.set(qn("w:type"), kind)
    return r


def _tab(parent):
    r = etree.SubElement(parent, qn("w:r"))
    etree.SubElement(r, qn("w:tab"))
    return r


def test_plain_runs_concatenate():
    # Adjacent <w:t> runs with no break between them stay glued (a word split
    # across runs must NOT gain a spurious separator).
    p = etree.Element(qn("w:p"))
    _run(p, "neuro")
    _run(p, "logy")
    assert paragraph_text(p) == "neurology"


def test_soft_break_renders_as_newline():
    p = etree.Element(qn("w:p"))
    _run(p, "Line one")
    _break(p)
    _run(p, "Line two")
    assert paragraph_text(p) == "Line one\nLine two"


def test_tab_and_page_break_render():
    p = etree.Element(qn("w:p"))
    _run(p, "a")
    _tab(p)
    _run(p, "b")
    _break(p, "page")
    _run(p, "c")
    assert paragraph_text(p) == "a\tb\nc"


def test_carriage_return_renders_as_newline():
    p = etree.Element(qn("w:p"))
    _run(p, "x")
    r = etree.SubElement(p, qn("w:r"))
    etree.SubElement(r, qn("w:cr"))
    _run(p, "y")
    assert paragraph_text(p) == "x\ny"


def test_tracked_deletion_excluded():
    p = etree.Element(qn("w:p"))
    _run(p, "keep ")
    d = etree.SubElement(p, qn("w:del"))
    dr = etree.SubElement(d, qn("w:r"))
    dt = etree.SubElement(dr, qn("w:delText"))
    dt.text = "GONE"
    _run(p, " tail")
    assert paragraph_text(p) == "keep  tail"


def test_literal_newline_in_wt_preserved():
    # python-docx add_run("a\nb") stores a literal newline inside one <w:t>;
    # it must survive unchanged (not be doubled or stripped).
    p = etree.Element(qn("w:p"))
    r = etree.SubElement(p, qn("w:r"))
    t = etree.SubElement(r, qn("w:t"))
    t.text = "1. a\n2. b"
    assert paragraph_text(p) == "1. a\n2. b"


def test_matches_read_views_accepted_content(tmp_path):
    # A single-paragraph doc whose only paragraph carries a soft break: the
    # per-w:p paragraph_text content equals the read_views accepted content.
    from docx import Document

    doc = Document()
    para = doc.add_paragraph()
    para.add_run("first")
    para.add_run().add_break()  # real <w:br/>
    para.add_run("second")
    out = tmp_path / "br.docx"
    doc.save(str(out))

    from zoterocite.docxio import Docx, DOCUMENT
    from zoterocite.paras import iter_paragraphs

    root = Docx(str(out)).read_tree(DOCUMENT)
    body_paras = [p for p in iter_paragraphs(root) if paragraph_text(p)]
    assert body_paras and paragraph_text(body_paras[0]) == "first\nsecond"
    assert read_views(str(out))["accepted"] == "first\nsecond"
