"""Standalone flow nodes (<w:tab>/<w:br>) and moved-then-deleted text must resolve
in the accepted/rejected views exactly like the prose around them.

Regression class: :func:`zoterocite.views._extract` applied the four tracked-change
ancestor guards (del/moveFrom for accepted, ins/moveTo for rejected) in its ``<w:t>``
branch only. The ``<w:tab>`` and ``<w:br>``/``<w:cr>`` branches guarded ``mc:Fallback``
alone, so a tracked-DELETED tab leaked into the ACCEPTED view and a tracked-INSERTED
tab leaked into the REJECTED view — wrong view text and wrong word/char counts versus
what Word renders. Separately the ``<w:delText>`` branch excluded an ``ins`` ancestor
in rejected mode but not a ``moveTo`` ancestor, so a moved-then-deleted delText was
shown (and counted) in the rejected view even though rejecting all changes drops the
whole moveTo subtree.

The guards now live in one shared helper (``views._tracked_hidden``) that every
visible-content branch calls, which is what removes the hand-duplication that caused
the mismatch in the first place.
"""
from docx import Document
from lxml import etree

from zoterocite.ooxml import qn
from zoterocite.views import counts, read_views

_AUTHOR = "Reviewer, B."
_DATE = "2026-01-01T00:00:00Z"


def _rev(parent, kind, rid):
    """Append a tracked-revision wrapper (<w:ins>/<w:del>/<w:moveTo>/...) to *parent*."""
    el = etree.SubElement(parent, qn("w:" + kind))
    el.set(qn("w:id"), str(rid))
    el.set(qn("w:author"), _AUTHOR)
    el.set(qn("w:date"), _DATE)
    return el


def _text_run(parent, text, tag="w:t"):
    r = etree.SubElement(parent, qn("w:r"))
    t = etree.SubElement(r, qn(tag))
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    t.text = text
    return r


def _tab_run(parent):
    r = etree.SubElement(parent, qn("w:r"))
    etree.SubElement(r, qn("w:tab"))
    return r


def _save(build, tmp_path, name):
    """Build a one-paragraph doc via *build(p_el)* and save it; return the path."""
    doc = Document()
    build(doc.add_paragraph()._p)
    out = tmp_path / name
    doc.save(str(out))
    return str(out)


# --------------------------------------------------------------------------
# (a) tracked-STRUCK tab: gone from accepted, restored in rejected
# --------------------------------------------------------------------------
def test_struck_tab_excluded_from_accepted_present_in_rejected(tmp_path):
    def build(p):
        _text_run(p, "Aim 1.")
        _tab_run(_rev(p, "del", 1))          # <w:del><w:r><w:tab/>
        _text_run(p, "tail")

    views = read_views(_save(build, tmp_path, "struck_tab.docx"))
    assert views["accepted"] == "Aim 1.tail"      # struck tab gone — no stray "\t"
    assert views["rejected"] == "Aim 1.\ttail"    # rejecting restores the tab
    assert views["raw"] == "Aim 1.\ttail"         # raw shows the markup as-is


# --------------------------------------------------------------------------
# (b) tracked-INSERTED tab: present in accepted, gone from rejected
# --------------------------------------------------------------------------
def test_inserted_tab_present_in_accepted_excluded_from_rejected(tmp_path):
    def build(p):
        _text_run(p, "a")
        _tab_run(_rev(p, "ins", 2))          # <w:ins><w:r><w:tab/>
        _text_run(p, "b")

    views = read_views(_save(build, tmp_path, "inserted_tab.docx"))
    assert views["accepted"] == "a\tb"       # accepting keeps the inserted tab
    assert views["rejected"] == "ab"         # rejecting removes it


def test_inserted_break_present_in_accepted_excluded_from_rejected(tmp_path):
    # Same guard, sibling branch: <w:br>/<w:cr> must resolve like <w:tab>.
    def build(p):
        _text_run(p, "one")
        r = etree.SubElement(_rev(p, "ins", 3), qn("w:r"))
        etree.SubElement(r, qn("w:br"))
        _text_run(p, "two")

    views = read_views(_save(build, tmp_path, "inserted_br.docx"))
    assert views["accepted"] == "one\ntwo"
    assert views["rejected"] == "onetwo"


def test_struck_break_excluded_from_accepted(tmp_path):
    def build(p):
        _text_run(p, "one")
        r = etree.SubElement(_rev(p, "del", 4), qn("w:r"))
        etree.SubElement(r, qn("w:cr"))
        _text_run(p, "two")

    views = read_views(_save(build, tmp_path, "struck_cr.docx"))
    assert views["accepted"] == "onetwo"
    assert views["rejected"] == "one\ntwo"


def test_moved_away_tab_excluded_from_accepted(tmp_path):
    # moveFrom is the accepted-view half of the pair; its tab must go too.
    def build(p):
        _text_run(p, "x")
        _tab_run(_rev(p, "moveFrom", 5))
        _text_run(p, "y")

    views = read_views(_save(build, tmp_path, "movefrom_tab.docx"))
    assert views["accepted"] == "xy"
    assert views["rejected"] == "x\ty"


# --------------------------------------------------------------------------
# (c) moved-then-deleted delText: absent from (and uncounted in) the rejected view
# --------------------------------------------------------------------------
def test_moved_then_deleted_deltext_not_in_rejected(tmp_path):
    # <w:moveTo><w:del><w:delText> — reject_all removes the entire moveTo subtree,
    # so the struck text must not render (nor be word-counted) in the rejected view.
    def build(p):
        _text_run(p, "keep ")
        move_to = _rev(p, "moveTo", 6)
        _text_run(_rev(move_to, "del", 7), "MOVED", tag="w:delText")

    views = read_views(_save(build, tmp_path, "moveto_del.docx"))
    assert views["accepted"] == "keep"       # del wins in the accepted view
    assert "MOVED" not in views["rejected"]
    assert views["rejected"] == "keep"
    assert counts(views["rejected"])["words"] == 1
    assert "MOVED" in views["raw"]           # raw still shows the markup

    # The pre-existing insert-then-deleted sibling guard is unchanged.
    def build_ins(p):
        _text_run(p, "keep ")
        ins = _rev(p, "ins", 8)
        _text_run(_rev(ins, "del", 9), "TYPO", tag="w:delText")

    ins_views = read_views(_save(build_ins, tmp_path, "ins_del.docx"))
    assert ins_views["rejected"] == "keep"


def test_plain_deleted_deltext_still_restored_in_rejected(tmp_path):
    # Guard against over-reach: an ordinary <w:del><w:delText> (no move/ins ancestor)
    # must STILL come back in the rejected view.
    def build(p):
        _text_run(p, "keep ")
        _text_run(_rev(p, "del", 10), "gone", tag="w:delText")

    views = read_views(_save(build, tmp_path, "plain_del.docx"))
    assert views["accepted"] == "keep"
    assert views["rejected"] == "keep gone"
