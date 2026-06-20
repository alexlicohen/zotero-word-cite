"""Field-safety of tracked replacement on paragraphs carrying a LIVE Zotero
citation field (a Word complex field: fldChar begin / instrText ZOTERO_ITEM /
fldChar separate / rendered-citation <w:t> / fldChar end).

Regression guard for CRIT-1 (audit A1+A2 / H1): a tracked replacement on a cited
paragraph must NEVER reach the whole-paragraph del+ins path, which strikes the
field's rendered citation and re-inserts it as STATIC LITERAL text — leaving, after
accept, a dead empty field (separate immediately followed by end) plus a typed
"(6)" that renumbers WRONG on the next Zotero refresh.

Invariants asserted after `accept_all`:
  * the begin/separate/end fldChar markers + the instrText survive byte-identical
    (counted),
  * the rendered citation text survives EXACTLY ONCE between separate and end
    (NOT struck, NOT duplicated as static literal),
  * only the intended plain prose words changed.
And after `reject_all` the original paragraph is restored exactly.
"""
import re

from lxml import etree

from zoterocite import Docx, new_doc, read_views, validate
from zoterocite.docxio import DOCUMENT
from zoterocite.ooxml import qn
from zoterocite.revisions import (
    tracked_replace_paragraph, tracked_replace_paragraph_el,
    tracked_replace_paragraphs, accept_all, reject_all,
)
from zoterocite.zoterofield import _field_runs, _plain_run, _runs

AC = "Cohen, Alexander (Neurology)"


# -- fixtures ----------------------------------------------------------------
def _xml(path):
    return Docx(path).raw(DOCUMENT).decode()


def _deltexts(body):
    return re.findall(r"<w:delText[^>]*>([^<]*)</w:delText>", body)


def _instexts(body):
    return [m for block in re.findall(r"<w:ins\b.*?</w:ins>", body, re.S)
            for m in re.findall(r"<w:t[^>]*>([^<]*)</w:t>", block)]


def _make_cited(tmp_path, prefix_text, rendered, suffix_text, *, name="src.docx"):
    """A one-paragraph doc: ``prefix_text`` + a live Zotero field rendering
    ``rendered`` + ``suffix_text``. The field is a real begin/instr/separate/result/
    end complex field (built with the same primitives as insert_citation)."""
    src = tmp_path / name
    new_doc(src, [{"runs": [{"text": prefix_text}]}])
    doc = Docx(src)
    root = doc.tree(DOCUMENT)
    p = list(root.iter(qn("w:p")))[0]
    instr = 'ADDIN ZOTERO_ITEM CSL_CITATION {"citationID":"ABC123"}'
    for r in _runs(_field_runs(instr, _plain_run(rendered))):
        p.append(r)
    sr = etree.SubElement(p, qn("w:r"))
    st = etree.SubElement(sr, qn("w:t"))
    st.set(qn("xml:space"), "preserve")
    st.text = suffix_text
    doc.save(src)
    return src


def _field_intact(body, rendered):
    """The 3 fldChar markers + instrText survive and the render sits once between
    separate and end."""
    assert body.count("<w:fldChar") == 3, body
    assert body.count("<w:instrText") == 1, body
    m = re.search(r'fldCharType="separate"/></w:r>(.*?)<w:r><w:fldChar w:fldCharType="end"',
                  body, re.S)
    assert m is not None, "no separate..end region (field destroyed)"
    assert f">{rendered}<" in m.group(1), f"render {rendered!r} lost from field result: {m.group(1)!r}"
    # the render must not be struck nor duplicated as a static literal
    assert rendered not in "".join(_deltexts(body))


# -- multi-span plain edit on a cited paragraph ------------------------------
def test_multi_span_plain_edit_keeps_field(tmp_path):
    src = _make_cited(tmp_path, "We replicated the prior finding ", "(6)", " across cohorts.")
    old = read_views(src)["accepted"]
    assert old == "We replicated the prior finding (6) across cohorts."
    new = "We confirmed the earlier finding (6) across both cohorts."

    doc = Docx(src)
    tracked_replace_paragraphs(doc, {0: new}, author=AC, scope="word")
    assert doc.skipped_field_paras == []          # applied, not refused
    out = tmp_path / "out.docx"
    doc.save(out)
    body = _xml(out)

    # the field is physically untouched in the tracked markup, render not struck
    assert "(6)" not in "".join(_deltexts(body))
    assert "(6)" not in "".join(_instexts(body))  # NOT re-inserted as static literal
    # only plain prose changed
    assert read_views(out)["accepted"] == new
    assert read_views(out)["rejected"] == old
    assert validate(out).ok

    # after accept_all the live field survives byte-identical, render once
    da = Docx(out); accept_all(da)
    acc = tmp_path / "acc.docx"; da.save(acc)
    _field_intact(_xml(acc), "(6)")
    assert read_views(acc)["accepted"] == new

    # after reject_all the original (incl. live field) is restored exactly
    dr = Docx(out); reject_all(dr)
    rej = tmp_path / "rej.docx"; dr.save(rej)
    _field_intact(_xml(rej), "(6)")
    assert read_views(rej)["accepted"] == old


# -- the A2 case: a 1-char plain edit whose minimal-diff substring ALSO --------
# -- appears inside the rendered citation -------------------------------------
def test_one_char_plain_edit_substring_in_render_keeps_field(tmp_path):
    # render is "(1)"; the plain edit "1" -> "2" — old_mid "1" also occurs inside
    # the render, so the pre-fix count over paragraph_text was 2 and surgical bailed
    # to the field-destroying whole-paragraph path. Now uniqueness is checked against
    # PLAIN-RUN text only, so the unique plain "1" is edited and the field survives.
    src = _make_cited(tmp_path, "We tested phase 1 in cohort ", "(1)", " overall.")
    old = read_views(src)["accepted"]
    assert old == "We tested phase 1 in cohort (1) overall."
    new = "We tested phase 2 in cohort (1) overall."

    doc = Docx(src)
    tracked_replace_paragraphs(doc, {0: new}, author=AC, scope="word")
    assert doc.skipped_field_paras == []
    out = tmp_path / "out.docx"; doc.save(out)
    body = _xml(out)

    # exactly the plain "1" struck and "2" inserted; the render "(1)" untouched
    assert _deltexts(body) == ["1"]
    assert "".join(_instexts(body)) == "2"
    assert "(1)" not in "".join(_deltexts(body))
    assert read_views(out)["accepted"] == new
    assert read_views(out)["rejected"] == old
    assert validate(out).ok

    da = Docx(out); accept_all(da); acc = tmp_path / "acc.docx"; da.save(acc)
    _field_intact(_xml(acc), "(1)")


# -- refuse rather than corrupt when the edit touches the citation render ------
def test_edit_touching_render_is_refused_not_corrupted(tmp_path):
    # An edit that changes the RENDERED citation text itself ("(6)" -> "(7)") cannot
    # be made field-safely (Zotero owns the render). It must be REFUSED — the
    # paragraph left byte-unchanged — never silently corrupted via whole-paragraph.
    src = _make_cited(tmp_path, "We replicated the finding ", "(6)", " here.")
    old = read_views(src)["accepted"]
    new = "We replicated the finding (7) here."

    doc = Docx(src)
    tracked_replace_paragraphs(doc, {0: new}, author=AC, scope="word")
    assert doc.skipped_field_paras == [0]         # refused, skip-and-report
    out = tmp_path / "out.docx"; doc.save(out)
    body = _xml(out)

    assert _deltexts(body) == []                  # nothing struck
    assert body.count("<w:ins ") == 0             # nothing inserted
    assert read_views(out)["accepted"] == old     # unchanged
    _field_intact(body, "(6)")
    assert validate(out).ok


# -- scope="paragraph" must NOT force the field-destroying whole-para path -----
def test_scope_paragraph_cannot_destroy_field(tmp_path):
    src = _make_cited(tmp_path, "We replicated the prior finding ", "(6)", " across cohorts.")
    old = read_views(src)["accepted"]
    new = "We confirmed the earlier finding (6) across both cohorts."

    doc = Docx(src)
    # Even with scope="paragraph" (which forces whole-paragraph for plain prose),
    # a cited paragraph stays on the field-safe path.
    tracked_replace_paragraphs(doc, {0: new}, author=AC, scope="paragraph")
    out = tmp_path / "out.docx"; doc.save(out)
    body = _xml(out)
    assert "(6)" not in "".join(_deltexts(body))   # render not struck
    da = Docx(out); accept_all(da); acc = tmp_path / "acc.docx"; da.save(acc)
    _field_intact(_xml(acc), "(6)")
    assert read_views(acc)["accepted"] == new


# -- the singular public entry exposes the refusal signal too -----------------
def test_singular_entry_signals_refusal(tmp_path):
    src = _make_cited(tmp_path, "We replicated the finding ", "(6)", " here.")
    old = read_views(src)["accepted"]
    doc = Docx(src)
    # editing the render via the singular anchored entry -> refused, doc unchanged
    tracked_replace_paragraph(doc, "We replicated", "We replicated the finding (7) here.",
                              author=AC)
    assert doc.last_replace_refused is True
    out = tmp_path / "out.docx"; doc.save(out)
    assert read_views(out)["accepted"] == old
    _field_intact(_xml(out), "(6)")

    # a safe plain edit via the singular entry -> applied, not refused
    doc2 = Docx(src)
    tracked_replace_paragraph(doc2, "We replicated", "We confirmed the finding (6) here.",
                              author=AC)
    assert doc2.last_replace_refused is False
    out2 = tmp_path / "out2.docx"; doc2.save(out2)
    assert read_views(out2)["accepted"] == "We confirmed the finding (6) here."
    da = Docx(out2); accept_all(da); acc = tmp_path / "a2.docx"; da.save(acc)
    _field_intact(_xml(acc), "(6)")


# -- the cached-element entry (used by the feedback apply path) ----------------
def test_replace_el_applies_to_cached_element_without_anchor(tmp_path):
    """tracked_replace_paragraph_el edits a paragraph passed BY ELEMENT (no anchor
    re-resolution), returns True when applied, and leaves a clean redline."""
    src = tmp_path / "p.docx"
    new_doc(str(src), ["We will explore the mechanism of tuber formation in patients."])
    doc = Docx(src)
    p = list(doc.tree(DOCUMENT).iter(qn("w:p")))[0]
    applied = tracked_replace_paragraph_el(
        doc, p, "We will determine the mechanism of tuber formation in patients.",
        author=AC, scope="word")
    assert applied is True and doc.last_replace_refused is False
    out = tmp_path / "out.docx"; doc.save(out)
    da = Docx(out); accept_all(da); acc = tmp_path / "a.docx"; da.save(acc)
    assert read_views(acc)["accepted"] == "We will determine the mechanism of tuber formation in patients."


def test_replace_el_refuses_field_unsafe_edit_returns_false(tmp_path):
    """On a cited paragraph whose edit touches the rendered citation, the element
    entry REFUSES (returns False, sets last_replace_refused, leaves doc unchanged)."""
    src = _make_cited(tmp_path, "We replicated the finding ", "(6)", " here.")
    old = read_views(src)["accepted"]
    doc = Docx(src)
    p = list(doc.tree(DOCUMENT).iter(qn("w:p")))[0]
    applied = tracked_replace_paragraph_el(
        doc, p, "We replicated the finding (7) here.", author=AC, scope="word")
    assert applied is False and doc.last_replace_refused is True
    out = tmp_path / "out.docx"; doc.save(out)
    assert read_views(out)["accepted"] == old
    _field_intact(_xml(out), "(6)")


# -- regression: insertion at a plain-run seam in a cited paragraph -----------
def test_insertion_at_plain_run_seam_in_cited_para_is_applied(tmp_path):
    """A pure word INSERTION landing exactly on the boundary between two adjacent
    plain runs in a field-bearing paragraph must NOT be silently dropped.

    The old run-seam ownership rule let an internal boundary be owned by NEITHER
    run (left run deferred forward, right run deferred backward), so the <w:ins>
    was never emitted — yet the API reported success (skipped_field_paras == []),
    defeating the skipped-field safety net.
    """
    src = tmp_path / "seam.docx"
    new_doc(src, [{"runs": [{"text": "Alpha "}]}])
    doc = Docx(src)
    root = doc.tree(DOCUMENT)
    p = list(root.iter(qn("w:p")))[0]
    # a SECOND plain run, so there is a real run|run seam at plain offset 6
    r2 = etree.SubElement(p, qn("w:r"))
    t2 = etree.SubElement(r2, qn("w:t"))
    t2.set(qn("xml:space"), "preserve"); t2.text = "Beta "
    # a live Zotero field rendering "(6)" + a trailing plain run
    instr = 'ADDIN ZOTERO_ITEM CSL_CITATION {"citationID":"ABC123"}'
    for r in _runs(_field_runs(instr, _plain_run("(6)"))):
        p.append(r)
    sr = etree.SubElement(p, qn("w:r"))
    st = etree.SubElement(sr, qn("w:t"))
    st.set(qn("xml:space"), "preserve"); st.text = " end."
    doc.save(src)

    old = read_views(src)["accepted"]
    assert old == "Alpha Beta (6) end."
    # insert "X " exactly at the run0|run1 seam (plain offset 6 == end of "Alpha ")
    new = "Alpha X Beta (6) end."

    doc = Docx(src)
    tracked_replace_paragraphs(doc, {0: new}, author=AC, scope="word")
    assert doc.skipped_field_paras == []          # applied, not refused
    out = tmp_path / "out.docx"; doc.save(out)
    body = _xml(out)

    # the inserted word is actually present as a tracked insertion (not dropped)
    assert "X" in "".join(_instexts(body)), "insertion at run seam was silently dropped"
    assert read_views(out)["accepted"] == new
    assert read_views(out)["rejected"] == old
    assert validate(out).ok
    _field_intact(body, "(6)")


# -- regression: insertion at the prose|field boundary lands BEFORE the field ---
def test_insertion_before_citation_field_lands_before_it(tmp_path):
    """A word inserted immediately before a live citation field must land BEFORE
    the field ("aa bb X (7) cc."), not after it with a doubled space — the
    run-seam owner must be the run ENDING at the offset (so the insert sits at
    that run's end, ahead of the following field)."""
    src = tmp_path / "before.docx"
    new_doc(src, [{"runs": [{"text": "aa "}]}])
    doc = Docx(src)
    root = doc.tree(DOCUMENT)
    p = list(root.iter(qn("w:p")))[0]
    r2 = etree.SubElement(p, qn("w:r"))
    t2 = etree.SubElement(r2, qn("w:t"))
    t2.set(qn("xml:space"), "preserve"); t2.text = "bb "
    for r in _runs(_field_runs('ADDIN ZOTERO_ITEM CSL_CITATION {"citationID":"X"}', _plain_run("(7)"))):
        p.append(r)
    sr = etree.SubElement(p, qn("w:r"))
    st = etree.SubElement(sr, qn("w:t"))
    st.set(qn("xml:space"), "preserve"); st.text = " cc."
    doc.save(src)

    assert read_views(src)["accepted"] == "aa bb (7) cc."
    new = "aa bb X (7) cc."          # insert "X " right before the field

    doc = Docx(src)
    tracked_replace_paragraphs(doc, {0: new}, author=AC, scope="word")
    assert doc.skipped_field_paras == []
    out = tmp_path / "out.docx"; doc.save(out)
    assert read_views(out)["accepted"] == new          # before the field, single-spaced
    assert read_views(out)["rejected"] == "aa bb (7) cc."
    assert validate(out).ok
    _field_intact(_xml(out), "(7)")
