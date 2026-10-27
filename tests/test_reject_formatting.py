"""``reject_all`` restores tracked FORMATTING changes the way Word's Reject All does.

A ``w:rPrChange`` / ``w:pPrChange`` holds a snapshot of the PRIOR properties; rejecting
it must put that snapshot back, not merely drop the record (which kept the NEW
formatting). A paragraph's own mark-run ``w:rPr`` and ``w:sectPr`` are not part of the
``pPrChange`` snapshot and stay. Synthetic text only.
"""
from lxml import etree

from zoterocite import Docx, new_doc
from zoterocite.docxio import DOCUMENT
from zoterocite.ooxml import NS, qn
from zoterocite.revisions import accept_all, reject_all

W = NS["w"]


def _doc(tmp_path, ppr, rpr):
    p = tmp_path / "f.docx"
    new_doc(p, [{"runs": [{"text": "Synthetic text."}]}])
    d = Docx(p)
    para = d.tree(DOCUMENT).find(".//" + qn("w:p"))
    for old in para.findall(qn("w:pPr")):
        para.remove(old)
    run = para.find(qn("w:r"))
    for old in run.findall(qn("w:rPr")):
        run.remove(old)
    para.insert(0, etree.fromstring(f'<w:pPr xmlns:w="{W}">{ppr}</w:pPr>'))
    run.insert(0, etree.fromstring(f'<w:rPr xmlns:w="{W}">{rpr}</w:rPr>'))
    return d, para, run


PPR = ('<w:pStyle w:val="Heading1"/><w:jc w:val="center"/><w:rPr><w:b/></w:rPr>'
       '<w:pPrChange w:id="5" w:author="A" w:date="2026-10-01T10:00:00Z">'
       '<w:pPr><w:ind w:firstLine="720"/><w:jc w:val="both"/></w:pPr></w:pPrChange>')
RPR = ('<w:b/><w:sz w:val="28"/>'
       '<w:rPrChange w:id="6" w:author="A" w:date="2026-10-01T10:00:00Z">'
       '<w:rPr><w:i/></w:rPr></w:rPrChange>')


def test_reject_restores_prior_paragraph_and_run_formatting(tmp_path):
    d, para, run = _doc(tmp_path, PPR, RPR)
    reject_all(d)
    ppr = para.find(qn("w:pPr"))
    assert ppr.find(qn("w:pStyle")) is None
    assert ppr.find(qn("w:jc")).get(qn("w:val")) == "both"
    assert ppr.find(qn("w:ind")).get(qn("w:firstLine")) == "720"
    assert ppr.find(qn("w:rPr")).find(qn("w:b")) is not None   # mark rPr kept
    assert ppr.find(qn("w:pPrChange")) is None
    rpr = run.find(qn("w:rPr"))
    assert [c.tag for c in rpr] == [qn("w:i")]


def test_accept_keeps_current_formatting(tmp_path):
    d, para, run = _doc(tmp_path, PPR, RPR)
    accept_all(d)
    ppr = para.find(qn("w:pPr"))
    assert ppr.find(qn("w:pStyle")).get(qn("w:val")) == "Heading1"
    assert ppr.find(qn("w:jc")).get(qn("w:val")) == "center"
    assert ppr.find(qn("w:pPrChange")) is None
    assert [c.tag for c in run.find(qn("w:rPr"))] == [qn("w:b"), qn("w:sz")]


def test_reject_to_empty_snapshot_removes_the_properties(tmp_path):
    d, para, run = _doc(tmp_path, "", '<w:b/><w:rPrChange w:id="7" w:author="A" '
                        'w:date="2026-10-01T10:00:00Z"><w:rPr/></w:rPrChange>')
    reject_all(d)
    assert run.find(qn("w:rPr")) is None
