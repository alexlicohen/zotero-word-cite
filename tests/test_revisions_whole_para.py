"""Whole-paragraph tracked replace (``revisions._replace_para_text``): which runs
get STRUCK.

Two invariants, both regression guards:

1. **Every visible-content run must be struck** — not just the ``<w:t>``-bearing
   ones. A standalone ``<w:tab>``/``<w:br>``/``<w:cr>`` run left unstruck SURVIVES
   into the accepted view, and because the replacement ``<w:ins>`` is appended at
   paragraph END the surviving run is repositioned BEFORE the inserted text. A
   paragraph like ``"Aim 1."`` + ``<w:tab/>`` + ``"Investigate X"`` then accepts to
   ``"\\tAim 1. Investigate Y"`` — a stray LEADING tab, the tab's original
   mid-paragraph position lost, and the accepted view no longer equal to new_text.

2. **A comment/annotation ANCHOR run must NEVER be struck** — wrapping
   ``<w:commentReference>``/``<w:annotationRef>`` in ``<w:del>`` tracked-deletes the
   comment's anchor point, so the comment detaches when the deletion is accepted.

Both assert the accepted view via the UNSTRIPPED
:func:`zoterocite.paras.paragraph_text`. ``read_views`` ``.strip()``s each view,
which would silently mask the stray *leading* tab of invariant 1 and leave the test
with no teeth.
"""
from lxml import etree

from zoterocite import Docx, new_doc, read_views
from zoterocite.docxio import DOCUMENT
from zoterocite.ooxml import qn
from zoterocite.paras import iter_paragraphs, paragraph_text
from zoterocite.revisions import _run_has_strikeable_content, tracked_replace_paragraph

AC = "Cohen, Alexander (Neurology)"


# -- fixtures ----------------------------------------------------------------
def _append_run(p, *, text=None, child=None, comment_style=False):
    r = etree.SubElement(p, qn("w:r"))
    if comment_style:
        rpr = etree.SubElement(r, qn("w:rPr"))
        etree.SubElement(rpr, qn("w:rStyle")).set(qn("w:val"), "CommentReference")
    if text is not None:
        t = etree.SubElement(r, qn("w:t"))
        t.set(qn("xml:space"), "preserve")
        t.text = text
    if child is not None:
        etree.SubElement(r, qn(child))
    return r


def _one_para_doc(tmp_path, build, *, name="src.docx"):
    """A one-paragraph doc whose paragraph element is handed to ``build``."""
    src = tmp_path / name
    new_doc(src, [{"runs": [{"text": ""}]}])
    doc = Docx(src)
    root = doc.tree(DOCUMENT)
    p = list(root.iter(qn("w:p")))[0]
    for child in list(p):                      # start from a clean paragraph
        if child.tag != qn("w:pPr"):
            p.remove(child)
    build(p)
    doc.save(src)
    return src


def _tabbed(p):
    """``"Aim 1."`` + a STANDALONE tab run + ``"Investigate X"``."""
    _append_run(p, text="Aim 1.")
    _append_run(p, child="w:tab")
    _append_run(p, text="Investigate X")


def _tabbed_commented(p):
    """Tab between two runs, plus a comment anchoring the whole paragraph."""
    etree.SubElement(p, qn("w:commentRangeStart")).set(qn("w:id"), "7")
    _append_run(p, text="Alpha")
    _append_run(p, child="w:tab")
    _append_run(p, text="Beta")
    etree.SubElement(p, qn("w:commentRangeEnd")).set(qn("w:id"), "7")
    r = _append_run(p, child="w:commentReference", comment_style=True)
    r.find(qn("w:commentReference")).set(qn("w:id"), "7")
    return r


# -- helpers -----------------------------------------------------------------
def _only_para_text(out):
    """The single body paragraph's accepted-view text via ``paragraph_text`` — the
    UNSTRIPPED reassembly (``read_views`` strips, hiding a stray leading tab)."""
    root = Docx(out).read_tree(DOCUMENT)
    paras = [p for p in iter_paragraphs(root) if paragraph_text(p)]
    assert len(paras) == 1
    return paragraph_text(paras[0])


def _is_struck(el):
    anc = el.getparent() if el is not None else None
    while anc is not None:
        if anc.tag == qn("w:del"):
            return True
        anc = anc.getparent()
    return False


def _tab_is_struck(out):
    root = Docx(out).read_tree(DOCUMENT)
    return _is_struck(root.find(".//" + qn("w:tab")))


# -- tests -------------------------------------------------------------------
def test_whole_para_replace_strikes_standalone_tab(tmp_path):
    src = _one_para_doc(tmp_path, _tabbed)
    doc = Docx(src)
    tracked_replace_paragraph(doc, "Investigate X", "Aim 1. Investigate Y",
                              author=AC, scope="paragraph")
    out = tmp_path / "tabbed_replace.docx"
    doc.save(out)
    # Accepted view is EXACTLY the intended new text — NO stray leading tab. Asserted
    # on the unstripped paragraph_text: read_views strips leading whitespace and would
    # hide the "\t" the bug leaves before the appended <w:ins>.
    assert _only_para_text(out) == "Aim 1. Investigate Y"
    assert _tab_is_struck(out)                 # the standalone tab run was struck
    # Rejecting restores the original paragraph (tab back in its original position).
    assert read_views(out)["rejected"] == "Aim 1.\tInvestigate X"


def test_whole_para_replace_never_strikes_comment_anchor(tmp_path):
    src = _one_para_doc(tmp_path, _tabbed_commented)
    doc = Docx(src)
    tracked_replace_paragraph(doc, "Beta", "Gamma Delta",
                              author=AC, scope="paragraph")
    out = tmp_path / "commented_replace.docx"
    doc.save(out)
    root = Docx(out).read_tree(DOCUMENT)
    # INVARIANT: the comment-reference run is NOT wrapped in <w:del> (striking it would
    # tracked-delete the comment's anchor point and detach the comment on accept).
    cref = root.find(".//" + qn("w:commentReference"))
    assert cref is not None
    assert not _is_struck(cref), "comment anchor was tracked-deleted"
    # Exact new text (tab struck, no stray leading tab) — unstripped paragraph_text.
    assert _only_para_text(out) == "Gamma Delta"
    assert _tab_is_struck(out)
    # Rejected view restores the original.
    assert read_views(out)["rejected"] == "Alpha\tBeta"


def test_strike_predicate_excludes_anchor_runs_even_with_content():
    """Direct guard on the predicate's ORDER: the comment/annotation-anchor exclusion
    is checked BEFORE the visible-content test, so an anchor run is never struck even
    when it co-carries a <w:t>/<w:tab> (some producers emit exactly that shape). The
    document-level test above cannot pin this on its own — a bare
    ``<w:commentReference/>`` run carries no strikeable child, so it would survive any
    predicate. Here the run carries both, which is the only shape where the exclusion
    is load-bearing."""
    def _run(*children, text=None):
        r = etree.Element(qn("w:r"))
        for c in children:
            etree.SubElement(r, qn(c))
        if text is not None:
            etree.SubElement(r, qn("w:t")).text = text
        return r

    # Anchor runs: never struck, with or without co-located visible content.
    assert not _run_has_strikeable_content(_run("w:commentReference"))
    assert not _run_has_strikeable_content(_run("w:annotationRef"))
    assert not _run_has_strikeable_content(_run("w:commentReference", text="x"))
    assert not _run_has_strikeable_content(_run("w:annotationRef", "w:tab"))
    # Visible-content runs Word renders: always struck.
    for child in ("w:tab", "w:br", "w:cr", "w:noBreakHyphen", "w:sym"):
        assert _run_has_strikeable_content(_run(child)), child
    assert _run_has_strikeable_content(_run(text="prose"))
    # A run with no visible content and no anchor (e.g. rPr-only): nothing to strike.
    assert not _run_has_strikeable_content(_run("w:rPr"))
    assert not _run_has_strikeable_content(_run("w:lastRenderedPageBreak"))
