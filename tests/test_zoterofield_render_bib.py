"""Pre-rendered grouped / narrative citations and a placed bibliography (round 3 #17, #19, #20).

#17  A multi-key citation is pre-rendered (author-date styles) instead of the
     '(citation)' placeholder: per-item renders without their parentheses,
     sorted by an accent-folded key, joined with '; '. Unavailable renders keep
     the placeholder and warn.
#19  CSL suppress-author on citation items (extras + cite_into + CLI): the field
     pre-renders the year part only.
#20  A pre-rendered multi-paragraph ZOTERO_BIBL under a named heading: begin in
     the first entry, end in the last, tracked paragraph insertion, Bibliography
     paragraph style added once and applied.

Synthetic text only; every Zotero call is monkeypatched.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from lxml import etree

from zoterocite import Docx, cli, new_doc, read_views, validate, zotero, zoterofield
from zoterocite.docxio import DOCUMENT
from zoterocite.ooxml import qn
from zoterocite.paras import iter_paragraphs, paragraph_text

ME = "Active Author"
OP = "2026-03-02T10:00:00Z"
GROUP = "1234567"
APA = {   # invented per-item author-date renders (Zotero include=citation HTML)
    "KOSC": "<span>(Óscar &amp; Pérez, 2019)</span>",
    "KOLI": "(Oliveira et al., 2018)",
    "KZHA": "(Zhang, 2017)",
    "KSM9": "(Smith, 2019)",
    "KSM0": "(Smith, 2020)",
}
BIB = {   # invented include=bib HTML in Zotero's wrapper shape
    "KOSC": '<div class="csl-bib-body" style="line-height: 2;">\n  <div class="csl-entry">'
            'Óscar, A., &amp; Pérez, B. (2019). Invented title. <i>J Synth</i>, 1, 2–3.</div>\n</div>',
    "KOLI": '<div class="csl-bib-body">\n  <div class="csl-entry">Oliveira, C., et al. (2018). '
            'Another invented title. <i>Synth Rev</i>, 4.</div>\n</div>',
    "KZHA": '<div class="csl-bib-body">\n  <div class="csl-entry">Zhang, D. (2017). Third '
            'invented title. <i>Made Up</i>, 9.</div>\n</div>',
}


def _renders(table):
    def fc(keys, style="apa", kind="bib", strip=True):
        return [table[k] for k in keys if k in table]
    return fc


def _patch_zotero(monkeypatch, table=APA, bib=BIB):
    monkeypatch.setattr(zotero, "csljson", lambda keys: [
        {"id": f"{GROUP}/{k}", "type": "article-journal", "title": f"Invented {k}"} for k in keys])
    monkeypatch.setattr(zotero, "item_uri_offline_safe",
                        lambda k: f"http://zotero.org/groups/{GROUP}/items/{k}")

    def fc(keys, style="apa", kind="bib", strip=True):
        src = table if kind == "citation" else bib
        return [src[k] for k in keys if k in src]
    monkeypatch.setattr(zotero, "formatted_citations", fc)


def _citation_json(path: Path):
    body = Docx(path).raw("word/document.xml").decode()
    m = re.search(r"CSL_CITATION (\{.*?\}) </w:instrText>", body)
    return json.loads(m.group(1).replace("&amp;", "&").replace("&quot;", '"')
                      .replace("&lt;", "<").replace("&gt;", ">"))


def _field_result(path: Path) -> str:
    root = Docx(path).read_tree(DOCUMENT)
    out, state = [], None
    for r in root.iter(qn("w:r")):
        fc = r.find(qn("w:fldChar"))
        if fc is not None:
            k = fc.get(qn("w:fldCharType"))
            state = "res" if k == "separate" else (None if k == "end" else state)
            continue
        if state == "res":
            out.extend(t.text or "" for t in r.iter(qn("w:t")))
    return "".join(out)


# ===========================================================================
# #17 grouped pre-render
# ===========================================================================
class TestGroupRender:
    def test_accent_folded_sort_and_join(self):
        got = zoterofield.group_citation_html(
            [APA["KZHA"], APA["KOSC"], APA["KOLI"]])
        assert zoterofield._strip_html(got) == \
            "(Oliveira et al., 2018; Óscar & Pérez, 2019; Zhang, 2017)"

    def test_same_author_years_in_order(self):
        got = zoterofield.group_citation_html([APA["KSM0"], APA["KSM9"]])
        assert got == "(Smith, 2019; Smith, 2020)"

    def test_formatting_tags_survive(self):
        got = zoterofield.group_citation_html(["(<i>Synthetic Report</i>, 2015)", "(Ames, 2014)"])
        assert got == "(Ames, 2014; <i>Synthetic Report</i>, 2015)"

    def test_numeric_or_unparenthesised_renders_have_no_grouped_form(self):
        assert zoterofield.group_citation_html(["(1)", "(2)"]) is None
        assert zoterofield.group_citation_html(["<sup>1</sup>", "<sup>2</sup>"]) is None
        assert zoterofield.group_citation_html(["Smith 2019", "(Lee, 2018)"]) is None

    def test_cite_into_multi_key_prerenders(self, tmp_path, monkeypatch):
        _patch_zotero(monkeypatch)
        src = tmp_path / "s.docx"
        new_doc(src, ["A claim citing three invented papers here."])
        out = tmp_path / "o.docx"
        zoterofield.cite_into(src, "three invented papers", keys=["KZHA", "KOSC", "KOLI"],
                              style="apa", out=out)
        want = "(Oliveira et al., 2018; Óscar & Pérez, 2019; Zhang, 2017)"
        assert _field_result(out) == want
        props = _citation_json(out)["properties"]
        assert props["plainCitation"] == want
        assert "(citation)" not in Docx(out).raw("word/document.xml").decode()
        assert validate(out).ok

    def test_unavailable_renders_keep_placeholder_and_warn(self, tmp_path, monkeypatch):
        _patch_zotero(monkeypatch)

        def boom(*a, **k):
            raise RuntimeError("offline")
        monkeypatch.setattr(zotero, "formatted_citations", boom)
        src = tmp_path / "s.docx"
        new_doc(src, ["A claim citing two invented papers here."])
        with pytest.warns(UserWarning, match="pre-render skipped.*offline"):
            zoterofield.cite_into(src, "two invented papers", keys=["KSM9", "KSM0"],
                                  style="apa", out=tmp_path / "o.docx")
        assert _field_result(tmp_path / "o.docx") == "(citation)"

    def test_omitted_render_keeps_placeholder_and_warns(self, tmp_path, monkeypatch):
        _patch_zotero(monkeypatch)
        with pytest.warns(UserWarning, match="1 render"):
            got = zoterofield.grouped_citation_render(["KSM9", "NOPE"], style="apa")
        assert got == "(citation)"

    def test_numeric_style_keeps_placeholder_silently(self, monkeypatch, recwarn):
        monkeypatch.setattr(zotero, "formatted_citations",
                            lambda keys, **k: [f"({i + 1})" for i, _ in enumerate(keys)])
        assert zoterofield.grouped_citation_render(["A", "B"], style="vancouver") == "(citation)"
        assert not [w for w in recwarn if issubclass(w.category, UserWarning)]


# ===========================================================================
# #19 suppress-author
# ===========================================================================
class TestSuppressAuthor:
    @pytest.mark.parametrize("flag", ["suppress-author", "suppress_author"])
    def test_flag_in_csl_json(self, flag):
        xml = zoterofield._build_zotero_field_xml(
            ["s"], ["K1", "K2"], [{"id": "K1"}, {"id": "K2"}], ["u1", "u2"],
            extras=[{flag: True}, {}])
        data = json.loads(re.search(r"CSL_CITATION (\{.*\}) <", xml).group(1)
                          .replace("&amp;", "&"))
        assert data["citationItems"][0]["suppress-author"] is True
        assert "suppress-author" not in data["citationItems"][1]

    def test_year_only_render(self):
        assert zoterofield.group_citation_html(
            ["(Smith et al., 2019a)"], suppress_author=[True]) == "(2019a)"

    def test_cite_into_narrative(self, tmp_path, monkeypatch):
        _patch_zotero(monkeypatch, table={"KNAR": "(Smith et al., 2019)"})
        src = tmp_path / "s.docx"
        new_doc(src, ["As Smith et al. showed, the effect holds."])
        out = tmp_path / "o.docx"
        zoterofield.cite_into(src, "Smith et al.", keys=["KNAR"], style="apa", out=out,
                              suppress_author=True)
        assert _field_result(out) == "(2019)"
        assert _citation_json(out)["citationItems"][0]["suppress-author"] is True

    def test_narrative_marker_becomes_author_text_plus_field(self, tmp_path):
        # "Smith et al. (2019)": the author stays plain text, "(2019)" becomes a
        # suppress-author field whose result equals it -> an untracked swap.
        src = tmp_path / "s.docx"
        new_doc(src, ["As Smith et al. (2019) showed, the effect holds."])
        doc = Docx(src)
        zoterofield.replace_text_with_zotero_field(
            doc, "(2019)", ["KNAR"], itemdata=[{"id": "KNAR"}], uris=["u"],
            rendered="(2019)", extras=[{"suppress-author": True}], track=True, author=ME)
        out = tmp_path / "o.docx"
        doc.save(out)
        assert _citation_json(out)["citationItems"][0]["suppress-author"] is True
        v = read_views(out)
        assert v["accepted"] == v["rejected"] == "As Smith et al. (2019) showed, the effect holds."
        assert Docx(out).read_tree(DOCUMENT).find(".//" + qn("w:del")) is None

    def test_cli_flag(self, tmp_path, monkeypatch, capsys):
        _patch_zotero(monkeypatch, table={"KNAR": "(Smith et al., 2019)"})
        src = tmp_path / "s.docx"
        new_doc(src, ["As Smith et al. showed, the effect holds."])
        out = tmp_path / "o.docx"
        cli.main(["cite-into", str(src), "--anchor", "Smith et al.", "--key", "KNAR",
                  "--style", "apa", "--suppress-author", "-o", str(out)])
        assert "inserted live Zotero citation field" in capsys.readouterr().out
        assert _citation_json(out)["citationItems"][0]["suppress-author"] is True
        assert _field_result(out) == "(2019)"


# ===========================================================================
# #20 multi-paragraph bibliography under a heading
# ===========================================================================
def _doc_with_heading(path: Path, extra=None) -> Path:
    new_doc(path, ["Introduction text cites things.", "References", "Appendix A"]
            + (extra or []))
    return path


def _styles(path: Path):
    return Docx(path).read_tree("word/styles.xml")


def _bib_styles(path: Path):
    return [s for s in _styles(path).iter(qn("w:style"))
            if s.get(qn("w:styleId")) == "Bibliography"]


class TestBibliography:
    def test_clean_entry_html(self):
        assert zoterofield._clean_bib_entry_html(BIB["KZHA"]) == \
            "Zhang, D. (2017). Third invented title. <i>Made Up</i>, 9."
        numeric = ('<div class="csl-bib-body"><div class="csl-entry"><div class="csl-left-margin">'
                   '1.</div><div class="csl-right-inline">Doe J. Invented.</div></div></div>')
        assert zoterofield._clean_bib_entry_html(numeric) == "1. Doe J. Invented."

    def _write(self, tmp_path, **kw):
        src = _doc_with_heading(tmp_path / "s.docx")
        doc = Docx(src)
        entries = [zoterofield._clean_bib_entry_html(BIB[k]) for k in ("KOLI", "KOSC", "KZHA")]
        ok = zoterofield.append_bibliography_field(
            doc, style="apa", entries_html=entries, under_heading="References",
            author=ME, date=OP, **kw)
        out = tmp_path / "o.docx"
        doc.save(out)
        return ok, out

    def test_placed_under_heading_begin_first_end_last(self, tmp_path):
        ok, out = self._write(tmp_path, track=True)
        assert ok
        paras = iter_paragraphs(Docx(out).read_tree(DOCUMENT))
        texts = [paragraph_text(p) for p in paras]
        assert texts[1] == "References" and texts[-1] == "Appendix A"
        entries = paras[2:5]
        assert texts[2].startswith("Oliveira") and texts[4].startswith("Zhang")

        def kinds(p):
            return [f.get(qn("w:fldCharType")) for f in p.iter(qn("w:fldChar"))]
        assert kinds(entries[0]) == ["begin", "separate"]
        assert kinds(entries[1]) == []
        assert kinds(entries[2]) == ["end"]
        assert "ZOTERO_BIBL" in "".join(entries[0].itertext())
        for p in entries:
            assert p.find(f"{qn('w:pPr')}/{qn('w:pStyle')}").get(qn("w:val")) == "Bibliography"
            assert p.find(f"{qn('w:pPr')}/{qn('w:rPr')}/{qn('w:ins')}") is not None
            assert [c.tag for c in p if c.tag != qn("w:pPr")] == [qn("w:ins")]
        # italics from the bib HTML survive as run formatting
        assert any(r.find(f"{qn('w:rPr')}/{qn('w:i')}") is not None
                   for r in entries[0].iter(qn("w:r")))
        v = read_views(out)
        assert "Oliveira, C., et al. (2018)" in v["accepted"]
        assert "Oliveira" not in v["rejected"]
        assert validate(out).ok

    def test_style_added_once_with_hanging_indent(self, tmp_path):
        ok, out = self._write(tmp_path, track=False)
        st = _bib_styles(out)
        assert len(st) == 1
        ind = st[0].find(f"{qn('w:pPr')}/{qn('w:ind')}")
        assert ind.get(qn("w:hanging")) == "720"
        assert st[0].find(qn("w:basedOn")).get(qn("w:val")) == "Normal"
        doc = Docx(out)
        assert zoterofield.ensure_bibliography_style(doc) == "Bibliography"
        assert zoterofield.append_bibliography_field(
            doc, entries_html=["x"], under_heading="References") is False   # idempotent
        doc.save(tmp_path / "o2.docx")
        assert len(_bib_styles(tmp_path / "o2.docx")) == 1

    def test_existing_style_by_name_is_reused(self, tmp_path):
        src = _doc_with_heading(tmp_path / "s.docx")
        doc = Docx(src)
        doc.tree("word/styles.xml").append(etree.fromstring(
            f'<w:style xmlns:w="{zoterofield.W}" w:type="paragraph" w:styleId="Bibliografia">'
            '<w:name w:val="Bibliography"/></w:style>'))
        assert zoterofield.ensure_bibliography_style(doc) == "Bibliografia"
        assert not [s for s in doc.tree("word/styles.xml").iter(qn("w:style"))
                    if s.get(qn("w:styleId")) == "Bibliography"]

    def test_missing_or_ambiguous_heading_refused_untouched(self, tmp_path):
        src = _doc_with_heading(tmp_path / "s.docx", extra=["References"])
        doc = Docx(src)
        before = etree.tostring(doc.tree(DOCUMENT))
        with pytest.raises(LookupError, match="matched 2"):
            zoterofield.append_bibliography_field(doc, entries_html=["x"],
                                                  under_heading="References")
        with pytest.raises(LookupError, match="matched 0"):
            zoterofield.append_bibliography_field(doc, entries_html=["x"],
                                                  under_heading="Works Cited")
        with pytest.raises(ValueError):
            zoterofield.append_bibliography_field(doc, heading="R", under_heading="References")
        assert etree.tostring(doc.tree(DOCUMENT)) == before

    def test_timestamps_fallback_old_marks_kept(self, tmp_path):
        # zotero-word-cite's revisions has no snapshot_dated_marks/spread_own_marks (no
        # zotero-word-cite caller writes a TRACKED bibliography), so the spread is skipped:
        # the new paragraphs carry the op date, the prior mark keeps its own.
        src = tmp_path / "s.docx"
        new_doc(src, ["Body.", "References"])
        doc = Docx(src)
        p0 = iter_paragraphs(doc.tree(DOCUMENT))[0]
        prior = "2026-01-05T09:00:00Z"
        old = etree.fromstring(
            f'<w:ins xmlns:w="{zoterofield.W}" w:id="77" w:author="{ME}" w:date="{prior}">'
            '<w:r><w:t>prior</w:t></w:r></w:ins>')
        p0.append(old)
        zoterofield.append_bibliography_field(
            doc, entries_html=["A one.", "B two.", "C three."], under_heading="References",
            track=True, author=ME, date=OP)
        assert old.get(qn("w:date")) == prior                    # never re-stamped
        paras = iter_paragraphs(doc.tree(DOCUMENT))[2:5]
        for p in paras:
            assert {el.get(qn("w:date")) for el in p.iter()
                    if el.get(qn("w:author")) == ME} == {OP}

    def test_cite_into_bib_under_missing_heading_cli_clean_exit(self, tmp_path, monkeypatch):
        # zotero-word-cite CLI: a missing --bib-under heading is a one-line cite-into error
        # (LookupError caught), never a traceback, and no output is written.
        _patch_zotero(monkeypatch)
        src = _doc_with_heading(tmp_path / "s.docx")
        out = tmp_path / "o.docx"
        with pytest.raises(SystemExit) as ei:
            cli.main(["cite-into", str(src), "--anchor", "cites things", "--keys", "KZHA",
                      "--style", "apa", "--bib-under", "Works Cited", "-o", str(out)])
        assert str(ei.value.code).startswith("cite-into: ") and "matched 0" in str(ei.value.code)
        assert not out.exists()

    def test_cite_into_bib_under_cli(self, tmp_path, monkeypatch, capsys):
        _patch_zotero(monkeypatch)
        src = _doc_with_heading(tmp_path / "s.docx")
        out = tmp_path / "o.docx"
        cli.main(["cite-into", str(src), "--anchor", "cites things", "--keys", "KZHA,KOLI",
                  "--style", "apa", "--bib-under", "References", "-o", str(out)])
        texts = [paragraph_text(p) for p in iter_paragraphs(Docx(out).read_tree(DOCUMENT))]
        assert texts[1] == "References"
        assert texts[2].startswith("Oliveira") and texts[3].startswith("Zhang")
        assert texts[4] == "Appendix A"
        assert validate(out).ok
