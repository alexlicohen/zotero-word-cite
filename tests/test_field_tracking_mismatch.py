"""Tracked fields whose runs escape their tracked change (bug #6b).

A Zotero Refresh in Word rewrites the result runs of a tracked-inserted citation
field OUTSIDE the field's ``w:ins``; Reject All then leaves a stray citation
number. ``validate`` flags it (``field_tracking_mismatch``, ERROR) and
``zoterofield.fix_field_tracking`` / ``zotero-scan --fix-tracking`` re-wrap the
loose runs with the begin wrapper's author and date. Synthetic fixtures only.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree

from zoterocite import cli
from zoterocite.docxio import Docx
from zoterocite.builder import new_doc
from zoterocite.views import read_views
from zoterocite.validate import validate
from zoterocite import zoterofield
from zoterocite.docxio import DOCUMENT
from zoterocite.ooxml import qn
from zoterocite.revisions import accept_all, reject_all

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W16DU = "http://schemas.microsoft.com/office/word/2023/wordml/word16du"
A, B = "Synthetic Author", "Other Synthetic"
DA, DB = "2026-02-03T10:11:12Z", "2026-02-04T08:00:00Z"
ZCODE = ' ADDIN ZOTERO_ITEM CSL_CITATION {"citationID":"x1","citationItems":[]} '


def run(text, sup=False):
    rpr = '<w:rPr><w:vertAlign w:val="superscript"/></w:rPr>' if sup else ""
    return f'<w:r>{rpr}<w:t xml:space="preserve">{text}</w:t></w:r>'


def fc(kind):
    return f'<w:r><w:fldChar w:fldCharType="{kind}"/></w:r>'


def instr(code, deleted=False):
    tag = "w:delInstrText" if deleted else "w:instrText"
    return f'<w:r><{tag} xml:space="preserve">{code}</{tag}></w:r>'


def ins(inner, rid, author=A, date=DA, extra=""):
    return f'<w:ins w:id="{rid}" w:author="{author}" w:date="{date}"{extra}>{inner}</w:ins>'


def dele(inner, rid, author=A, date=DA):
    return f'<w:del w:id="{rid}" w:author="{author}" w:date="{date}">{inner}</w:del>'


def build(path: Path, paras) -> Path:
    new_doc(path, ["placeholder"])
    d = Docx(path)
    body = d.tree(DOCUMENT).find(qn("w:body"))
    for p in body.findall(qn("w:p")):
        body.remove(p)
    sect = body.find(qn("w:sectPr"))
    for inner in paras:
        sect.addprevious(etree.fromstring(
            f'<w:p xmlns:w="{W}" xmlns:w16du="{W16DU}">{inner}</w:p>'.encode()))
    d.save(path)
    return path


def refreshed_ins(tmp_path, name="ins.docx", extra=""):
    """The Zotero-Refresh shape: begin/instr/separate and end in A's w:ins, the
    rendered number '7' untracked between them."""
    return build(tmp_path / name, [
        run("Prior work ")
        + ins(fc("begin") + instr(ZCODE) + fc("separate"), 10, extra=extra)
        + run("7", sup=True)
        + ins(fc("end"), 11, extra=extra)
        + run(" showed it.")])


def fixed(src: Path, out: Path):
    doc = Docx(src)
    st = zoterofield.fix_field_tracking(doc)
    doc.save(out)
    return st


def resolved_text(path: Path, fn) -> str:
    doc = Docx(path)
    fn(doc)
    root = doc.read_tree(DOCUMENT)
    return "".join(t.text or "" for t in root.iter(qn("w:t")))


def mismatch_errors(rep):
    return [e for e in rep.errors if "outside the field's own tracked change" in e]


def marks(path: Path):
    root = Docx(path).read_tree(DOCUMENT)
    return [el for el in root.iter(qn("w:ins"), qn("w:del"))]


# -- detection ------------------------------------------------------------------
def test_validate_flags_refreshed_tracked_citation(tmp_path):
    rep = validate(refreshed_ins(tmp_path), screen_retractions=False)
    assert "field_tracking_mismatch" in rep.info["checks"]
    errs = mismatch_errors(rep)
    assert len(errs) == 1 and "ZOTERO_ITEM" in errs[0] and not rep.ok


def test_consistent_and_untracked_fields_are_not_flagged(tmp_path):
    ok_ins = build(tmp_path / "ok.docx", [
        run("A ") + ins(fc("begin") + instr(ZCODE) + fc("separate")
                        + run("7", sup=True) + fc("end"), 10) + run(" b.")])
    plain = build(tmp_path / "plain.docx", [
        run("A ") + fc("begin") + instr(ZCODE) + fc("separate")
        + run("7", sup=True) + fc("end") + run(" b.")])
    # result in ANOTHER author's insertion is still removed on Reject All
    other = build(tmp_path / "other.docx", [
        ins(fc("begin") + instr(ZCODE) + fc("separate"), 10)
        + ins(run("7"), 12, author=B, date=DB) + ins(fc("end"), 11)])
    for p in (ok_ins, plain, other):
        assert zoterofield.field_tracking_issues(Docx(p)) == []
        assert mismatch_errors(validate(p, screen_retractions=False)) == []


def test_issue_details(tmp_path):
    (i,) = zoterofield.field_tracking_issues(Docx(refreshed_ins(tmp_path)))
    assert i == {"part": "word/document.xml", "field": "ZOTERO_ITEM", "tracked": "ins",
                 "author": A, "date": DA, "loose_runs": 1, "loose_text": "7",
                 "fixable": True}


def test_any_field_type_and_loose_end_run(tmp_path):
    # Generalised beyond Zotero: a NUMPAGES field whose separate/result/end all escaped.
    src = build(tmp_path / "np.docx", [
        run("Page count ") + ins(fc("begin") + instr(" NUMPAGES "), 10)
        + fc("separate") + run("3") + fc("end")])
    (i,) = zoterofield.field_tracking_issues(Docx(src))
    assert i["field"] == "NUMPAGES" and i["loose_runs"] == 3
    out = tmp_path / "np.fixed.docx"
    fixed(src, out)
    assert "3" not in resolved_text(out, reject_all)
    assert zoterofield.field_tracking_issues(Docx(out)) == []


# -- the fix --------------------------------------------------------------------
def test_fix_removes_stray_number_on_reject_and_keeps_it_on_accept(tmp_path):
    src = refreshed_ins(tmp_path)
    assert "7" in resolved_text(src, reject_all)          # the bug: stray number
    assert "7" in read_views(src)["rejected"]
    out = tmp_path / "fixed.docx"
    st = fixed(src, out)
    assert st == {"fields": 1, "runs": 1, "wrappers": 1, "unfixed": 0}
    assert resolved_text(out, reject_all) == "Prior work  showed it."
    assert resolved_text(out, accept_all) == "Prior work 7 showed it."
    views = read_views(out)
    assert "7" not in views["rejected"] and "7" in views["accepted"]
    rep = validate(out, screen_retractions=False)
    assert mismatch_errors(rep) == [] and rep.ok


def test_new_wrapper_copies_author_date_and_attrs_with_fresh_id(tmp_path):
    extra = f' w16du:dateUtc="2026-02-03T15:11:12Z"'
    src = refreshed_ins(tmp_path, extra=extra)
    before = {(el.get(qn("w:id")), el.get(qn("w:date"))) for el in marks(src)}
    out = tmp_path / "fixed.docx"
    fixed(src, out)
    after = marks(out)
    new = [el for el in after
           if (el.get(qn("w:id")), el.get(qn("w:date"))) not in before]
    assert len(new) == 1
    w = new[0]
    assert w.tag == qn("w:ins")
    assert w.get(qn("w:author")) == A and w.get(qn("w:date")) == DA
    assert w.get(f"{{{W16DU}}}dateUtc") == "2026-02-03T15:11:12Z"
    ids = [el.get(qn("w:id")) for el in after]
    assert len(ids) == len(set(ids)) and w.get(qn("w:id")) not in {"10", "11"}
    # no existing mark re-dated
    assert before <= {(el.get(qn("w:id")), el.get(qn("w:date"))) for el in after}
    assert [t.text for t in w.iter(qn("w:t"))] == ["7"]


def test_footnote_field_fixed_with_ids_unique_across_parts(tmp_path):
    # Zotero note styles put citations in footnotes. Ids 1-2 are in the body and
    # 3-4 in the footnotes, so a body-only or unseeded allocator would collide.
    src = build(tmp_path / "fn.docx", [
        ins(fc("begin") + instr(ZCODE) + fc("separate"), 1) + run("7")
        + ins(fc("end"), 2)])
    d = Docx(src)
    fn = (f'<w:footnotes xmlns:w="{W}"><w:footnote w:id="1"><w:p>'
          + ins(fc("begin") + instr(ZCODE) + fc("separate"), 3) + run("8")
          + ins(fc("end"), 4) + '</w:p></w:footnote></w:footnotes>')
    d.add_part("word/footnotes.xml", fn.encode(), "application/vnd.openxmlformats-"
               "officedocument.wordprocessingml.footnotes+xml")
    d.save(src)
    parts = [i["part"] for i in zoterofield.field_tracking_issues(Docx(src))]
    assert parts == ["word/document.xml", "word/footnotes.xml"]
    out = tmp_path / "fixed.docx"
    assert fixed(src, out)["wrappers"] == 2
    got = Docx(out)
    ids = [el.get(qn("w:id")) for part in (DOCUMENT, "word/footnotes.xml")
           for el in got.read_tree(part).iter(qn("w:ins"))]
    assert len(ids) == 6 and len(set(ids)) == 6
    assert zoterofield.field_tracking_issues(got) == []


def test_fix_is_idempotent_and_leaves_consistent_files_byte_identical(tmp_path):
    out = tmp_path / "fixed.docx"
    fixed(refreshed_ins(tmp_path), out)
    again = tmp_path / "again.docx"
    assert fixed(out, again) == {"fields": 0, "runs": 0, "wrappers": 0, "unfixed": 0}
    assert Docx(again).raw(DOCUMENT) == Docx(out).raw(DOCUMENT)


def test_deleted_field_result_wrapped_as_del_with_deltext(tmp_path):
    src = build(tmp_path / "del.docx", [
        run("Prior work ")
        + dele(fc("begin") + instr(ZCODE, deleted=True) + fc("separate"), 20)
        + run("7", sup=True)
        + dele(fc("end"), 21) + run(" showed it.")])
    assert "7" in resolved_text(src, accept_all)          # stray in the ACCEPTED view
    out = tmp_path / "fixed.docx"
    assert fixed(src, out)["wrappers"] == 1
    root = Docx(out).read_tree(DOCUMENT)
    (d,) = [el for el in root.iter(qn("w:del")) if el.get(qn("w:id")) not in ("20", "21")]
    assert d.find(f"{qn('w:r')}/{qn('w:delText')}").text == "7"
    assert d.find(f".//{qn('w:t')}") is None
    assert "7" not in resolved_text(out, accept_all)
    assert "7" in resolved_text(out, reject_all)
    v = read_views(out)
    assert "7" not in v["accepted"] and "7" in v["rejected"]


def test_inserted_then_deleted_field_gets_ins_then_del(tmp_path):
    begin = ins(dele(fc("begin") + instr(ZCODE, deleted=True) + fc("separate"), 31,
                     author=B, date=DB), 30)
    end = ins(dele(fc("end"), 33, author=B, date=DB), 32)
    src = build(tmp_path / "insdel.docx", [run("X ") + begin + run("7") + end + run(" y")])
    out = tmp_path / "fixed.docx"
    assert fixed(src, out)["wrappers"] == 2
    root = Docx(out).read_tree(DOCUMENT)
    (dt,) = [t for t in root.iter(qn("w:delText")) if t.text == "7"]
    d = dt.getparent().getparent()
    i = d.getparent()
    assert (d.tag, d.get(qn("w:author")), d.get(qn("w:date"))) == (qn("w:del"), B, DB)
    assert (i.tag, i.get(qn("w:author")), i.get(qn("w:date"))) == (qn("w:ins"), A, DA)
    assert "7" not in resolved_text(out, accept_all)
    assert "7" not in resolved_text(out, reject_all)


def test_multi_run_result_one_wrapper_and_field_spanning_paragraphs(tmp_path):
    src = build(tmp_path / "multi.docx", [
        run("See ") + ins(fc("begin") + instr(ZCODE) + fc("separate"), 10)
        + run("1") + run(",") + run("2") + run("-4"),
        run("continued ") + run("result"),
        ins(fc("end"), 11) + run(" tail.")])
    (i,) = zoterofield.field_tracking_issues(Docx(src))
    assert i["loose_runs"] == 6
    out = tmp_path / "fixed.docx"
    st = fixed(src, out)
    assert st == {"fields": 1, "runs": 6, "wrappers": 2, "unfixed": 0}
    assert resolved_text(out, reject_all) == "See  tail."
    assert resolved_text(out, accept_all) == "See 1,2-4continued result tail."


def test_nested_field_runs_take_their_own_fields_author(tmp_path):
    inner = (ins(fc("begin") + instr(" PAGEREF _Ref1 \\h ") + fc("separate"), 40,
                 author=B, date=DB)
             + run("9") + ins(fc("end"), 41, author=B, date=DB))
    src = build(tmp_path / "nested.docx", [
        run("Lead ") + ins(fc("begin") + instr(ZCODE) + fc("separate"), 10)
        + run("7") + inner + run("8") + ins(fc("end"), 11) + run(" tail")])
    assert len(zoterofield.field_tracking_issues(Docx(src))) == 2
    out = tmp_path / "fixed.docx"
    fixed(src, out)
    root = Docx(out).read_tree(DOCUMENT)
    owner = {t.text: t.getparent().getparent() for t in root.iter(qn("w:t"))
             if t.text in ("7", "8", "9")}
    assert owner["9"].get(qn("w:author")) == B and owner["9"].get(qn("w:date")) == DB
    assert owner["7"].get(qn("w:author")) == A and owner["8"].get(qn("w:author")) == A
    assert resolved_text(out, reject_all) == "Lead  tail"
    assert zoterofield.field_tracking_issues(Docx(out)) == []


def test_textbox_run_in_a_loose_host_run_is_not_wrapped_twice(tmp_path):
    v = "urn:schemas-microsoft-com:vml"
    box = (f'<w:r><w:pict><v:shape xmlns:v="{v}"><v:textbox><w:txbxContent>'
           f'<w:p>{run("9")}</w:p></w:txbxContent></v:textbox></v:shape></w:pict></w:r>')
    src = build(tmp_path / "box.docx", [
        ins(fc("begin") + instr(ZCODE) + fc("separate"), 10) + box + ins(fc("end"), 11)])
    out = tmp_path / "fixed.docx"
    assert fixed(src, out)["wrappers"] == 1
    assert zoterofield.field_tracking_issues(Docx(out)) == []


def test_loose_run_inside_bare_deletion_is_reported_not_rewrapped(tmp_path):
    src = build(tmp_path / "bare.docx", [
        ins(fc("begin") + instr(ZCODE) + fc("separate"), 10)
        + dele(f'<w:r><w:delText>7</w:delText></w:r>', 50, author=B, date=DB)
        + ins(fc("end"), 11)])
    (i,) = zoterofield.field_tracking_issues(Docx(src))
    assert i["fixable"] is False
    out = tmp_path / "fixed.docx"
    assert fixed(src, out)["unfixed"] == 1
    assert Docx(out).raw(DOCUMENT) == Docx(src).raw(DOCUMENT)
    assert "need a manual fix" in mismatch_errors(validate(out, screen_retractions=False))[0]


def test_field_codes_unchanged_by_the_shared_walker(tmp_path):
    # scan_citations reads live instrText only; a deleted field's code stays "".
    src = build(tmp_path / "codes.docx", [
        fc("begin") + instr(" A") + instr("B ") + fc("separate") + run("r") + fc("end"),
        dele(fc("begin") + instr(ZCODE, deleted=True) + fc("end"), 5)])
    root = Docx(src).read_tree(DOCUMENT)
    assert zoterofield._field_codes(root) == [" AB ", ""]
    kinds = [f.kind for f in zoterofield.iter_complex_fields(root)]
    assert kinds == ["AB", "ZOTERO_ITEM"]


# -- CLI ------------------------------------------------------------------------
def test_cli_fix_tracking(tmp_path, capsys):
    src = refreshed_ins(tmp_path)
    out = tmp_path / "cli.docx"
    cmd = ["zotero-scan", str(src), "--fix-tracking"]
    with pytest.raises(SystemExit) as no_out:
        cli.main(cmd)
    assert "-o/--out" in str(no_out.value.code)
    with pytest.raises(SystemExit) as ok:
        cli.main(cmd + ["-o", str(out)])
    assert ok.value.code == 0
    stdout = capsys.readouterr().out
    assert "1 field(s) repaired" in stdout and "ZOTERO_ITEM" in stdout
    assert validate(out, screen_retractions=False).ok
