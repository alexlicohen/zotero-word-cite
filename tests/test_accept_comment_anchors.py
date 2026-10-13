"""accept_all / reject_all never lose a comment ANCHOR marker.

Google Docs' Office export puts ``w:commentRangeStart`` / ``w:commentRangeEnd`` and
the ``w:commentReference`` run INSIDE a suggestion wrapper (a w:del deleting the
comment's anchored text; a w:ins by A nested in a w:del by B). Resolving that
wrapper wholesale used to drop the markers with it, and Word then hides the comment.
``revisions._hoist_comment_markers`` lifts them to the position of the OUTERMOST
wrapper the resolution removes. A rejected inserted paragraph mark with no
following paragraph keeps its paragraph when anything survives in it (hoisted
markers included), so the markers are not dropped with the shell.

All fixtures are synthetic: a crafted document.xml swapped into a ``new_doc``
package.
"""
from __future__ import annotations

from collections import Counter

import pytest
from lxml import etree

from zoterocite import Docx, new_doc
from zoterocite.docxio import DOCUMENT
from zoterocite.ooxml import qn
from zoterocite.paras import paragraph_text
from zoterocite.revisions import accept_all, reject_all

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A = 'w:author="Reviewer, Example" w:date="2026-01-01T00:00:00Z"'
B = 'w:author="Editor, Sample" w:date="2026-01-02T00:00:00Z"'
MARKER_TAGS = ("commentRangeStart", "commentRangeEnd", "commentReference")


# -- XML builders -------------------------------------------------------------
def _doc_xml(paras_inner):
    body = "".join(f"<w:p>{inner}</w:p>" for inner in paras_inner)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'
    ).encode()


def _t(text):
    return f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>'


def _dt(text):
    return f'<w:r><w:delText xml:space="preserve">{text}</w:delText></w:r>'


def _crs(cid):
    return f'<w:commentRangeStart w:id="{cid}"/>'


def _cre(cid):
    return f'<w:commentRangeEnd w:id="{cid}"/>'


def _ref(cid):
    return ('<w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr>'
            f'<w:commentReference w:id="{cid}"/></w:r>')


def _anchored(cid, run):
    """A comment fully anchored on ``run``: start, run, end, reference."""
    return _crs(cid) + run + _cre(cid) + _ref(cid)


def _pmark(kind, rid=90):
    return f'<w:pPr><w:rPr><w:{kind} w:id="{rid}" {A}/></w:rPr></w:pPr>'


# -- fixtures (name -> list of paragraph inner XML) ---------------------------
FIXTURES = {
    # a comment anchored on deleted text: markers INSIDE the w:del
    "marker_in_del": [
        _t("keep ")
        + f'<w:del w:id="1" {A}>{_anchored(0, _dt("gone"))}</w:del>'
        + _t(" tail")],
    # Google layering: an insertion by A nested in a deletion by B, comment inside
    "ins_A_in_del_B": [
        _t("keep ")
        + f'<w:del w:id="2" {B}>{_dt("pre ")}'
        + f'<w:ins w:id="3" {A}>{_anchored(1, _dt("mid"))}</w:ins>'
        + f'{_dt(" post")}</w:del>'
        + _t(" tail")],
    # a comment anchored on inserted text (lost on reject)
    "marker_in_ins": [
        _t("keep ")
        + f'<w:ins w:id="4" {A}>{_anchored(2, _t("added"))}</w:ins>'
        + _t(" tail")],
    # an inline move: comment on the moveFrom source and another on the moveTo copy
    "marker_in_move": [
        _t("start ")
        + '<w:moveFromRangeStart w:id="20" w:name="m1"/>'
        + f'<w:moveFrom w:id="5" {A}>{_anchored(3, _t("OLD "))}</w:moveFrom>'
        + '<w:moveFromRangeEnd w:id="20"/>'
        + _t("mid ")
        + '<w:moveToRangeStart w:id="21" w:name="m1"/>'
        + f'<w:moveTo w:id="6" {A}>{_anchored(4, _t("OLD "))}</w:moveTo>'
        + '<w:moveToRangeEnd w:id="21"/>'
        + _t("end")],
    # one run carrying deleted text AND the comment reference
    "mixed_run_deltext_ref": [
        _t("keep ")
        + f'<w:del w:id="7" {A}>{_crs(5)}{_cre(5)}'
        + '<w:r><w:delText>x</w:delText><w:commentReference w:id="5"/></w:r></w:del>'
        + _t(" tail")],
    # whole-paragraph move: source (moveFrom mark) and destination (moveTo mark),
    # each carrying its own comment inside the wrapper
    "para_move": [
        _t("before"),
        _pmark("moveFrom", 91) + f'<w:moveFrom w:id="8" {A}>{_anchored(6, _t("moved"))}</w:moveFrom>',
        _t("after"),
        _pmark("moveTo", 92) + f'<w:moveTo w:id="9" {A}>{_anchored(7, _t("moved"))}</w:moveTo>',
        _t("last")],
    # a deleted paragraph mark (pPr/rPr/w:del) with a comment in the paragraph
    "para_mark_del": [
        _pmark("del", 94) + _anchored(9, _t("Alpha")),
        _t("Beta")],
    # an inserted paragraph mark on a paragraph that merges forward on reject
    "ins_para_mark_merges": [
        _pmark("ins", 95) + f'<w:ins w:id="12" {A}>{_anchored(10, _t("added"))}</w:ins>',
        _t("next")],
    # a removed wrapper nested in ANOTHER removed wrapper: the markers must go to
    # the OUTERMOST one (accept drops del and moveFrom; reject drops ins and moveTo)
    "del_in_moveFrom": [
        _t("start ")
        + '<w:moveFromRangeStart w:id="22" w:name="m2"/>'
        + f'<w:moveFrom w:id="14" {A}>{_t("a ")}'
        + f'<w:del w:id="15" {B}>{_anchored(12, _dt("cut"))}</w:del>'
        + f'{_t(" b")}</w:moveFrom>'
        + '<w:moveFromRangeEnd w:id="22"/>'
        + _t(" end")],
    "ins_in_moveTo": [
        _t("start ")
        + '<w:moveToRangeStart w:id="23" w:name="m3"/>'
        + f'<w:moveTo w:id="16" {A}>{_t("a ")}'
        + f'<w:ins w:id="17" {B}>{_anchored(13, _t("new"))}</w:ins>'
        + f'{_t(" b")}</w:moveTo>'
        + '<w:moveToRangeEnd w:id="23"/>'
        + _t(" end")],
    # an inserted paragraph mark on the LAST paragraph: no merge target on reject
    "ins_para_mark_last": [
        _t("before"),
        _pmark("ins", 96) + _t("kept ")
        + f'<w:ins w:id="13" {A}>{_anchored(11, _t("added"))}</w:ins>'],
}


@pytest.fixture
def build(tmp_path):
    """Swap a crafted document.xml into a real .docx package and reload it."""
    base = tmp_path / "base.docx"
    new_doc(base, ["placeholder"])
    counter = {"n": 0}

    def _build(paras_inner) -> Docx:
        doc = Docx(base)
        doc.set_part_bytes(DOCUMENT, _doc_xml(paras_inner))
        counter["n"] += 1
        out = tmp_path / f"anchors_{counter['n']}.docx"
        doc.save(out)
        return Docx(out)

    return _build


def _marker_census(root) -> Counter:
    """(comment id, marker kind) -> count, across the whole document part."""
    c = Counter()
    for kind in MARKER_TAGS:
        for el in root.iter(qn("w:" + kind)):
            c[(el.get(qn("w:id")), kind)] += 1
    return c


def _roundtrip(doc, tmp_path, name):
    """Save -> reload through Docx; the part must reparse."""
    out = tmp_path / name
    doc.save(out)
    re_doc = Docx(out)
    etree.fromstring(re_doc.raw(DOCUMENT))       # reparses
    return re_doc


def _body_children(root, p_index=0):
    p = list(root.iter(qn("w:p")))[p_index]
    return p, [etree.QName(ch).localname for ch in p if ch.tag != qn("w:pPr")]


def _para_texts(root):
    return [paragraph_text(p) for p in root.iter(qn("w:p"))]


def _hoisted_ref_ok(r) -> bool:
    """A fresh CommentReference run: rPr/rStyle=CommentReference + the reference only."""
    rs = r.find(f"{qn('w:rPr')}/{qn('w:rStyle')}")
    kids = [etree.QName(ch).localname for ch in r]
    return (rs is not None and rs.get(qn("w:val")) == "CommentReference"
            and kids == ["rPr", "commentReference"])


# == the invariant: no comment marker is ever lost =============================
@pytest.mark.parametrize("fn", [accept_all, reject_all], ids=["accept", "reject"])
@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_comment_marker_census_is_invariant(build, tmp_path, name, fn):
    doc = build(FIXTURES[name])
    before = _marker_census(doc.tree(DOCUMENT))
    assert before, "fixture carries no comment markers"
    fn(doc)
    re_doc = _roundtrip(doc, tmp_path, f"{name}_{fn.__name__}.docx")
    after = _marker_census(re_doc.tree(DOCUMENT))
    assert after == before, f"{name}/{fn.__name__}: markers changed {before} -> {after}"
    raw = re_doc.raw(DOCUMENT).decode()
    for tag in ("w:ins ", "w:del ", "w:moveFrom ", "w:moveTo "):
        assert f"<{tag}" not in raw, f"{tag.strip()} survived {fn.__name__}"


# == position, order and the fresh reference run ================================
def test_accept_hoists_markers_to_the_del_position_in_order(build):
    doc = build(FIXTURES["marker_in_del"])
    accept_all(doc)
    p, kids = _body_children(doc.tree(DOCUMENT))
    assert kids == ["r", "commentRangeStart", "commentRangeEnd", "r", "r"]
    assert paragraph_text(p) == "keep  tail"
    assert _hoisted_ref_ok(list(p)[3])


def test_nested_ins_in_del_accept_hoists_to_outer_del(build):
    doc = build(FIXTURES["ins_A_in_del_B"])
    accept_all(doc)
    p, kids = _body_children(doc.tree(DOCUMENT))
    # the whole del(B) is gone; its markers sit where it stood, in order
    assert kids == ["r", "commentRangeStart", "commentRangeEnd", "r", "r"]
    assert paragraph_text(p) == "keep  tail"
    assert _hoisted_ref_ok(list(p)[3])


def test_nested_ins_in_del_reject_hoists_to_ins_inside_unwrapped_del(build):
    doc = build(FIXTURES["ins_A_in_del_B"])
    reject_all(doc)
    p, kids = _body_children(doc.tree(DOCUMENT))
    # reject removes only the ins(A): its markers land at the ins position, between
    # the del's restored "pre " and " post" (the del is unwrapped, not removed)
    assert kids == ["r", "r", "commentRangeStart", "commentRangeEnd", "r", "r", "r"]
    assert paragraph_text(p) == "keep pre  post tail"


def test_reject_hoists_markers_out_of_ins(build):
    doc = build(FIXTURES["marker_in_ins"])
    reject_all(doc)
    p, kids = _body_children(doc.tree(DOCUMENT))
    assert kids == ["r", "commentRangeStart", "commentRangeEnd", "r", "r"]
    assert paragraph_text(p) == "keep  tail"
    assert _hoisted_ref_ok(list(p)[3])


def test_accept_keeps_ins_markers_in_place(build):
    """Accept UNWRAPS an insertion: its markers stay on the kept text, unmoved."""
    doc = build(FIXTURES["marker_in_ins"])
    accept_all(doc)
    p, kids = _body_children(doc.tree(DOCUMENT))
    assert kids == ["r", "commentRangeStart", "r", "commentRangeEnd", "r", "r"]
    assert paragraph_text(p) == "keep added tail"


@pytest.mark.parametrize("fn,lost_id,kept_id,text", [
    # accept drops moveFrom: id 3 hoisted, id 4 stays on the kept moveTo copy
    (accept_all, "3", "4", "start mid OLD end"),
    # reject drops moveTo: id 4 hoisted, id 3 stays on the restored moveFrom copy
    (reject_all, "4", "3", "start OLD mid end"),
], ids=["accept-moveFrom", "reject-moveTo"])
def test_inline_move_markers(build, fn, lost_id, kept_id, text):
    doc = build(FIXTURES["marker_in_move"])
    fn(doc)
    p = next(doc.tree(DOCUMENT).iter(qn("w:p")))
    assert paragraph_text(p) == text
    kids = [ch for ch in p if ch.tag != qn("w:pPr")]
    s = next(i for i, ch in enumerate(kids) if ch.tag == qn("w:commentRangeStart")
             and ch.get(qn("w:id")) == lost_id)
    # hoisted trio is contiguous: start, end, fresh reference run (no text between)
    assert kids[s + 1].tag == qn("w:commentRangeEnd") and kids[s + 1].get(qn("w:id")) == lost_id
    assert _hoisted_ref_ok(kids[s + 2])
    # the kept copy's comment still wraps its text
    k = next(i for i, ch in enumerate(kids) if ch.tag == qn("w:commentRangeStart")
             and ch.get(qn("w:id")) == kept_id)
    assert kids[k + 1].findtext(qn("w:t")) == "OLD "


def test_mixed_run_accept_moves_only_the_reference(build):
    doc = build(FIXTURES["mixed_run_deltext_ref"])
    accept_all(doc)
    p, kids = _body_children(doc.tree(DOCUMENT))
    assert kids == ["r", "commentRangeStart", "commentRangeEnd", "r", "r"]
    assert paragraph_text(p) == "keep  tail"          # the delText resolved away
    assert _hoisted_ref_ok(list(p)[3])
    assert "delText" not in etree.tostring(p).decode()


def test_mixed_run_reject_restores_text_and_keeps_reference(build):
    doc = build(FIXTURES["mixed_run_deltext_ref"])
    reject_all(doc)
    p, _ = _body_children(doc.tree(DOCUMENT))
    assert paragraph_text(p) == "keep x tail"
    ref = next(p.iter(qn("w:commentReference")))
    run = ref.getparent()
    assert run.find(qn("w:t")) is not None and run.find(qn("w:t")).text == "x"


@pytest.mark.parametrize("fn,name", [
    (accept_all, "del_in_moveFrom"),     # del inside moveFrom: both removed
    (reject_all, "ins_in_moveTo"),       # ins inside moveTo: both removed
], ids=["accept-del-in-moveFrom", "reject-ins-in-moveTo"])
def test_nested_removed_wrappers_hoist_to_the_outermost(build, fn, name):
    doc = build(FIXTURES[name])
    fn(doc)
    p, kids = _body_children(doc.tree(DOCUMENT))
    # the whole outer wrapper is gone; the trio sits where it stood, in order
    assert kids == ["r", "commentRangeStart", "commentRangeEnd", "r", "r"]
    assert paragraph_text(p) == "start  end"
    assert _hoisted_ref_ok(list(p)[3])


# == pPr/rPr/w:del is a paragraph-mark MARKER, never a wrapper ================
def test_property_level_revision_is_not_a_wrapper():
    from zoterocite.revisions import _is_revision_wrapper
    x = etree.fromstring(
        f'<w:body xmlns:w="{W}"><w:p><w:pPr><w:rPr><w:del w:id="1" {A}/></w:rPr></w:pPr>'
        f'<w:del w:id="2" {A}>{_dt("x")}</w:del></w:p>'
        f'<w:tbl><w:tr><w:trPr><w:del w:id="3" {A}/></w:trPr></w:tr></w:tbl></w:body>')
    dels = list(x.iter(qn("w:del")))
    assert [_is_revision_wrapper(d) for d in dels] == [False, True, False]


def test_para_mark_del_accept_keeps_markers_in_order(build):
    doc = build(FIXTURES["para_mark_del"])
    accept_all(doc)
    p, kids = _body_children(doc.tree(DOCUMENT))
    assert kids == ["commentRangeStart", "r", "commentRangeEnd", "r"]


# == rejected inserted paragraph marks ========================================
def test_reject_ins_para_mark_merge_carries_hoisted_markers_forward(build):
    doc = build(FIXTURES["ins_para_mark_merges"])
    reject_all(doc)
    root = doc.tree(DOCUMENT)
    assert _para_texts(root) == ["next"]
    p, kids = _body_children(root)
    assert kids == ["commentRangeStart", "commentRangeEnd", "r", "r"]
    assert _hoisted_ref_ok(list(p)[2])


def test_reject_ins_para_mark_on_last_paragraph_keeps_it(build):
    """No following paragraph to merge into: the paragraph is KEPT (its surviving
    text and its hoisted markers), never dropped with them."""
    doc = build(FIXTURES["ins_para_mark_last"])
    reject_all(doc)
    root = doc.tree(DOCUMENT)
    assert _para_texts(root) == ["before", "kept "]
    p, kids = _body_children(root, 1)
    assert kids == ["r", "commentRangeStart", "commentRangeEnd", "r"]
    assert _hoisted_ref_ok(list(p)[-1])
