"""Anchor tie-break: a heading anchor also matches its own table-of-contents entry.

``ParaIndex.find`` (and so ``find_paragraph`` and every caller of it) drops TOC
paragraphs from an ambiguous hit list and returns the one body paragraph left.
``find_all`` / ``find_paragraphs`` are unchanged. A TOC is recognised three ways,
each built here: a TOC complex field (fldChar/instrText) with hyperlinked entries,
TOC1/TOC2 paragraph styles, and a ``w:sdt`` whose docPartGallery is
'Table of Contents'.

Synthetic fixtures only (``new_doc`` + a hand-built body).
"""
from __future__ import annotations

import pytest
from lxml import etree

from zoterocite import Docx, new_doc, read_views
from zoterocite.docxio import DOCUMENT
from zoterocite.ooxml import NS
from zoterocite.paras import (
    ParaIndex, find_paragraph, find_paragraphs, get_body, iter_paragraphs, paragraph_text,
)
from zoterocite.revisions import tracked_delete_paragraph_text

AC = "Reviewer, Example"
HEAD1 = "Background and Significance"
HEAD2 = "Approach"
BODY = "The study enrolls a synthetic cohort."

W = NS["w"]


def _candidates(root, anchor):
    from zoterocite.paras import find_paragraph_candidates
    return find_paragraph_candidates(root, anchor)


def _r(text):
    return f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>'


def _heading(text, level=1):
    return f'<w:p><w:pPr><w:pStyle w:val="Heading{level}"/></w:pPr>{_r(text)}</w:p>'


def _toc_entry(text, page, anchor, style=None, *, begin=False, end=False):
    """A Word-shaped TOC line: hyperlink wrapping the text, a tab, and a nested
    PAGEREF field. *begin* opens the outer TOC field in this paragraph."""
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    head = ""
    if begin:
        head = ('<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
                '<w:r><w:instrText xml:space="preserve"> TOC \\o "1-3" \\h \\z \\u </w:instrText></w:r>'
                '<w:r><w:fldChar w:fldCharType="separate"/></w:r>')
    link = (f'<w:hyperlink w:anchor="{anchor}" w:history="1">{_r(text)}'
            '<w:r><w:tab/></w:r>'
            '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
            f'<w:r><w:instrText xml:space="preserve"> PAGEREF {anchor} \\h </w:instrText></w:r>'
            '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
            f'{_r(page)}'
            '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:hyperlink>')
    tail = '<w:r><w:fldChar w:fldCharType="end"/></w:r>' if end else ""
    return f"<w:p>{ppr}{head}{link}{tail}</w:p>"


def _toc_field_body():
    # Unstyled entries: ONLY the complex field marks them as TOC.
    return (_toc_entry(HEAD1, "1", "_Toc1", begin=True)
            + _toc_entry(HEAD2, "2", "_Toc2", end=True))


def _toc_style_body():
    # Styled entries, no field and no sdt: ONLY the pStyle marks them as TOC.
    return (f'<w:p><w:pPr><w:pStyle w:val="TOC1"/></w:pPr>{_r(HEAD1)}<w:r><w:tab/></w:r>{_r("1")}</w:p>'
            f'<w:p><w:pPr><w:pStyle w:val="TOC2"/></w:pPr>{_r(HEAD2)}<w:r><w:tab/></w:r>{_r("2")}</w:p>')


def _toc_sdt_body():
    # Unstyled, fieldless entries inside a docPartGallery sdt: ONLY the sdt marks them.
    return ('<w:sdt><w:sdtPr><w:id w:val="42"/><w:docPartObj>'
            '<w:docPartGallery w:val="Table of Contents"/><w:docPartUnique/>'
            '</w:docPartObj></w:sdtPr><w:sdtContent>'
            f'<w:p>{_r(HEAD1)}<w:r><w:tab/></w:r>{_r("1")}</w:p>'
            f'<w:p>{_r(HEAD2)}<w:r><w:tab/></w:r>{_r("2")}</w:p>'
            '</w:sdtContent></w:sdt>')


TOC_SHAPES = {
    "field": _toc_field_body,
    "style": _toc_style_body,
    "sdt": _toc_sdt_body,
}


def _build(tmp_path, toc_xml, after=None, name="toc.docx"):
    """Package: the TOC block, then Heading1 + body + Heading2 + body."""
    src = tmp_path / name
    new_doc(src, ["placeholder"])
    doc = Docx(src)
    root = doc.tree(DOCUMENT)
    body = get_body(root)
    sect = body.find(f"{{{W}}}sectPr")
    for child in list(body):
        if child is not sect:
            body.remove(child)
    content = after if after is not None else (
        _heading(HEAD1) + f"<w:p>{_r(BODY)}</w:p>" + _heading(HEAD2)
        + f"<w:p>{_r('Methods follow.')}</w:p>")
    frag = etree.fromstring(f'<w:body xmlns:w="{W}">{toc_xml}{content}</w:body>')
    for i, child in enumerate(list(frag)):
        body.insert(i, child)
    doc.save(src)
    return src


def _style(p):
    ps = p.find(f"{{{W}}}pPr/{{{W}}}pStyle")
    return None if ps is None else ps.get(f"{{{W}}}val")


def _is_heading(p):
    return (_style(p) or "").startswith("Heading")


# ---------------------------------------------------------------------------
# find_paragraph resolves the heading, for each TOC shape
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("shape", sorted(TOC_SHAPES))
def test_find_paragraph_prefers_heading_over_toc(tmp_path, shape):
    src = _build(tmp_path, TOC_SHAPES[shape]())
    root = Docx(src).read_tree(DOCUMENT)
    # the raw match set still sees BOTH (find_all unchanged)
    assert len(find_paragraphs(root, HEAD1)) == 2
    p = find_paragraph(root, HEAD1)
    assert _is_heading(p) and paragraph_text(p) == HEAD1
    p2 = ParaIndex(root).find(HEAD2)
    assert _is_heading(p2) and paragraph_text(p2) == HEAD2
    assert _candidates(root, HEAD1) == [p]


def test_sdt_wrapped_paragraphs_are_iterated(tmp_path):
    # iter_paragraphs descends into sdtContent: the TOC entries are visible to the
    # matcher, which is why the tie-break is needed at all.
    src = _build(tmp_path, _toc_sdt_body())
    paras = iter_paragraphs(Docx(src).read_tree(DOCUMENT))
    in_sdt = [p for p in paras if p.getparent().tag == f"{{{W}}}sdtContent"]
    assert len(in_sdt) == 2


@pytest.mark.parametrize("shape", sorted(TOC_SHAPES))
def test_is_toc_classifies_every_paragraph(tmp_path, shape):
    src = _build(tmp_path, TOC_SHAPES[shape]())
    root = Docx(src).read_tree(DOCUMENT)
    idx = ParaIndex(root)
    flags = [idx.is_toc(p) for p in iter_paragraphs(root)]
    assert flags == [True, True, False, False, False, False]


# ---------------------------------------------------------------------------
# a tracked edit on the heading anchor edits the heading, not the TOC
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("shape", sorted(TOC_SHAPES))
def test_tracked_delete_paragraph_text_edits_heading(tmp_path, shape):
    src = _build(tmp_path, TOC_SHAPES[shape]())
    doc = Docx(src)
    tracked_delete_paragraph_text(doc, HEAD1, author=AC)
    out = tmp_path / "out.docx"
    doc.save(out)
    acc = read_views(out)["accepted"].splitlines()
    rej = read_views(out)["rejected"].splitlines()
    # the TOC line survives, the heading line is gone from the accepted view
    assert any(l.startswith(HEAD1) and l != HEAD1 for l in acc)
    assert HEAD1 not in acc
    assert HEAD1 in rej
    root = Docx(out).read_tree(DOCUMENT)
    dels = list(root.iter(f"{{{W}}}del"))
    assert dels and all(_is_heading(d.getparent()) for d in dels)


# ---------------------------------------------------------------------------
# behaviour that must NOT change
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("shape", sorted(TOC_SHAPES))
def test_anchor_matching_only_toc_still_resolves(tmp_path, shape):
    # No body heading with this text: the TOC entry is the sole hit and wins.
    after = f"<w:p>{_r(BODY)}</w:p>"
    src = _build(tmp_path, TOC_SHAPES[shape](), after=after)
    root = Docx(src).read_tree(DOCUMENT)
    p = find_paragraph(root, HEAD2)
    assert ParaIndex(root).is_toc(p)
    assert paragraph_text(p).startswith(HEAD2)


@pytest.mark.parametrize("shape", sorted(TOC_SHAPES))
def test_two_body_matches_still_raise(tmp_path, shape):
    after = (_heading(HEAD1) + f"<w:p>{_r(BODY)}</w:p>"
             + f"<w:p>{_r(HEAD1 + ' are revisited later.')}</w:p>")
    src = _build(tmp_path, TOC_SHAPES[shape](), after=after)
    root = Docx(src).read_tree(DOCUMENT)
    assert len(find_paragraphs(root, HEAD1)) == 3
    with pytest.raises(LookupError, match="matched 3 paragraphs"):
        find_paragraph(root, HEAD1)


def test_two_body_matches_without_toc_still_raise(tmp_path):
    after = _heading(HEAD1) + f"<w:p>{_r(HEAD1)}</w:p>"
    src = _build(tmp_path, "", after=after)
    root = Docx(src).read_tree(DOCUMENT)
    with pytest.raises(LookupError, match="matched 2 paragraphs"):
        find_paragraph(root, HEAD1)
    assert len(_candidates(root, HEAD1)) == 2


def test_two_toc_matches_and_no_body_match_still_raise(tmp_path):
    # "Approach" appears in two TOC lines (TOC1 + TOC2) and nowhere in the body:
    # zero non-TOC hits, so the ambiguity is reported, not guessed.
    toc = (f'<w:p><w:pPr><w:pStyle w:val="TOC1"/></w:pPr>{_r(HEAD2)}</w:p>'
           f'<w:p><w:pPr><w:pStyle w:val="TOC2"/></w:pPr>{_r(HEAD2 + " details")}</w:p>')
    src = _build(tmp_path, toc, after=f"<w:p>{_r(BODY)}</w:p>")
    root = Docx(src).read_tree(DOCUMENT)
    with pytest.raises(LookupError, match="matched 2 paragraphs"):
        find_paragraph(root, HEAD2)


def test_find_all_order_and_content_unchanged(tmp_path):
    src = _build(tmp_path, _toc_field_body())
    root = Docx(src).read_tree(DOCUMENT)
    paras = iter_paragraphs(root)
    hits = ParaIndex(root).find_all(HEAD1)
    assert hits == [paras[0], paras[2]]
    assert find_paragraphs(root, HEAD1) == hits


def test_field_end_at_start_of_heading_is_not_toc(tmp_path):
    # The TOC field's closing fldChar sits at the START of the heading paragraph.
    # The heading shows no text while the TOC is open, so it is not a TOC line.
    toc = _toc_entry(HEAD1, "1", "_Toc1", begin=True)
    after = (f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
             f'<w:r><w:fldChar w:fldCharType="end"/></w:r>{_r(HEAD1)}</w:p>'
             f"<w:p>{_r(BODY)}</w:p>")
    src = _build(tmp_path, toc, after=after)
    root = Docx(src).read_tree(DOCUMENT)
    p = find_paragraph(root, HEAD1)
    assert _is_heading(p)


def test_fldsimple_toc_is_toc(tmp_path):
    toc = (f'<w:p><w:fldSimple w:instr=" TOC \\o &quot;1-3&quot; ">{_r(HEAD1)}'
           '</w:fldSimple></w:p>')
    src = _build(tmp_path, toc)
    root = Docx(src).read_tree(DOCUMENT)
    assert _is_heading(find_paragraph(root, HEAD1))


def test_toc_heading_style_counts_as_toc(tmp_path):
    # TOCHeading is the TOC's own title line ("Contents"); a body paragraph with
    # the same text wins the tie.
    toc = f'<w:p><w:pPr><w:pStyle w:val="TOCHeading"/></w:pPr>{_r("Contents")}</w:p>'
    after = _heading("Contents") + f"<w:p>{_r(BODY)}</w:p>"
    src = _build(tmp_path, toc, after=after)
    root = Docx(src).read_tree(DOCUMENT)
    assert _is_heading(find_paragraph(root, "Contents"))


def test_non_toc_field_does_not_mark_paragraph(tmp_path):
    # A PAGEREF/REF field around body text is not a TOC: two body matches still raise.
    fld = ('<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
           '<w:r><w:instrText xml:space="preserve"> REF _Ref1 \\h </w:instrText></w:r>'
           f'<w:r><w:fldChar w:fldCharType="separate"/></w:r>{_r(HEAD1)}'
           '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>')
    src = _build(tmp_path, fld)
    root = Docx(src).read_tree(DOCUMENT)
    with pytest.raises(LookupError, match="matched 2 paragraphs"):
        find_paragraph(root, HEAD1)


def test_other_docpart_gallery_sdt_is_not_toc(tmp_path):
    # A bibliography / cover-page building block is not a TOC: a body heading and
    # a same-text paragraph inside such an sdt remain a genuine ambiguity.
    for gallery in ("Bibliographies", "Cover Pages"):
        sdt = ('<w:sdt><w:sdtPr><w:docPartObj>'
               f'<w:docPartGallery w:val="{gallery}"/></w:docPartObj></w:sdtPr>'
               f'<w:sdtContent><w:p>{_r(HEAD1)}</w:p></w:sdtContent></w:sdt>')
        src = _build(tmp_path, sdt, name=f"{gallery[:3]}.docx")
        root = Docx(src).read_tree(DOCUMENT)
        with pytest.raises(LookupError, match="matched 2 paragraphs"):
            find_paragraph(root, HEAD1)


def test_plain_content_control_is_not_toc(tmp_path):
    # An sdt with no docPartObj (an ordinary content control) is not a TOC.
    sdt = ('<w:sdt><w:sdtPr><w:alias w:val="Title"/><w:id w:val="7"/></w:sdtPr>'
           f'<w:sdtContent><w:p>{_r(HEAD1)}</w:p></w:sdtContent></w:sdt>')
    src = _build(tmp_path, sdt)
    root = Docx(src).read_tree(DOCUMENT)
    with pytest.raises(LookupError, match="matched 2 paragraphs"):
        find_paragraph(root, HEAD1)
