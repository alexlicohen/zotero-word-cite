"""Numeric markers by LOCATION, bracket-after-digit and superscript cites
(refextract + zoterofield half of grant-side test_citelink_occurrences.py; the
document-level cite-link apply is not part of zotero-word-cite).

1. refextract records each numeric marker's ``para`` (index into
   ``paras.iter_paragraphs``) and ``offset`` (into the accepted-view
   ``paragraph_text``), so a list enumerator sharing a cited token's text is not a
   marker; ``zoterofield.replace_occurrences_with_zotero_fields`` converts exactly
   the given occurrences.
2. A bracketed marker directly after a digit ("0.05[12]") is detected. A
   parenthesis after a digit stays excluded (journal volume(issue) shapes).
3. Superscript-formatted numeric citations are detected (opt-in,
   ``extract_references(..., superscript=True)``); a superscript occurrence
   converts to a field with a superscript placeholder.

Synthetic text only; no network.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree

from zoterocite import Docx, new_doc, read_views, validate
from zoterocite import refextract, zoterofield
from zoterocite.docxio import DOCUMENT
from zoterocite.ooxml import qn
from zoterocite.paras import iter_paragraphs, paragraph_text

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
SUP = '<w:rPr><w:vertAlign w:val="superscript"/></w:rPr>'
SUB = '<w:rPr><w:vertAlign w:val="subscript"/></w:rPr>'
NOTE_SUP = ('<w:rPr><w:rStyle w:val="FootnoteReference"/>'
            '<w:vertAlign w:val="superscript"/></w:rPr>')


def r(text: str, rpr: str = "") -> str:
    return f'<w:r>{rpr}<w:t xml:space="preserve">{text}</w:t></w:r>'


def sup(text: str) -> str:
    return r(text, SUP)


def ins(inner: str, author: str = "Other Author", rid: int = 900) -> str:
    return (f'<w:ins w:id="{rid}" w:author="{author}" '
            f'w:date="2026-01-01T00:00:00Z">{inner}</w:ins>')


def dele(text: str, rid: int = 901) -> str:
    return (f'<w:del w:id="{rid}" w:author="Other Author" w:date="2026-01-01T00:00:00Z">'
            f'<w:r><w:delText xml:space="preserve">{text}</w:delText></w:r></w:del>')


def build(path: Path, blocks) -> Path:
    """A .docx whose body is *blocks*: each a paragraph's inner XML, or
    ``("table", [[cell_inner_xml, ...], ...])``."""
    new_doc(path, ["placeholder"])
    d = Docx(path)
    root = d.tree(DOCUMENT)
    body = root.find(qn("w:body"))
    for p in body.findall(qn("w:p")):
        body.remove(p)
    sect = body.find(qn("w:sectPr"))

    def _add(xml: str):
        el = etree.fromstring(xml.encode())
        if sect is not None:
            sect.addprevious(el)
        else:
            body.append(el)

    for b in blocks:
        if isinstance(b, tuple) and b[0] == "table":
            rows = "".join(
                "<w:tr>" + "".join(f"<w:tc><w:p>{c}</w:p></w:tc>" for c in row) + "</w:tr>"
                for row in b[1])
            _add(f'<w:tbl xmlns:w="{W}"><w:tblPr/>{rows}</w:tbl>')
        else:
            _add(f'<w:p xmlns:w="{W}">{b}</w:p>')
    d.save(path)
    return path


def numeric(path, **kw):
    return [m for m in refextract.extract_references(path, **kw)["intext"]
            if m["kind"] == "numeric"]


def texts(path):
    root = Docx(path).read_tree(DOCUMENT)
    return [paragraph_text(p) for p in iter_paragraphs(root)]


# ===========================================================================
# Mocks (resolver + library) and a numbered references doc
# ===========================================================================
_LIB = {f"10.7000/r{i}": f"KEYR{i:03d}" for i in range(1, 9)}


# ===========================================================================
# 1. Enumerators never become fields
# ===========================================================================
LIST_PARA = "Children showed (1) seizures, (2) delays, and (3) regression."
CITE_PARA = "Earlier cohorts reported the same pattern (2)."


class TestEnumeratorNotConverted:
    def test_refextract_records_location_and_skips_enumerators(self, tmp_path):
        p = build(tmp_path / "d.docx", [r(LIST_PARA), r(CITE_PARA)])
        ms = numeric(p)
        assert [(m["text"], m["para"]) for m in ms] == [("(2)", 1)]
        t = texts(p)
        m = ms[0]
        assert t[m["para"]][m["offset"]:m["offset"] + 3] == "(2)"


# ===========================================================================
# Paragraph indexing agrees between refextract and the apply path
# ===========================================================================
class TestParagraphIndexAgreement:
    def _draft(self, tmp_path):
        return build(tmp_path / "d.docx", [
            r("Intro sentence cites one work (1)."),                       # 0
            ("table", [[r("Cell text cites (3)."), r("Plain cell (9 rows)")]]),  # 1, 2
            r("Struck ") + dele("(4) removed ") + r("text, cite kept (1)."),  # 3
            r("References"),                                             # 4
            r("(1) Writer1 Q. Synthetic work number 1. J Synth. 2020."),  # 5
            r("(3) Writer3 Q. Synthetic work number 3. J Synth. 2020."),  # 6
            r("Acknowledgments"),                                        # 7
            r("Thanks to the cohort (3)."),                              # 8
        ])

    def test_locations_point_at_marker_text(self, tmp_path):
        p = self._draft(tmp_path)
        t = texts(p)
        got = [(m["text"], m["para"]) for m in numeric(p)]
        # Table cells are paragraphs 1-2; the struck "(4)" is not a marker; the
        # reference-list region (4-6) is skipped; paragraph 8 follows it.
        assert got == [("(1)", 0), ("(3)", 1), ("(1)", 3), ("(3)", 8)]
        for m in numeric(p):
            assert t[m["para"]][m["offset"]:m["offset"] + len(m["text"])] == m["text"]


# ===========================================================================
# 2. Bracket after a digit
# ===========================================================================
class TestMarkerAfterDigit:
    def test_bracket_after_digit_detected(self, tmp_path):
        p = build(tmp_path / "d.docx", [
            r("The effect held at p &lt; 0.05[12] in every arm."),
            r("A trial in 2019[3] confirmed it."),
        ])
        assert [m["text"] for m in numeric(p)] == ["[12]", "[3]"]

    def test_paren_after_digit_still_excluded(self, tmp_path):
        """Decision (corpus-checked): "(n)" right after a digit stays OUT — in the
        real-document net every such candidate outside a reference list was a
        journal volume(issue) shape, none a citation."""
        p = build(tmp_path / "d.docx", [
            r("The statistic x2(3) was large."),
            r("See Fig. 2(4) for the layout."),
            r("Neurology 2019;12(3):100-110 is cited inline."),
        ])
        assert numeric(p) == []


# ===========================================================================
# 3. Superscript citations — detection
# ===========================================================================
class TestSuperscriptDetection:
    def _sup(self, tmp_path, *paras):
        p = build(tmp_path / "s.docx", list(paras))
        return [(m["text"], m.get("superscript")) for m in numeric(p, superscript=True)]

    def test_single_list_range(self, tmp_path):
        got = self._sup(
            tmp_path,
            r("Lesion mapping matured") + sup("12") + r(" over a decade."),
            r("Tubers predict outcome") + sup("3,4") + r("."),
            r("Several cohorts agree.") + sup("5–7") + r(" Next sentence."),
        )
        assert got == [("12", True), ("3,4", True), ("5–7", True)]

    def test_off_by_default(self, tmp_path):
        p = build(tmp_path / "s.docx", [r("Lesion mapping matured") + sup("12") + r(".")])
        assert numeric(p) == []

    def test_split_across_runs_is_one_marker(self, tmp_path):
        p = build(tmp_path / "s.docx", [
            r("Network effects were shown") + sup("1") + sup("2,") + r(" and more.")])
        ms = numeric(p, superscript=True)
        assert [(m["text"], m["offset"]) for m in ms] == [("12", 26)]

    def test_document_order_with_brackets(self, tmp_path):
        p = build(tmp_path / "s.docx", [
            r("First claim") + sup("3") + r(", second claim (1), third") + sup("2") + r(".")])
        assert [m["text"] for m in numeric(p, superscript=True)] == ["3", "(1)", "2"]

    @pytest.mark.parametrize("inner", [
        r("A dose of 10") + sup("3") + r(" cells was used."),           # exponent after digit
        r("The area was 4 m") + sup("2") + r(" in total."),            # unit exponent
        r("Fit was good (R") + sup("2") + r(" = 0.8)."),               # symbol exponent
        r("Effect size was η") + r("p", SUB) + sup("2") + r(" = 0.1."),  # after a subscript
        r("Clearance V") + r("max", SUB) + sup("2") + r(" was high."),   # long subscript
        r("On the 1") + sup("st") + r(" visit the score rose."),       # ordinal
        r("The note marker") + r("2", NOTE_SUP) + r(" is a footnote."),  # footnote style
        sup("1") + r(" Department of Synthetic Studies, Some University."),  # para-initial
        r("Mixed run") + sup("12a") + r(" text."),                     # not numeric
        r("Starts superscript") + sup("12") + r("5 then plain."),      # number continues
    ])
    def test_negatives(self, tmp_path, inner):
        assert self._sup(tmp_path, inner) == []

    def test_footnote_reference_element_ignored(self, tmp_path):
        note = ('<w:r><w:rPr><w:rStyle w:val="FootnoteReference"/>'
                '<w:vertAlign w:val="superscript"/></w:rPr>'
                '<w:footnoteReference w:id="1"/></w:r>')
        assert self._sup(tmp_path, r("A claim with a note") + note + r(" here.")) == []

    def test_byline_affiliations_ignored(self, tmp_path):
        byline = (r("Jane A. Doe") + sup("1,2") + r(", John Roe") + sup("3")
                  + r(", Ann Poe") + sup("1"))
        assert self._sup(tmp_path, byline) == []

    def test_field_result_superscript_ignored(self, tmp_path):
        field = ('<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
                 '<w:r><w:instrText xml:space="preserve"> ADDIN ZOTERO_ITEM x </w:instrText></w:r>'
                 '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
                 + sup("4") +
                 '<w:r><w:fldChar w:fldCharType="end"/></w:r>')
        assert self._sup(tmp_path, r("Already cited") + field + r(" text.")) == []
        simple = f'<w:fldSimple w:instr=" ADDIN X ">{sup("4")}</w:fldSimple>'
        assert self._sup(tmp_path, r("Simple field") + simple + r(" text.")) == []

    def test_bracket_with_superscript_digits_counted_once(self, tmp_path):
        p = build(tmp_path / "s.docx", [r("Shown before [") + sup("2") + r("].")])
        assert [m["text"] for m in numeric(p, superscript=True)] == ["[2]"]


# ===========================================================================
# 3. Superscript citations — conversion
# ===========================================================================


# ===========================================================================
# zoterofield.replace_occurrences_with_zotero_fields (unit)
# ===========================================================================
def _live_superscript_texts(out):
    """Text of NON-deleted superscript runs outside any field."""
    root = Docx(out).read_tree(DOCUMENT)
    found = []
    for t in root.iter(qn("w:t")):
        run = t.getparent()
        va = run.find(f"{qn('w:rPr')}/{qn('w:vertAlign')}")
        if va is None or va.get(qn("w:val")) != "superscript":
            continue
        anc = run.getparent()
        deleted = False
        while anc is not None:
            if anc.tag == qn("w:del"):
                deleted = True
            anc = anc.getparent()
        if not deleted:
            found.append(t.text)
    return found


def _occ(para, offset, anchor, **kw):
    return {"para": para, "offset": offset, "anchor": anchor, "keys": ["K1"],
            "itemdata": [{"id": "K1", "type": "article-journal", "title": "T"}],
            "uris": ["http://zotero.org/groups/1/items/K1"], **kw}


class TestReplaceOccurrences:
    def test_wrong_offset_refused_untouched(self, tmp_path):
        p = build(tmp_path / "z.docx", [r("Cite here (1) and (1).")])
        doc = Docx(p)
        before = etree.tostring(doc.read_tree(DOCUMENT))
        st = zoterofield.replace_occurrences_with_zotero_fields(doc, [_occ(0, 3, "(1)")])
        assert st == ["marker not found at its recorded location"]
        assert etree.tostring(doc.read_tree(DOCUMENT)) == before

    def test_bad_para_or_offset_refused(self, tmp_path):
        p = build(tmp_path / "z.docx", [r("Cite here (1).")])
        doc = Docx(p)
        st = zoterofield.replace_occurrences_with_zotero_fields(
            doc, [_occ(5, 10, "(1)"), _occ(0, None, "(1)")])
        assert st == ["paragraph index out of range", "no marker offset"]

    def test_only_second_occurrence(self, tmp_path):
        p = build(tmp_path / "z.docx", [r("Cite here (1) and (1).")])
        doc = Docx(p)
        st = zoterofield.replace_occurrences_with_zotero_fields(
            doc, [_occ(0, 18, "(1)")], track=True)
        assert st == [None]
        out = tmp_path / "o.docx"
        doc.save(out)
        assert texts(out) == ["Cite here (1) and (citation)."]

    def test_right_to_left_keeps_offsets(self, tmp_path):
        p = build(tmp_path / "z.docx", [r("A (1) b [2] c (1).")])
        doc = Docx(p)
        st = zoterofield.replace_occurrences_with_zotero_fields(
            doc, [_occ(0, 2, "(1)"), _occ(0, 8, "[2]"), _occ(0, 14, "(1)")])
        assert st == [None, None, None]
        out = tmp_path / "o.docx"
        doc.save(out)
        assert texts(out) == ["A (citation) b (citation) c (citation)."]

    def test_offset_maps_across_tab_and_moved_text(self, tmp_path):
        """paragraph_text counts a tab and drops w:moveFrom text; the run splitter
        sees the reverse. The offset must still land on the marker."""
        moved = ('<w:moveFrom w:id="7" w:author="O" w:date="2026-01-01T00:00:00Z">'
                 + r("(1) moved away ") + '</w:moveFrom>')
        p = build(tmp_path / "z.docx", [
            r("Lead") + '<w:r><w:tab/></w:r>' + moved + r("cite (1).")])
        t = texts(p)[0]
        off = t.index("(1)")
        doc = Docx(p)
        assert zoterofield.replace_occurrences_with_zotero_fields(
            doc, [_occ(0, off, "(1)")]) == [None]
        out = tmp_path / "o.docx"
        doc.save(out)
        assert texts(out) == ["Lead\tcite (citation)."]
        raw = Docx(out).raw("word/document.xml").decode()
        assert "(1) moved away" in raw            # the moved-away text untouched

    def test_superscript_flag_requires_superscript_run(self, tmp_path):
        p = build(tmp_path / "z.docx", [r("Plain number 12 here.")])
        doc = Docx(p)
        st = zoterofield.replace_occurrences_with_zotero_fields(
            doc, [_occ(0, 13, "12", superscript=True)])
        assert st == ["marker run is no longer superscript"]

    def test_superscript_placeholder_keeps_formatting(self, tmp_path):
        p = build(tmp_path / "z.docx", [r("Claim") + sup("3") + r(".")])
        doc = Docx(p)
        st = zoterofield.replace_occurrences_with_zotero_fields(
            doc, [_occ(0, 5, "3", superscript=True,
                       rendered_html="<sup>(citation)</sup>")], track=True)
        assert st == [None]
        out = tmp_path / "o.docx"
        doc.save(out)
        assert _live_superscript_texts(out) == ["(citation)"]
        cit = zoterofield.scan_citations(out)
        assert len(cit) == 1 and [i["key"] for i in cit[0]["items"]] == ["K1"]
