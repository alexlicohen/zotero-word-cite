"""Marker-to-field conversion across tracked changes and existing fields (round 3 #16, #18).

#16  A citation marker whose text crosses tracked containers (an earlier review
     corrected a year or a name inside it) converts with track=True instead of
     being refused: base runs and inserted runs are struck in place (zotero-word-cite
     has no own-insertion owner, so the active author's runs are struck too), and the
     field is a tracked insertion right after the span. The accepted view reads
     the field result where the marker was; the rejected view is unchanged. When
     the result equals the old text over plain contiguous untracked runs, the
     swap is untracked. Integrity refusals (moveTo host, hyperlink / content
     control boundary, field result) still refuse and are reported.
#18  Matching never re-hits an existing field's result (masked), so rerunning a
     conversion is a no-op and a second same-text marker is the one found.

Plus the timestamp fallback of the multi-mark writers (no spread owner in zotero-word-cite).
Synthetic text only; no network.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree

from zoterocite import Docx, new_doc, read_views, validate, zoterofield
from zoterocite.docxio import DOCUMENT
from zoterocite.ooxml import qn
from zoterocite.paras import iter_paragraphs, paragraph_text

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
ME = "Active Author"
OTHER = "Other Reviewer"
OLD = "2026-01-05T09:00:00Z"
OP = "2026-03-02T10:00:00Z"
KEYS = ["KEYA0001"]
ITEMDATA = [{"id": "KEYA0001", "type": "article-journal", "title": "Invented study"}]
URIS = ["http://zotero.org/groups/1/items/KEYA0001"]


def r(text: str, rpr: str = "") -> str:
    return f'<w:r>{rpr}<w:t xml:space="preserve">{text}</w:t></w:r>'


def ins(inner: str, author: str = OTHER, rid: int = 900, date: str = OLD) -> str:
    return f'<w:ins w:id="{rid}" w:author="{author}" w:date="{date}">{inner}</w:ins>'


def dele(text: str, author: str = OTHER, rid: int = 901, date: str = OLD) -> str:
    return (f'<w:del w:id="{rid}" w:author="{author}" w:date="{date}">'
            f'<w:r><w:delText xml:space="preserve">{text}</w:delText></w:r></w:del>')


def build(path: Path, paras) -> Path:
    new_doc(path, ["placeholder"])
    d = Docx(path)
    body = d.tree(DOCUMENT).find(qn("w:body"))
    for p in body.findall(qn("w:p")):
        body.remove(p)
    sect = body.find(qn("w:sectPr"))
    for inner in paras:
        sect.addprevious(etree.fromstring(f'<w:p xmlns:w="{W}">{inner}</w:p>'.encode()))
    d.save(path)
    return path


def convert(path: Path, anchor: str, rendered: str, *, track=True, out=None, **kw) -> Path:
    doc = Docx(path)
    zoterofield.replace_text_with_zotero_field(
        doc, anchor, KEYS, itemdata=ITEMDATA, uris=URIS, rendered=rendered,
        track=track, author=ME, date=OP, **kw)
    out = out or path.with_name(path.stem + ".out.docx")
    doc.save(out)
    return out


def root_of(path: Path):
    return Docx(path).read_tree(DOCUMENT)


def nested_ins_count(root) -> int:
    return sum(1 for el in root.iter(qn("w:ins"))
               if next(el.iterancestors(qn("w:ins")), None) is not None)


def views(path: Path):
    v = read_views(path)
    return v["accepted"], v["rejected"]


def field_count(path: Path) -> int:
    return len(zoterofield.scan_citations(path))


# ===========================================================================
# #16 tracked splice across ins/del
# ===========================================================================
class TestTrackedSplice:
    def test_year_corrected_by_another_reviewer(self, tmp_path):
        # "(Smith et al., 2019)" -> another reviewer struck 2019 and inserted 2020.
        p = build(tmp_path / "d.docx", [
            r("Prior work ") + r("(Smith et al., ") + dele("2019") + ins(r("2020"))
            + r(") shows it.")])
        acc0, rej0 = views(p)
        assert acc0 == "Prior work (Smith et al., 2020) shows it."
        out = convert(p, "(Smith et al., 2020)", "(Smith & Lee, 2020)")
        acc, rej = views(out)
        assert acc == "Prior work (Smith & Lee, 2020) shows it."
        assert rej == rej0 == "Prior work (Smith et al., 2019) shows it."
        root = root_of(out)
        # the other reviewer's deletion is untouched; their inserted "2020" is
        # struck IN PLACE (ins > del by the active author)
        assert [d.get(qn("w:author")) for d in root.iter(qn("w:del"))
                if "".join(d.itertext()) == "2019"] == [OTHER]
        struck = [d for d in root.iter(qn("w:del")) if "".join(d.itertext()) == "2020"]
        assert len(struck) == 1 and struck[0].get(qn("w:author")) == ME
        assert struck[0].getparent().tag == qn("w:ins")
        assert struck[0].getparent().get(qn("w:author")) == OTHER
        assert nested_ins_count(root) == 0
        assert field_count(out) == 1
        assert validate(out).ok

    # zotero-word-cite's revisions has no ``_own_ins_host`` (no zotero-word-cite caller converts a
    # marker inside the tool author's own pending insertion), so zoterofield's
    # fallback treats every insertion as foreign: the run is struck instead of
    # dropped. Same accepted/rejected views, more marks; still valid OOXML.
    def test_own_pending_insertion_fallback_struck_views_kept(self, tmp_path):
        p = build(tmp_path / "d.docx", [
            r("(Smith et al., ") + dele("2019", author=ME) + ins(r("2020"), author=ME)
            + r(") here.")])
        _, rej0 = views(p)
        out = convert(p, "(Smith et al., 2020)", "(Smith, 2020)")
        acc, rej = views(out)
        assert acc == "(Smith, 2020) here."
        assert rej == rej0 == "(Smith et al., 2019) here."
        root = root_of(out)
        deleted = ["".join(d.itertext()) for d in root.iter(qn("w:del"))]
        assert "2020" in deleted                        # fallback: struck, not dropped
        assert "2019" in deleted                        # the earlier deletion stays
        assert nested_ins_count(root) == 0
        assert field_count(out) == 1
        assert validate(out).ok

    def test_marker_ending_inside_own_insertion_fallback(self, tmp_path):
        p = build(tmp_path / "d.docx", [
            r("see (Lee, ") + ins(r("2018) and more"), author=ME, date=OLD)])
        out = convert(p, "(Lee, 2018)", "(Lee & Ng, 2018)")
        acc, rej = views(out)
        assert acc == "see (Lee & Ng, 2018) and more"
        assert rej == "see (Lee,"   # read_views strips
        root = root_of(out)
        assert nested_ins_count(root) == 0
        assert field_count(out) == 1
        # the pre-existing insertion keeps its date; the field is a new insertion
        assert {i.get(qn("w:date")) for i in root.iter(qn("w:ins"))
                if i.find(".//" + qn("w:instrText")) is None} == {OLD}
        assert validate(out).ok

    def test_foreign_insertion_split_so_field_lands_at_marker(self, tmp_path):
        p = build(tmp_path / "d.docx", [
            r("see (Smith, ") + ins(r("2020) for detail"))])
        out = convert(p, "(Smith, 2020)", "(Smith & Diaz, 2020)")
        acc, rej = views(out)
        assert acc == "see (Smith & Diaz, 2020) for detail"
        assert rej == "see (Smith,"   # read_views strips
        root = root_of(out)
        top = [el for el in root.find(qn("w:body")).find(qn("w:p"))
               if el.tag == qn("w:ins")]
        assert [i.get(qn("w:author")) for i in top] == [OTHER, ME, OTHER]
        assert len({i.get(qn("w:id")) for i in root.iter(qn("w:ins"))}) == len(
            list(root.iter(qn("w:ins"))))               # fresh id on the fragment
        assert nested_ins_count(root) == 0
        assert validate(out).ok

    def test_mixed_base_foreign_own_and_deletion(self, tmp_path):
        p = build(tmp_path / "d.docx", [
            r("As shown (Ab") + ins(r("c, "), rid=910) + ins(r("20"), author=ME, rid=911)
            + dele("19", rid=912) + r("21) twice.")])
        acc0, rej0 = views(p)
        assert acc0 == "As shown (Abc, 2021) twice."
        out = convert(p, "(Abc, 2021)", "(Abc & Xu, 2021)")
        acc, rej = views(out)
        assert acc == "As shown (Abc & Xu, 2021) twice."
        assert rej == rej0
        assert nested_ins_count(root_of(out)) == 0
        assert validate(out).ok

    def test_marker_wholly_inside_foreign_insertion(self, tmp_path):
        p = build(tmp_path / "d.docx", [r("Text ") + ins(r("claim (Lee, 2018) here"))])
        out = convert(p, "(Lee, 2018)", "(Lee et al., 2018)")
        acc, rej = views(out)
        assert acc == "Text claim (Lee et al., 2018) here"
        assert rej == "Text"
        assert nested_ins_count(root_of(out)) == 0     # was ins-in-ins before
        assert validate(out).ok

    def test_occurrence_api_reports_success_across_insertion(self, tmp_path):
        p = build(tmp_path / "d.docx", [r("x (Kim, ") + ins(r("2017)")) + r(".")])
        t = paragraph_text(iter_paragraphs(root_of(p))[0])
        doc = Docx(p)
        st = zoterofield.replace_occurrences_with_zotero_fields(doc, [{
            "para": 0, "offset": t.index("(Kim"), "anchor": "(Kim, 2017)",
            "keys": KEYS, "itemdata": ITEMDATA, "uris": URIS, "rendered": "(Kim, 2017a)"}],
            track=True, author=ME, date=OP)
        assert st == [None]

    def test_untracked_mode_still_refuses_tracked_boundary(self, tmp_path):
        p = build(tmp_path / "d.docx", [r("(Smith, ") + ins(r("2020)"))])
        doc = Docx(p)
        before = etree.tostring(doc.tree(DOCUMENT))
        with pytest.raises(LookupError, match="tracked insertion"):
            zoterofield.replace_text_with_zotero_field(
                doc, "(Smith, 2020)", KEYS, itemdata=ITEMDATA, uris=URIS,
                rendered="(Smith, 2020)", track=False, author=ME)
        assert etree.tostring(doc.tree(DOCUMENT)) == before


class TestUntrackedSwap:
    def test_equal_result_over_plain_runs_swaps_untracked(self, tmp_path):
        p = build(tmp_path / "d.docx", [r("Known (Smith et al., ") + r("2020) result.")])
        acc0, rej0 = views(p)
        out = convert(p, "(Smith et al., 2020)", "(Smith et al., 2020)")
        root = root_of(out)
        assert root.find(".//" + qn("w:del")) is None
        assert root.find(".//" + qn("w:ins")) is None
        assert views(out) == (acc0, rej0)
        assert field_count(out) == 1
        assert validate(out).ok

    def test_different_result_is_tracked(self, tmp_path):
        p = build(tmp_path / "d.docx", [r("Known (Smith et al., 2020) result.")])
        out = convert(p, "(Smith et al., 2020)", "(Smith & Lee, 2020)")
        root = root_of(out)
        assert root.find(".//" + qn("w:del")) is not None
        assert root.find(".//" + qn("w:ins")) is not None

    def test_equal_result_across_insertion_is_tracked(self, tmp_path):
        p = build(tmp_path / "d.docx", [r("Known (Smith et al., ") + ins(r("2020)"))])
        out = convert(p, "(Smith et al., 2020)", "(Smith et al., 2020)")
        root = root_of(out)
        assert root.find(".//" + qn("w:del")) is not None
        acc, rej = views(out)
        assert acc == "Known (Smith et al., 2020)" and rej == "Known (Smith et al.,"

    def test_equal_result_over_noncontiguous_runs_is_tracked(self, tmp_path):
        # a bookmark between the marker runs: not one contiguous span
        p = build(tmp_path / "d.docx", [
            r("Known (Smith, ") + '<w:bookmarkStart w:id="5" w:name="bm"/>'
            + r("2020) here.") + '<w:bookmarkEnd w:id="5"/>'])
        out = convert(p, "(Smith, 2020)", "(Smith, 2020)")
        assert root_of(out).find(".//" + qn("w:del")) is not None
        acc, rej = views(out)
        assert acc == rej == "Known (Smith, 2020) here."


class TestIntegrityRefusals:
    def _status(self, tmp_path, inner, anchor):
        p = build(tmp_path / "d.docx", [inner])
        t = paragraph_text(iter_paragraphs(root_of(p))[0])
        doc = Docx(p)
        before = etree.tostring(doc.tree(DOCUMENT))
        st = zoterofield.replace_occurrences_with_zotero_fields(doc, [{
            "para": 0, "offset": t.index(anchor), "anchor": anchor, "keys": KEYS,
            "itemdata": ITEMDATA, "uris": URIS}], track=True, author=ME, date=OP)
        assert etree.tostring(doc.tree(DOCUMENT)) == before
        return st[0]

    def test_move_destination_refused(self, tmp_path):
        inner = (r("A ") + f'<w:moveTo w:id="70" w:author="{OTHER}" w:date="{OLD}">'
                 + r("(Ng, 2016)") + '</w:moveTo>')
        assert "w:moveTo" in self._status(tmp_path, inner, "(Ng, 2016)")

    def test_hyperlink_boundary_refused(self, tmp_path):
        inner = r("A (Ng, ") + f'<w:hyperlink w:anchor="x">{r("2016) b")}</w:hyperlink>'
        assert "inline container" in self._status(tmp_path, inner, "(Ng, 2016)")

    def test_content_control_boundary_refused(self, tmp_path):
        inner = (r("A (Ng, ") + '<w:sdt><w:sdtPr/><w:sdtContent>' + r("2016) b")
                 + '</w:sdtContent></w:sdt>')
        assert "inline container" in self._status(tmp_path, inner, "(Ng, 2016)")

    def test_field_result_refused(self, tmp_path):
        fld = zoterofield._build_zotero_field_xml(["s"], KEYS, ITEMDATA, URIS,
                                                  rendered="(Ng, 2016)")
        assert "existing field result" in self._status(tmp_path, r("A ") + fld, "(Ng, 2016)")

    def test_nested_tracked_change_refused(self, tmp_path):
        inner = r("A ") + ins(f'<w:hyperlink w:anchor="x">{r("(Ng, 2016)")}</w:hyperlink>')
        assert "nested tracked change" in self._status(tmp_path, inner, "(Ng, 2016)")

    def test_insertion_nested_in_insertion_refused(self, tmp_path):
        # The run's own w:ins sits inside another w:ins: the new field's w:ins
        # would land inside a tracked change (the `lv.tag in _REV_TAGS` arm).
        inner = r("A ") + ins(ins(r("(Ng, 2016)"), author=ME, rid=902))
        assert "nested tracked change" in self._status(tmp_path, inner, "(Ng, 2016)")

    def test_post_splice_check_restores_on_mismatch(self, tmp_path, monkeypatch):
        p = build(tmp_path / "d.docx", [r("x (Kim, ") + ins(r("2017)")) + r(".")])
        doc = Docx(p)
        before = etree.tostring(doc.tree(DOCUMENT))
        # sabotage: the tracked splice "forgets" to strike anything
        monkeypatch.setattr(zoterofield, "_own_insertion", lambda run, para, author: run.getparent())
        with pytest.raises(LookupError, match="post-splice check failed"):
            zoterofield.replace_text_with_zotero_field(
                doc, "(Kim, 2017)", KEYS, itemdata=ITEMDATA, uris=URIS,
                rendered="(Kim, 2017a)", track=True, author=ME, date=OP)
        assert etree.tostring(doc.tree(DOCUMENT)) == before


# ===========================================================================
# #18 field results are masked from matching
# ===========================================================================
class TestFieldResultMask:
    def test_rerun_is_noop(self, tmp_path):
        p = build(tmp_path / "d.docx", [r("Known (Smith et al., 2020) result.")])
        out = convert(p, "(Smith et al., 2020)", "(Smith et al., 2020)")
        doc = Docx(out)
        before = etree.tostring(doc.tree(DOCUMENT))
        with pytest.raises(LookupError, match="field results"):
            zoterofield.replace_text_with_zotero_field(
                doc, "(Smith et al., 2020)", KEYS, itemdata=ITEMDATA, uris=URIS,
                rendered="(Smith et al., 2020)", track=True, author=ME, date=OP)
        assert zoterofield.replace_all_text_with_zotero_field(
            doc, "(Smith et al., 2020)", KEYS, itemdata=ITEMDATA, uris=URIS,
            rendered="(citation)", track=True, author=ME, date=OP) == 0
        assert etree.tostring(doc.tree(DOCUMENT)) == before

    def test_second_occurrence_is_found(self, tmp_path):
        p = build(tmp_path / "d.docx", [r("First (Lee, 2018) then again (Lee, 2018).")])
        once = convert(p, "(Lee, 2018)", "(Lee, 2018)")
        twice = convert(once, "(Lee, 2018)", "(Lee, 2018)", out=tmp_path / "t.docx")
        assert field_count(twice) == 2
        acc, rej = views(twice)
        assert acc == rej == "First (Lee, 2018) then again (Lee, 2018)."
        # the second field sits at the second marker, not nested in the first
        root = root_of(twice)
        assert len(list(root.iter(qn("w:instrText")))) == 2
        assert all(next(f.iterancestors(qn("w:instrText")), None) is None
                   for f in root.iter(qn("w:instrText")))

    def test_marker_cannot_straddle_a_field_result(self, tmp_path):
        fld = zoterofield._build_zotero_field_xml(["s"], KEYS, ITEMDATA, URIS, rendered="B")
        p = build(tmp_path / "d.docx", [r("A") + fld + r("C tail")])
        assert paragraph_text(iter_paragraphs(root_of(p))[0]) == "ABC tail"
        doc = Docx(p)
        with pytest.raises(LookupError):
            zoterofield.replace_text_with_zotero_field(
                doc, "ABC", KEYS, itemdata=ITEMDATA, uris=URIS, rendered="X",
                track=True, author=ME, date=OP)

    def test_occurrence_inside_field_result_is_refused(self, tmp_path):
        fld = zoterofield._build_zotero_field_xml(["s"], KEYS, ITEMDATA, URIS, rendered="(1)")
        p = build(tmp_path / "d.docx", [r("Cite ") + fld + r(" and (1).")])
        doc = Docx(p)
        st = zoterofield.replace_occurrences_with_zotero_fields(doc, [
            {"para": 0, "offset": 5, "anchor": "(1)", "keys": KEYS,
             "itemdata": ITEMDATA, "uris": URIS},
            {"para": 0, "offset": 13, "anchor": "(1)", "keys": KEYS,
             "itemdata": ITEMDATA, "uris": URIS}], track=True, author=ME, date=OP)
        assert "existing field result" in st[0]     # offset 5 is the field's result
        assert st[1] is None                         # the plain "(1)" converts


# ===========================================================================
# authored-mark timestamps
# ===========================================================================
def _mark_dates(root, author=ME):
    """[(paragraph index, w:date)] for the author's dated marks, document order."""
    out = []
    for i, p in enumerate(iter_paragraphs(root)):
        for el in p.iter():
            if el.get(qn("w:author")) == author and el.get(qn("w:date")):
                out.append((i, el.get(qn("w:date"))))
    return out


class TestTimestamps:
    """zotero-word-cite's revisions has no ``snapshot_dated_marks``/``spread_own_marks``
    (no zotero-word-cite caller runs these multi-mark writers with track=True), so
    zoterofield skips the trailing spread: this op's marks share the op date and
    every pre-existing mark keeps its exact ``w:date``."""

    def test_occurrences_fallback_old_marks_kept(self, tmp_path):
        p = build(tmp_path / "d.docx", [
            r("Earlier ") + ins(r("edit"), author=ME, rid=950, date=OLD),
            r("One (Kim, 2017) here."),
            r("Two (Kim, 2017) there.")])
        doc = Docx(p)
        texts = [paragraph_text(q) for q in iter_paragraphs(doc.tree(DOCUMENT))]
        occ = [{"para": i, "offset": texts[i].index("(Kim"), "anchor": "(Kim, 2017)",
                "keys": KEYS, "itemdata": ITEMDATA, "uris": URIS,
                "rendered": "(Kim, 2017a)"} for i in (1, 2)]
        assert zoterofield.replace_occurrences_with_zotero_fields(
            doc, occ, track=True, author=ME, date=OP) == [None, None]
        dates = _mark_dates(doc.tree(DOCUMENT))
        assert [d for i, d in dates if i == 0] == [OLD]         # prior mark untouched
        assert {d for i, d in dates if i in (1, 2)} == {OP}     # no spread: op date

    def test_replace_all_fallback(self, tmp_path):
        p = build(tmp_path / "d.docx", [r("One [CITE: k] here."), r("Two [CITE: k] there.")])
        doc = Docx(p)
        n = zoterofield.replace_all_text_with_zotero_field(
            doc, "[CITE: k]", KEYS, itemdata=ITEMDATA, uris=URIS, rendered="(citation)",
            track=True, author=ME, date=OP)
        assert n == 2
        assert {x for _, x in _mark_dates(doc.tree(DOCUMENT))} == {OP}
