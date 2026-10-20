"""unify.apply_unification: numeric markers convert BY LOCATION, and a tracked
apply is ONE op (pinned date, one spread over this op's marks only).

* Location: refextract numeric markers carry ``para`` + ``offset``; every one
  goes through ONE batched ``zoterofield.replace_occurrences_with_zotero_fields``
  call. A text search converted the FIRST same-text copy in the paragraph (a list
  enumerator before the real cite) and refused a marker cited in two paragraphs.
* Dates: every write of a tracked apply (foreign-field swap, location batch,
  text path, placeholders) carries one pinned op date and the passed author, then
  ``revisions.spread_own_marks`` runs once over THIS op's marks. Marks that
  existed before the op (any author, including this author's earlier rounds,
  even one carrying the very same date string) keep their exact ``w:date``.

Synthetic documents only; no network, no Zotero writes.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from lxml import etree

from zoterocite import Docx, new_doc, unify, zoterofield
from zoterocite.docxio import DOCUMENT
from zoterocite.ooxml import qn
from zoterocite.paras import iter_paragraphs, paragraph_text

GROUP = "http://zotero.org/groups/1234567/items/"
OP_DATE = "2026-10-19T12:00:00Z"
OLD_DATE = "2025-03-04T05:06:07Z"
AUTHOR = "Test Author"

# printed number -> (title keyword, DOI, library key, first author)
REFS = {
    1: ("Tuberous sclerosis and autism", "10.1/r1", "KEYONE01", "Adams"),
    2: ("Cortical tubers in children", "10.1/r2", "KEYTWO02", "Baker"),
    3: ("Seizure outcomes after surgery", "10.1/r3", "KEYTHR03", "Carter"),
    4: ("Language delay in infancy", "10.1/r4", "KEYFOU04", "Dunn"),
}
REFLIST = [f"[{n}] {fam} J. {t}. J Neurol. 2020;{n}:1-5."
           for n, (t, _d, _k, fam) in REFS.items()]

MENDELEY_DOI = "10.1000/widgets.2001"
MENDELEY_INSTR = " ADDIN CSL_CITATION " + json.dumps({
    "citationItems": [{
        "id": "ITEM-1",
        "itemData": {"author": [{"family": "Widget", "given": "John"}], "id": "ITEM-1",
                     "issued": {"date-parts": [["2001"]]}, "title": "A study of widgets",
                     "DOI": MENDELEY_DOI, "type": "article-journal"},
        "uris": ["http://www.mendeley.com/documents/?uuid=55ff8735"],
    }],
    "mendeley": {"formattedCitation": "(cite)", "plainTextFormattedCitation": "(cite)"},
    "properties": {"noteIndex": 0},
}) + " "


def _resolve(text, *, fetch=True):
    for title, doi, _key, fam in REFS.values():
        if title.lower() in text.lower():
            meta = {"doi": doi, "title": title, "authors": [{"family": fam, "given": "J"}],
                    "year": "2020", "journal": "J Neurol", "type": "article-journal"}
            return {"input": text, "metadata": meta, "confidence": "high",
                    "source": "crossref", "candidates": [dict(meta, score=90.0)],
                    "identifiers": {"doi": None, "pmid": None, "arxiv": None, "isbn": None}}
    return {"input": text, "metadata": None, "confidence": "none", "source": None,
            "candidates": [], "identifiers": {"doi": None, "pmid": None, "arxiv": None,
                                              "isbn": None}}


@pytest.fixture()
def net(monkeypatch):
    """Every reference is in the library (no creates); the Mendeley work too."""
    lib = {"doi": {doi: key for _t, doi, key, _f in REFS.values()}, "pmid": {}, "title": {}}
    lib["doi"][MENDELEY_DOI] = "MENKEY01"
    monkeypatch.setattr(unify.refresolve, "resolve_reference", _resolve)
    monkeypatch.setattr(unify.zotero, "library_index", lambda **kw: lib)
    monkeypatch.setattr(unify.citecheck, "ensure_retraction_db", lambda **kw: (None, None))
    monkeypatch.setattr(unify.entrez, "esearch_pmids_status",
                        lambda *a, **k: ([], {"degraded": False, "reason": None}))
    monkeypatch.setattr(unify.zotero, "key_can_write_status", lambda: True)
    monkeypatch.setattr(unify.zotero, "key_can_write", lambda: True)
    monkeypatch.setattr(unify.zotero, "item_uri", lambda key: GROUP + key)
    monkeypatch.setattr(unify.zotero, "csljson",
                        lambda keys: [{"id": k, "type": "article-journal"} for k in keys])
    monkeypatch.setattr(unify.zotero, "formatted_citations",
                        lambda keys, **kw: [f"({k})" for k in keys])

    def _no_create(*a, **k):
        raise AssertionError("every reference is in the library: nothing may be created")
    monkeypatch.setattr(unify.zotero, "create_items", _no_create)
    monkeypatch.setattr(unify, "now_iso", lambda: OP_DATE)
    return monkeypatch


def _doc(tmp_path: Path, body, name="draft.docx") -> Path:
    p = tmp_path / name
    new_doc(p, list(body) + ["References"] + REFLIST)
    return p


def _apply(path: Path, out: Path, *, track: bool, n_foreign=None):
    plan = unify.plan_unification(path)
    if n_foreign is not None:
        plan["summary"]["n_foreign_fields"] = n_foreign
    return unify.apply_unification(path, plan, {"accept": []}, out=out, track=track,
                                   author=AUTHOR)


def _paras(path: Path):
    return iter_paragraphs(Docx(path).tree(DOCUMENT))


def _text_before_field(p) -> str:
    """Accepted text of the paragraph's runs before its first field begins."""
    out = []
    for r in p.iter(qn("w:r")):
        if r.find(qn("w:fldChar")) is not None:
            break
        if any(a.tag == qn("w:del") for a in r.iterancestors()):
            continue
        out.extend(t.text or "" for t in r.iter(qn("w:t")))
    return "".join(out)


def _citations(path: Path):
    return [[it["key"] for it in c["items"]] for c in zoterofield.scan_citations(path)]


# ---------------------------------------------------------------------------
# (1)-(3) location
# ---------------------------------------------------------------------------

class TestByLocation:
    ENUM = "We ask (1) whether cells divide and (2) whether they die, as shown before (1)."

    @pytest.mark.parametrize("track", [False, True])
    def test_enumerator_before_citation_only_citation_converts(self, tmp_path, net, track):
        src = _doc(tmp_path, [self.ENUM])
        out = tmp_path / "out.docx"
        report = _apply(src, out, track=track)

        assert _citations(out) == [["KEYONE01"]]
        assert report["replaced"] == 1
        assert report["unresolved_in_doc"] == []
        body = _paras(out)[0]
        # The field sits where the CITATION was; the enumerator "(1)" (and
        # "(2)") are untouched plain text before it.
        assert _text_before_field(body) == (
            "We ask (1) whether cells divide and (2) whether they die, as shown before ")
        # Reference-list line untouched.
        assert REFLIST[0] in [paragraph_text(p) for p in _paras(out)]

    def test_same_marker_in_two_paragraphs_both_convert(self, tmp_path, net):
        src = _doc(tmp_path, ["First finding is robust [3].",
                              "A second paragraph agrees [3]."])
        out = tmp_path / "out.docx"
        report = _apply(src, out, track=True)

        assert _citations(out) == [["KEYTHR03"], ["KEYTHR03"]]
        assert report["replaced"] == 2
        assert report["unresolved_in_doc"] == []
        texts = [paragraph_text(p) for p in _paras(out)]
        assert REFLIST[2] in texts, "the '[3] ...' reference line must never convert"

    def test_grouped_marker_one_field_citing_both(self, tmp_path, net):
        src = _doc(tmp_path, ["Language follows seizures [3,4].",
                              "Another claim [1]."])
        out = tmp_path / "out.docx"
        report = _apply(src, out, track=True)

        assert _citations(out) == [["KEYTHR03", "KEYFOU04"], ["KEYONE01"]]
        assert report["replaced"] == 2   # one per PHYSICAL marker, not per key

    @pytest.mark.parametrize("track", [False, True])
    def test_several_markers_in_one_paragraph_with_text_path_marker(
            self, tmp_path, net, track):
        """Two located markers and an author-year (text-path) marker share one
        paragraph: one batched call splices right to left, BEFORE the text path,
        so no write shifts a recorded offset."""
        src = _doc(tmp_path, ["Prior work (Adams, 2020) shows this [2] and that [3]."])
        out = tmp_path / "out.docx"
        report = _apply(src, out, track=track)

        assert report["unresolved_in_doc"] == []
        assert report["replaced"] == 3
        assert _citations(out) == [["KEYONE01"], ["KEYTWO02"], ["KEYTHR03"]]

    def test_refused_occurrence_reported_not_converted(self, tmp_path, net, monkeypatch):
        """A refusal from the location owner lands in unresolved_in_doc with the
        existing shape and is not counted."""
        src = _doc(tmp_path, ["First finding is robust [3].", "Another claim [1]."])
        real = zoterofield.replace_occurrences_with_zotero_fields
        calls = []

        def _spy(doc, occs, **kw):
            calls.append(([(o["para"], o["offset"], o["anchor"]) for o in occs], kw))
            occs = [dict(o, offset=o["offset"] + 1) if o["anchor"] == "[3]" else o
                    for o in occs]   # a stale location for "[3]"
            return real(doc, occs, **kw)
        monkeypatch.setattr(unify.zoterofield, "replace_occurrences_with_zotero_fields", _spy)
        out = tmp_path / "out.docx"
        report = _apply(src, out, track=True)

        assert len(calls) == 1, "every located marker goes through ONE batched call"
        assert sorted(calls[0][0]) == [(0, 24, "[3]"), (1, 14, "[1]")]
        assert calls[0][1]["spread"] is False and calls[0][1]["date"] == OP_DATE
        assert _citations(out) == [["KEYONE01"]]
        assert report["replaced"] == 1
        (u,) = report["unresolved_in_doc"]
        assert set(u) == {"marker", "kind", "reason"}
        assert u["marker"] == "[3]" and u["kind"] == "numeric"
        assert "marker not found at its recorded location" in u["reason"]


# ---------------------------------------------------------------------------
# (4)-(5) one op: pinned date, one spread, pre-existing marks untouched
# ---------------------------------------------------------------------------

def _append_complex_field(p, instr: str):
    for kind, text in (("begin", None), (None, instr), ("separate", None),
                       (None, "(cite)"), ("end", None)):
        r = etree.SubElement(p, qn("w:r"))
        if kind:
            etree.SubElement(r, qn("w:fldChar")).set(qn("w:fldCharType"), kind)
        elif text == instr:
            it = etree.SubElement(r, qn("w:instrText"))
            it.set(qn("xml:space"), "preserve")
            it.text = instr
        else:
            etree.SubElement(r, qn("w:t")).text = text


def _append_mark(p, tag: str, rid: str, author: str, date: str, text: str):
    w = etree.SubElement(p, qn(tag))
    w.set(qn("w:id"), rid)
    w.set(qn("w:author"), author)
    w.set(qn("w:date"), date)
    r = etree.SubElement(w, qn("w:r"))
    t = etree.SubElement(r, qn("w:delText" if tag == "w:del" else "w:t"))
    t.set(qn("xml:space"), "preserve")
    t.text = text


def _one_op_doc(tmp_path: Path) -> Path:
    src = _doc(tmp_path, [
        "An earlier round by the same author.",
        "Mendeley sentence goes here.",
        "Alpha claim one [1].",
        "Beta claim two [2].",
        "Gamma claim three [3].",
        "An earlier round by someone else.",
        "An older round by the same author.",
    ], name="src.docx")
    doc = Docx(src)
    paras = iter_paragraphs(doc.tree(DOCUMENT))
    _append_complex_field(paras[1], MENDELEY_INSTR)
    # Pre-existing marks. The same author's earlier-round mark carries the VERY
    # SAME date string the op pins (only provenance, not date value, protects
    # it) and comes FIRST in document order, so a value-scoped spread would pull
    # it to the start of the window.
    _append_mark(paras[0], "w:ins", "901", AUTHOR, OP_DATE, " Added earlier.")
    _append_mark(paras[5], "w:del", "902", "Someone Else", OP_DATE, " Struck earlier.")
    _append_mark(paras[6], "w:ins", "903", AUTHOR, OLD_DATE, " Added long ago.")
    out = tmp_path / "draft.docx"
    doc.save(out)
    return out


def _marks(path: Path):
    """[(para_index, tag, w:id, w:author, w:date)] for every tracked ins/del."""
    rows = []
    for i, p in enumerate(_paras(path)):
        for el in p.iter(qn("w:ins"), qn("w:del")):
            rows.append((i, etree.QName(el).localname, el.get(qn("w:id")),
                         el.get(qn("w:author")), el.get(qn("w:date"))))
    return rows


PRE_IDS = {"901": OP_DATE, "902": OP_DATE, "903": OLD_DATE}


class TestOneOp:
    def test_tracked_apply_spreads_only_this_ops_marks(self, tmp_path, net):
        src = _one_op_doc(tmp_path)
        out = tmp_path / "out.docx"
        report = _apply(src, out, track=True, n_foreign=1)

        # 1 foreign field (citeconvert) + 3 located numeric markers.
        assert report["replaced"] == 4, report
        assert _citations(out) == [["MENKEY01"], ["KEYONE01"], ["KEYTWO02"], ["KEYTHR03"]]

        rows = _marks(out)
        pre = {rid: date for _i, _tag, rid, _a, date in rows if rid in PRE_IDS}
        assert pre == PRE_IDS, f"pre-existing marks must keep their exact w:date: {pre}"

        op = [r for r in rows if r[2] not in PRE_IDS]
        assert {a for _i, _t, _rid, a, _d in op} == {AUTHOR}, (
            "every mark this op wrote (incl. the converted foreign field) is authored "
            f"by the passed author: {op}")
        assert {i for i, *_ in op} == {1, 2, 3, 4}
        assert all(tag in ("ins", "del") for _i, tag, *_ in op)
        by_para = {}
        for i, _tag, _rid, _a, date in op:
            by_para.setdefault(i, set()).add(date)
        assert all(len(d) == 1 for d in by_para.values()), by_para   # one event per edit
        dates = [by_para[i].pop() for i in sorted(by_para)]
        # zotero-word-cite has no spread_own_marks owner (zoterofield's spread
        # falls back to a no-op), so every op mark keeps the ONE pinned op date.
        assert dates == [OP_DATE] * len(dates), dates

    def test_untracked_apply_writes_no_marks(self, tmp_path, net):
        src = _one_op_doc(tmp_path)
        out = tmp_path / "out.docx"
        report = _apply(src, out, track=False, n_foreign=1)

        assert report["replaced"] == 4
        assert _citations(out) == [["MENKEY01"], ["KEYONE01"], ["KEYTWO02"], ["KEYTHR03"]]
        assert _marks(out) == _marks(src), "an untracked apply adds/changes no tracked mark"


class TestConvertToZoteroDates:
    """citeconvert.convert_to_zotero standalone: author + one pinned date per
    call, own marks spread at the end; spread=False leaves them for the caller."""

    def _two_field_doc(self, tmp_path):
        src = tmp_path / "conv_src.docx"
        new_doc(src, ["Earlier round.", "Field one here.", "Field two here."])
        doc = Docx(src)
        paras = iter_paragraphs(doc.tree(DOCUMENT))
        # Same author, same date string as the op, FIRST in document order.
        _append_mark(paras[0], "w:ins", "901", AUTHOR, OP_DATE, " Added earlier.")
        _append_complex_field(paras[1], MENDELEY_INSTR)
        _append_complex_field(paras[2], MENDELEY_INSTR.replace("ITEM-1", "ITEM-2"))
        doc.save(src)
        return src

    @pytest.mark.parametrize("spread", [True, False])
    def test_convert_pins_date_and_spreads_own_marks(self, tmp_path, net, spread):
        from zoterocite import citeconvert
        src = self._two_field_doc(tmp_path)
        out = tmp_path / "conv_out.docx"
        res = citeconvert.convert_to_zotero(src, out=out, managers=("mendeley",), track=True,
                                            author=AUTHOR, date=OP_DATE, spread=spread)
        assert len(res["converted"]) == 2
        rows = _marks(out)
        assert [r for r in rows if r[2] == "901"][0][4] == OP_DATE
        op = [r for r in rows if r[2] != "901"]
        assert {a for *_x, a, _d in op} == {AUTHOR}
        dates = [sorted({d for i, *_x, d in op if i == pi})[0] for pi in (1, 2)]
        if spread:
            # No spread owner in zotero-word-cite: the pinned date is kept.
            assert dates == [OP_DATE, OP_DATE]
        else:
            assert dates == [OP_DATE, OP_DATE]
