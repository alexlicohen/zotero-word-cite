"""Superscript citation scan reads EFFECTIVE vertAlign, not direct formatting only.

``extract_references(path, superscript=True)`` (cite-link) used to see a
superscript only when the run carried ``<w:vertAlign w:val="superscript"/>``
directly. Word's precedence is: direct run rPr (an explicit ``baseline`` cancels
a styled superscript) > the run's character style ``basedOn`` chain > the
paragraph style's (or default paragraph style's) rPr chain > docDefaults. A
footnote/endnote reference style is recognised by its ``w:name`` (any id) or by
being ``basedOn`` one. A missing / malformed styles part degrades to the
direct-only scan. Synthetic text only.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from lxml import etree

from zoterocite import Docx, new_doc, refextract
from zoterocite.docxio import DOCUMENT
from zoterocite.ooxml import qn
from zoterocite.paras import iter_paragraphs, paragraph_text

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
STYLES_PART = "word/styles.xml"


def _cstyle(sid, name, *, based=None, va=None, kind="character", default=False):
    b = f'<w:basedOn w:val="{based}"/>' if based else ""
    rpr = f'<w:rPr><w:vertAlign w:val="{va}"/></w:rPr>' if va else ""
    d = ' w:default="1"' if default else ""
    return (f'<w:style w:type="{kind}"{d} w:styleId="{sid}">'
            f'<w:name w:val="{name}"/>{b}{rpr}</w:style>')


STYLE_DEFS = "".join([
    _cstyle("Normal", "Normal", kind="paragraph", default=True),
    _cstyle("DefaultParagraphFont", "Default Paragraph Font", default=True),
    _cstyle("CiteSup", "Cite Sup", va="superscript"),
    _cstyle("CiteSupChild", "Cite Sup Child", based="CiteSup"),
    _cstyle("CiteSupGrandchild", "Cite Sup Grandchild", based="CiteSupChild"),
    _cstyle("CycA", "Cyc A", based="CycB"),
    _cstyle("CycB", "Cyc B", based="CycA"),
    _cstyle("Dangling", "Dangling", based="NoSuchStyle"),
    _cstyle("CiteSub", "Cite Sub", va="subscript"),
    _cstyle("SupPara", "Sup Para", kind="paragraph", va="superscript"),
    _cstyle("SupParaChild", "Sup Para Child", kind="paragraph", based="SupPara"),
    _cstyle("MyNoteRef", "footnote reference", va="superscript"),
    _cstyle("MyEndRef", "Endnote Reference", va="superscript"),
    _cstyle("MyNoteRefChild", "Note Ref Child", based="MyNoteRef"),
])


def _styles_xml(defs: str = STYLE_DEFS, doc_default_va: str = "") -> bytes:
    dd = (f'<w:docDefaults><w:rPrDefault><w:rPr><w:vertAlign w:val="{doc_default_va}"/>'
          f'</w:rPr></w:rPrDefault></w:docDefaults>') if doc_default_va else ""
    return f'<w:styles xmlns:w="{W}">{dd}{defs}</w:styles>'.encode()


def r(text: str, *, style: str = "", va: str = "") -> str:
    inner = (f'<w:rStyle w:val="{style}"/>' if style else "") + \
            (f'<w:vertAlign w:val="{va}"/>' if va else "")
    rpr = f"<w:rPr>{inner}</w:rPr>" if inner else ""
    return f'<w:r>{rpr}<w:t xml:space="preserve">{text}</w:t></w:r>'


def para(inner: str, pstyle: str = "") -> str:
    ppr = f'<w:pPr><w:pStyle w:val="{pstyle}"/></w:pPr>' if pstyle else ""
    return f'<w:p xmlns:w="{W}">{ppr}{inner}</w:p>'


def build(path: Path, paras, styles: "bytes | None | str" = None) -> Path:
    """A .docx whose body is *paras* (full ``w:p`` XML). ``styles`` bytes replace
    word/styles.xml; ``"drop"`` removes the styles parts entirely."""
    new_doc(path, ["placeholder"])
    d = Docx(path)
    root = d.tree(DOCUMENT)
    body = root.find(qn("w:body"))
    for p in body.findall(qn("w:p")):
        body.remove(p)
    sect = body.find(qn("w:sectPr"))
    for xml in paras:
        el = etree.fromstring(xml.encode())
        if sect is not None:
            sect.addprevious(el)
        else:
            body.append(el)
    if isinstance(styles, bytes):
        d.set_part_bytes(STYLES_PART, styles)
    d.save(path)
    if styles == "drop":
        tmp = path.with_suffix(".tmp")
        with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                if info.filename in (STYLES_PART, "word/stylesWithEffects.xml"):
                    continue
                zout.writestr(info, zin.read(info.filename))
        tmp.replace(path)
    return path


def sup_markers(path):
    return [(m["text"], m["para"], m["offset"])
            for m in refextract.extract_references(path, superscript=True)["intext"]
            if m["kind"] == "numeric" and m.get("superscript")]


def _ptext(path, i):
    return paragraph_text(iter_paragraphs(Docx(path).read_tree(DOCUMENT))[i])


PREFIX = "Lesion mapping matured"   # 22 chars


# ===========================================================================
# Character / paragraph style resolution
# ===========================================================================
class TestEffectiveVertAlign:
    def test_character_style_superscript_detected(self, tmp_path):
        p = build(tmp_path / "d.docx", [
            para(r("Intro sentence without cites.")),
            para(r(PREFIX) + r("12", style="CiteSup") + r(" over a decade.")),
        ], _styles_xml())
        got = sup_markers(p)
        assert got == [("12", 1, 22)]
        assert _ptext(p, 1)[22:24] == "12"

    def test_based_on_superscript_style_detected(self, tmp_path):
        p = build(tmp_path / "d.docx", [
            para(r(PREFIX) + r("3,4", style="CiteSupChild") + r(".")),
            para(r(PREFIX) + r("5", style="CiteSupGrandchild") + r(" again.")),
        ], _styles_xml())
        assert sup_markers(p) == [("3,4", 0, 22), ("5", 1, 22)]

    def test_direct_baseline_cancels_style_superscript(self, tmp_path):
        p = build(tmp_path / "d.docx", [
            para(r(PREFIX) + r("12", style="CiteSup", va="baseline") + r(" here.")),
            para(r(PREFIX + " ") + r("7", style="CiteSup", va="baseline") + r(" here.")),
        ], _styles_xml())
        assert sup_markers(p) == []

    def test_direct_baseline_cancels_paragraph_style_superscript(self, tmp_path):
        # Prose runs cancel the paragraph style's superscript; the marker run inherits it.
        p = build(tmp_path / "d.docx", [
            para(r(PREFIX, va="baseline") + r("12") + r(" over a decade.", va="baseline"),
                 pstyle="SupPara"),
        ], _styles_xml())
        assert sup_markers(p) == [("12", 0, 22)]

    def test_paragraph_style_superscript_detected(self, tmp_path):
        p = build(tmp_path / "d.docx", [
            para(r(PREFIX, va="baseline") + r("8,9") + r(" next.", va="baseline"),
                 pstyle="SupParaChild"),
            # same shape without the paragraph style: nothing superscript
            para(r(PREFIX, va="baseline") + r("8,9") + r(" next.", va="baseline")),
        ], _styles_xml())
        assert sup_markers(p) == [("8,9", 0, 22)]

    def test_default_paragraph_style_used_when_no_pstyle(self, tmp_path):
        defs = STYLE_DEFS.replace(
            _cstyle("Normal", "Normal", kind="paragraph", default=True),
            _cstyle("Normal", "Normal", kind="paragraph", default=True, va="superscript"))
        p = build(tmp_path / "d.docx", [
            para(r(PREFIX, va="baseline") + r("6") + r(" next.", va="baseline")),
        ], _styles_xml(defs))
        assert sup_markers(p) == [("6", 0, 22)]

    def test_doc_defaults_superscript_detected(self, tmp_path):
        p = build(tmp_path / "d.docx", [
            para(r(PREFIX, va="baseline") + r("6") + r(" next.", va="baseline")),
        ], _styles_xml(doc_default_va="superscript"))
        assert sup_markers(p) == [("6", 0, 22)]

    def test_character_style_beats_paragraph_style(self, tmp_path):
        # Paragraph style superscript, but the marker's character style says baseline.
        defs = STYLE_DEFS + _cstyle("CiteBase", "Cite Base", va="baseline")
        p = build(tmp_path / "d.docx", [
            para(r(PREFIX, va="baseline") + r("12", style="CiteBase") + r(" x.", va="baseline"),
                 pstyle="SupPara"),
        ], _styles_xml(defs))
        assert sup_markers(p) == []

    def test_cycle_and_dangling_based_on_tolerated(self, tmp_path):
        p = build(tmp_path / "d.docx", [
            para(r(PREFIX) + r("12", style="CycA") + r(" here.")),
            para(r(PREFIX) + r("13", style="Dangling") + r(" here.")),
            para(r(PREFIX) + r("14", style="NoSuchStyle") + r(" here.")),
            para(r(PREFIX) + r("15", style="CiteSup") + r(" here."), pstyle="NoSuchPara"),
        ], _styles_xml())
        assert sup_markers(p) == [("15", 3, 22)]

    def test_subscript_via_style_blocks_following_superscript(self, tmp_path):
        # "Vmax²": the subscripted "max" (4-letter token, so the letter guard does not
        # fire) makes the following superscript an exponent, not a citation.
        p = build(tmp_path / "d.docx", [
            para(r("The fitted parameter V") + r("max", style="CiteSub")
                 + r("2", va="superscript") + r(" was stable.")),
            para(r("The fitted parameter V") + r("max")
                 + r("2", va="superscript") + r(" was stable.")),
        ], _styles_xml())
        assert sup_markers(p) == [("2", 1, 25)]


# ===========================================================================
# Note-reference styles recognised by name / basedOn
# ===========================================================================
class TestNoteRefStyles:
    @pytest.mark.parametrize("sid", ["MyNoteRef", "MyEndRef", "MyNoteRefChild"])
    def test_custom_id_note_reference_excluded(self, tmp_path, sid):
        p = build(tmp_path / "d.docx", [
            para(r(PREFIX) + r("12", style=sid) + r(" here.")),
            para(r(PREFIX) + r("12", style=sid, va="superscript") + r(" here.")),
        ], _styles_xml())
        assert sup_markers(p) == []

    def test_builtin_id_still_excluded_without_styles(self, tmp_path):
        p = build(tmp_path / "d.docx", [
            para(r(PREFIX) + r("12", style="FootnoteReference", va="superscript") + r(" here.")),
        ], "drop")
        assert sup_markers(p) == []


# ===========================================================================
# Degradation and the default path
# ===========================================================================
class TestDegradeAndDefault:
    def _paras(self):
        return [
            para(r(PREFIX) + r("12", va="superscript") + r(" direct.")),
            para(r(PREFIX) + r("34", style="CiteSup") + r(" styled.")),
        ]

    def test_no_styles_part_is_direct_only(self, tmp_path):
        p = build(tmp_path / "d.docx", self._paras(), "drop")
        assert not Docx(p).has(STYLES_PART)
        assert sup_markers(p) == [("12", 0, 22)]

    def test_malformed_styles_part_is_direct_only(self, tmp_path):
        p = build(tmp_path / "d.docx", self._paras(), b"<w:styles not xml")
        assert sup_markers(p) == [("12", 0, 22)]

    def test_with_styles_both_found(self, tmp_path):
        p = build(tmp_path / "d.docx", self._paras(), _styles_xml())
        assert sup_markers(p) == [("12", 0, 22), ("34", 1, 22)]

    @pytest.mark.parametrize("styles", ["drop", "styles", "malformed"])
    def test_superscript_off_never_reads_styles(self, tmp_path, monkeypatch, styles):
        src = {"drop": "drop", "styles": _styles_xml(),
               "malformed": b"<w:styles not xml"}[styles]
        p = build(tmp_path / "d.docx", self._paras() + [
            para(r(PREFIX, va="baseline") + r("8") + r(" x.", va="baseline"), pstyle="SupPara"),
        ], src)
        baseline = refextract.extract_references(p)

        def _boom(*a, **k):
            raise AssertionError("styles must not be read when superscript=False")

        monkeypatch.setattr(refextract, "_load_sup_styles", _boom)
        again = refextract.extract_references(p)
        assert again == baseline
        assert not [m for m in again["intext"] if m.get("superscript")]
