"""Tests for zoterocite.citeconvert — foreign-manager detection + conversion.

No network: the Zotero client is monkeypatched. Fixtures inject raw OOXML field
codes for each manager (EndNote ADDIN EN.CITE, bare Mendeley ADDIN CSL_CITATION,
native Word CITATION + b:Sources, a Zotero field, and a manual References list).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from lxml import etree

from zoterocite import Docx, new_doc, validate, scan_citations
from zoterocite.docxio import DOCUMENT
from zoterocite.ooxml import NS, qn
from zoterocite.citeconvert import (
    classify_citation_sources,
    classification_findings,
    convert_to_zotero,
)
from zoterocite import citeconvert as cc

W = NS["w"]
B_NS = "http://schemas.openxmlformats.org/officeDocument/2006/bibliography"
GROUP = "http://zotero.org/groups/2504198/items/"


# ---------------------------------------------------------------------------
# Field-code fixtures (verbatim-shaped, trimmed)
# ---------------------------------------------------------------------------

ENDNOTE_INSTR = (
    " ADDIN EN.CITE "
    "<EndNote><Cite><Author>Bronk Ramsey</Author><Year>2009</Year>"
    "<RecNum>19</RecNum>"
    "<DisplayText><style face=\"superscript\">[1]</style></DisplayText>"
    "<record><rec-number>19</rec-number>"
    "<ref-type name=\"Journal Article\">17</ref-type>"
    "<contributors><authors><author>Bronk Ramsey, Christopher</author></authors></contributors>"
    "<titles><title>Bayesian Analysis of Radiocarbon Dates</title></titles>"
    "<dates><year>2009</year></dates>"
    "<electronic-resource-num>10.1017/S0033822200033865</electronic-resource-num>"
    "</record></Cite></EndNote> "
)

MENDELEY_INSTR = " ADDIN CSL_CITATION " + json.dumps({
    "citationItems": [{
        "id": "ITEM-1",
        "itemData": {
            "author": [{"family": "Smith", "given": "John", "parse-names": False}],
            "id": "ITEM-1",
            "issued": {"date-parts": [["2001"]]},
            "title": "A study of widgets",
            "DOI": "10.1000/widgets.2001",
            "type": "article-journal",
        },
        "uris": ["http://www.mendeley.com/documents/?uuid=55ff8735"],
    }],
    "mendeley": {"formattedCitation": "(Smith, 2001)",
                 "plainTextFormattedCitation": "(Smith, 2001)"},
    "properties": {"noteIndex": 0},
    "schema": "https://github.com/citation-style-language/schema/raw/master/csl-citation.json",
}) + " "

# Second Mendeley citation to the SAME DOI as the first (dedup target).
MENDELEY_INSTR_DUP = " ADDIN CSL_CITATION " + json.dumps({
    "citationItems": [{
        "id": "ITEM-9",
        "itemData": {
            "author": [{"family": "Smith", "given": "John"}],
            "id": "ITEM-9",
            "issued": {"date-parts": [["2001"]]},
            "title": "A study of widgets",
            "DOI": "10.1000/widgets.2001",
            "type": "article-journal",
        },
        "uris": ["http://www.mendeley.com/documents/?uuid=abcdef00"],
    }],
    "mendeley": {"plainTextFormattedCitation": "(Smith, 2001)"},
    "properties": {"noteIndex": 0},
    "schema": "https://github.com/citation-style-language/schema/raw/master/csl-citation.json",
}) + " "

ZOTERO_INSTR = "ADDIN ZOTERO_ITEM CSL_CITATION " + json.dumps({
    "citationID": "ABC123",
    "properties": {"formattedCitation": "(1)", "plainCitation": "(1)", "noteIndex": 0},
    "citationItems": [{
        "id": "2504198/ZKEY",
        "uris": [GROUP + "ZKEY"],
        "itemData": {"id": "2504198/ZKEY", "type": "article-journal",
                     "title": "Already in Zotero", "DOI": "10.5555/already"},
    }],
    "schema": "https://github.com/citation-style-language/schema/raw/master/csl-citation.json",
})

WORD_INSTR = " CITATION Smith2020 \\l 1033 "


def _append_complex_field(p, instr: str):
    """Append a complex field (begin/instr/separate/result/end) to paragraph p."""
    def run():
        return etree.SubElement(p, qn("w:r"))
    r1 = run(); etree.SubElement(r1, qn("w:fldChar")).set(qn("w:fldCharType"), "begin")
    r2 = run(); it = etree.SubElement(r2, qn("w:instrText")); it.set(qn("xml:space"), "preserve"); it.text = instr
    r3 = run(); etree.SubElement(r3, qn("w:fldChar")).set(qn("w:fldCharType"), "separate")
    r4 = run(); t = etree.SubElement(r4, qn("w:t")); t.text = "(cite)"
    r5 = run(); etree.SubElement(r5, qn("w:fldChar")).set(qn("w:fldCharType"), "end")


def _append_word_sdt(p, instr: str):
    """Append a Word built-in citation: <w:sdt><w:sdtPr><w:citation/>...complex field."""
    sdt = etree.SubElement(p, qn("w:sdt"))
    sdtpr = etree.SubElement(sdt, qn("w:sdtPr"))
    etree.SubElement(sdtpr, qn("w:id")).set(qn("w:val"), "836273690")
    etree.SubElement(sdtpr, qn("w:citation"))
    etree.SubElement(sdt, qn("w:sdtEndPr"))
    content = etree.SubElement(sdt, qn("w:sdtContent"))
    def run():
        return etree.SubElement(content, qn("w:r"))
    r1 = run(); etree.SubElement(r1, qn("w:fldChar")).set(qn("w:fldCharType"), "begin")
    r2 = run(); it = etree.SubElement(r2, qn("w:instrText")); it.set(qn("xml:space"), "preserve"); it.text = instr
    r3 = run(); etree.SubElement(r3, qn("w:fldChar")).set(qn("w:fldCharType"), "separate")
    r4 = run(); t = etree.SubElement(r4, qn("w:t")); t.text = "(Smith, 2020)"
    r5 = run(); etree.SubElement(r5, qn("w:fldChar")).set(qn("w:fldCharType"), "end")


def _find_para(root, anchor):
    from zoterocite.paras import find_paragraph
    return find_paragraph(root, anchor)


def _add_word_sources(doc: Docx):
    """Add a b:Sources customXml part with one DOI'd JournalArticle (Smith2020)."""
    xml = (
        f'<b:Sources xmlns:b="{B_NS}" SelectedStyle="" StyleName="APA">'
        '<b:Source><b:Tag>Smith2020</b:Tag><b:SourceType>JournalArticle</b:SourceType>'
        '<b:Author><b:Author><b:NameList>'
        '<b:Person><b:Last>Smith</b:Last><b:First>Jane</b:First></b:Person>'
        '</b:NameList></b:Author></b:Author>'
        '<b:Title>A study of things</b:Title><b:JournalName>Journal of Examples</b:JournalName>'
        '<b:Year>2020</b:Year><b:DOI>10.1000/example.2020.001</b:DOI>'
        '</b:Source></b:Sources>'
    )
    doc.add_part("customXml/item1.xml", xml.encode("utf-8"))


def build_mixed_doc(tmp_path: Path, *, with_dup=False, with_zotero=True,
                    with_manual=True, with_word=True) -> Path:
    """Create a .docx mixing EndNote, Mendeley, Word, Zotero, and manual refs."""
    paras = [
        "EndNote sentence alpha goes here.",
        "Mendeley sentence beta goes here.",
    ]
    if with_dup:
        paras.append("Repeat sentence gamma goes here.")
    if with_word:
        paras.append("WordBuiltin sentence delta goes here.")
    if with_zotero:
        paras.append("ZoteroManaged sentence epsilon goes here.")
    if with_manual:
        paras += [
            "References",
            "1. Doe J, Roe R. A manual unmanaged reference about cortex. Journal of Things. 2018;5(2):10-20. doi:10.9999/manual.2018",
        ]
    src = tmp_path / "src.docx"
    new_doc(src, paras)
    doc = Docx(src)
    root = doc.tree(DOCUMENT)

    _append_complex_field(_find_para(root, "sentence alpha"), ENDNOTE_INSTR)
    _append_complex_field(_find_para(root, "sentence beta"), MENDELEY_INSTR)
    if with_dup:
        _append_complex_field(_find_para(root, "sentence gamma"), MENDELEY_INSTR_DUP)
    if with_word:
        _append_word_sdt(_find_para(root, "sentence delta"), WORD_INSTR)
        _add_word_sources(doc)
    if with_zotero:
        _append_complex_field(_find_para(root, "sentence epsilon"), ZOTERO_INSTR)

    out = tmp_path / "mixed.docx"
    doc.save(out)
    return out


# ---------------------------------------------------------------------------
# Fake Zotero library
# ---------------------------------------------------------------------------

# Library keyed by normalized DOI and normalized title.
FAKE_LIB = {
    "doi:10.1017/s0033822200033865": "ENKEY1",      # EndNote -> match
    "doi:10.1000/widgets.2001": "MENKEY1",           # Mendeley -> match
    "doi:10.1000/example.2020.001": "WORDKEY1",      # Word -> match
    "doi:10.5555/already": "ZKEY",                   # already-zotero
    "title:a manual unmanaged reference about cortex": "MANKEY1",
}


@pytest.fixture()
def fake_zotero(monkeypatch):
    from zoterocite import zotero

    def get_item_by_doi(doi):
        norm = "doi:" + doi.strip().lower()
        key = FAKE_LIB.get(norm)
        return {"key": key, "data": {"key": key}} if key else None

    def search_items(query, qmode=None):
        # title search: return an item if the normalized title is in FAKE_LIB
        ntitle = "title:" + " ".join(c for c in query.lower() if True)
        # crude: build normalized title key like the module does
        import re as _re
        norm = "title:" + _re.sub(r"[^a-z0-9]+", " ", query.lower()).strip()
        key = FAKE_LIB.get(norm)
        if not key:
            return []
        return [{"key": key, "data": {"key": key, "title": query}}]

    def csljson(keys):
        return [{"id": k, "type": "article-journal", "title": f"Title for {k}"} for k in keys]

    def item_uri(key):
        return GROUP + key

    def formatted_citations(keys, style="vancouver", kind="bib", strip=True):
        return [f"({k})" for k in keys]

    monkeypatch.setattr(zotero, "get_item_by_doi", get_item_by_doi)
    monkeypatch.setattr(zotero, "search_items", search_items)
    monkeypatch.setattr(zotero, "csljson", csljson)
    monkeypatch.setattr(zotero, "item_uri", item_uri)
    monkeypatch.setattr(zotero, "formatted_citations", formatted_citations)
    return zotero


# ===========================================================================
# DOI normalisation (regression E9)
# ===========================================================================

class TestNormalizeDoi:
    def test_interior_doi_substring_not_stripped(self):
        # The old unanchored doi.replace("doi:","") removed EVERY "doi:" run,
        # corrupting a DOI body that legitimately contains the substring
        # "doi:". The extract-then-normalise path (textpatterns.extract_dois +
        # normalize_doi) strips only the leading prefix.
        assert cc._extract_doi("doi:10.1000/abdoi:cd") == "10.1000/abdoi:cd"

    def test_leading_prefix_and_case_and_bracket(self):
        assert cc._extract_doi("https://doi.org/10.1038/S41586-020-2649-2]") == \
            "10.1038/s41586-020-2649-2"

    def test_uppercase_label_and_trailing_paren(self):
        assert cc._extract_doi("DOI: 10.1000/abc)") == "10.1000/abc"

    def test_no_doi_returns_none(self):
        assert cc._extract_doi("not a doi at all") is None

    def test_normalize_doi_is_canonical_owner(self):
        # The module's normalize_doi must be the citecheck owner, not a re-impl.
        from zoterocite.citecheck import _normalise_doi
        assert cc.normalize_doi is _normalise_doi


# ===========================================================================
# PART 1 — classification
# ===========================================================================

class TestClassify:
    def test_counts_per_manager(self, tmp_path):
        path = build_mixed_doc(tmp_path, with_dup=True)
        res = classify_citation_sources(path)
        c = res["counts"]
        assert c["endnote"] == 1
        assert c["mendeley"] == 2  # original + dup
        assert c["word"] == 1
        assert c["zotero"] == 1
        assert c["manual"] >= 1

    def test_extracted_doi_and_title(self, tmp_path):
        path = build_mixed_doc(tmp_path)
        res = classify_citation_sources(path)
        by_mgr = {}
        for it in res["items"]:
            by_mgr.setdefault(it["manager"], []).append(it)

        en = by_mgr["endnote"][0]["extracted"]
        # Extraction now routes through citeconvert.normalize_doi (the canonical
        # owner), so the DOI is returned in canonical lowercased form — matching
        # the lowercase library lookup key without relying on a second
        # normalisation pass downstream.
        assert en["doi"] == "10.1017/s0033822200033865"
        assert en["title"] == "Bayesian Analysis of Radiocarbon Dates"
        assert en["year"] == "2009"
        assert any("Bronk Ramsey" in a for a in en["authors"])

        men = by_mgr["mendeley"][0]["extracted"]
        assert men["doi"] == "10.1000/widgets.2001"
        assert men["title"] == "A study of widgets"
        assert men["year"] == "2001"

        word = by_mgr["word"][0]["extracted"]
        assert word["doi"] == "10.1000/example.2020.001"
        assert word["title"] == "A study of things"
        assert word["tag"] == "Smith2020"

    def test_word_tag_switch_order(self):
        from zoterocite.citeconvert import _word_tag_from_instr
        assert _word_tag_from_instr(" CITATION Smith2020 \\l 1033 ") == "Smith2020"
        # boolean switch (\n, no argument) before the tag must not swallow it
        assert _word_tag_from_instr(" CITATION \\n Smith2020 \\l 1033 ") == "Smith2020"
        # value switch with its argument before the tag
        assert _word_tag_from_instr(" CITATION \\l 1033 Smith2020 ") == "Smith2020"

    def test_manual_low_confidence_and_doi(self, tmp_path):
        path = build_mixed_doc(tmp_path)
        res = classify_citation_sources(path)
        manual = [it for it in res["items"] if it["manager"] == "manual"]
        assert manual
        m = manual[0]
        assert m.get("low_confidence") is True
        assert m["extracted"].get("doi") == "10.9999/manual.2018"

    def test_findings_mix_and_warnings(self, tmp_path):
        path = build_mixed_doc(tmp_path)
        res = classify_citation_sources(path)
        findings = classification_findings(res)
        checks = [f.check for f in findings]
        assert "CITE-SOURCES" in checks            # INFO summary
        assert "CITE-FOREIGN" in checks            # WARN for endnote/mendeley/word
        assert "CITE-MANUAL" in checks             # WARN for manual ref
        # no foreign WARN should target the zotero field
        foreign = [f for f in findings if f.check == "CITE-FOREIGN"]
        assert all("Zotero" not in f.message.split("managed by")[1].split("—")[0]
                   for f in foreign)


# ===========================================================================
# E8 — a malformed b:Sources customXml part must surface a diagnostic Finding,
# not silently strip metadata from the Word citations it backed.
# ===========================================================================

def _add_malformed_word_sources(doc: Docx, part: str = "customXml/item1.xml"):
    """Inject a b:Sources store that is unparseable (unclosed tags) but still
    selected by _word_source_index (contains 'Sources' and the b: namespace)."""
    bad = (
        f'<b:Sources xmlns:b="{B_NS}" SelectedStyle="" StyleName="APA">'
        '<b:Source><b:Tag>Smith2020</b:Tag><b:Title>A study of <unclosed things'
    )  # deliberately truncated / unbalanced -> XMLSyntaxError
    doc.add_part(part, bad.encode("utf-8"))


class TestMalformedSourceStore:
    def _doc_with_bad_store(self, tmp_path: Path) -> Path:
        src = tmp_path / "bad_src.docx"
        new_doc(src, ["A Word-cited sentence here.", "Another sentence."])
        doc = Docx(src)
        _add_malformed_word_sources(doc)
        out = tmp_path / "bad.docx"
        doc.save(out)
        return out

    def test_source_errors_recorded_in_result(self, tmp_path):
        path = self._doc_with_bad_store(tmp_path)
        res = classify_citation_sources(path)
        assert res.get("source_errors") == ["customXml/item1.xml"]

    def test_finding_surfaced_not_silent(self, tmp_path):
        path = self._doc_with_bad_store(tmp_path)
        res = classify_citation_sources(path)
        findings = classification_findings(res)
        parse = [f for f in findings if f.check == "CITE-SOURCE-PARSE"]
        assert len(parse) == 1, f"Expected one parse Finding, got {findings}"
        f = parse[0]
        assert f.severity == "WARN"
        assert "unparseable" in f.message.lower()
        assert "customXml/item1.xml" in f.message
        assert f.location == "customXml/item1.xml"

    def test_valid_store_yields_no_parse_finding(self, tmp_path):
        # A well-formed mixed doc (built with _add_word_sources) has no errors.
        path = build_mixed_doc(tmp_path)
        res = classify_citation_sources(path)
        assert res.get("source_errors") == []
        findings = classification_findings(res)
        assert not any(f.check == "CITE-SOURCE-PARSE" for f in findings)

    def test_cite_check_surfaces_parse_finding(self, tmp_path):
        """Even with no foreign-manager counts, cite_check surfaces the parse
        failure (it would otherwise be silent — the affected cites drop to
        word=0)."""
        from zoterocite.citecheck import cite_check
        path = self._doc_with_bad_store(tmp_path)
        findings = cite_check(path, auto_refresh=False, check_existence=False)
        assert any(f.check == "CITE-SOURCE-PARSE" for f in findings), (
            f"cite_check did not surface the parse failure: "
            f"{[f.check for f in findings]}"
        )


# ===========================================================================
# PART 2 — conversion
# ===========================================================================

class TestConvert:
    def test_converts_endnote_and_mendeley(self, tmp_path, fake_zotero):
        path = build_mixed_doc(tmp_path, with_word=False, with_manual=True)
        out = tmp_path / "converted.docx"
        res = convert_to_zotero(path, out=out, managers=("endnote", "mendeley"))

        managers_converted = {c["manager"] for c in res["converted"]}
        assert "endnote" in managers_converted
        assert "mendeley" in managers_converted

        # output doc validates (no Word repair) ...
        assert validate(res["out"]).ok
        # ... and now carries live Zotero fields where the foreign ones were.
        cits = scan_citations(res["out"])
        keys = {it["key"] for cit in cits for it in cit["items"]}
        assert "ENKEY1" in keys
        assert "MENKEY1" in keys
        # original EndNote / bare Mendeley markers are gone from those fields
        body = Docx(res["out"]).raw(DOCUMENT).decode()
        assert "EN.CITE" not in body
        assert body.count("ADDIN ZOTERO_ITEM CSL_CITATION") >= 3  # 2 converted + 1 pre-existing

    def test_converts_word_builtin(self, tmp_path, fake_zotero):
        path = build_mixed_doc(tmp_path, with_manual=False)
        out = tmp_path / "c.docx"
        res = convert_to_zotero(path, out=out, managers=("word",))
        assert any(c["manager"] == "word" for c in res["converted"])
        body = Docx(res["out"]).raw(DOCUMENT).decode()
        assert "<w:citation/>" not in body  # the Word content control is replaced
        keys = {it["key"] for cit in scan_citations(res["out"]) for it in cit["items"]}
        assert "WORDKEY1" in keys
        assert validate(res["out"]).ok

    def test_dedup_same_doi_to_one_item(self, tmp_path, fake_zotero, monkeypatch):
        # Two Mendeley citations to the SAME DOI -> one library lookup, dedup>=1.
        path = build_mixed_doc(tmp_path, with_dup=True, with_word=False,
                               with_zotero=False, with_manual=False)
        out = tmp_path / "d.docx"
        calls = []
        orig = fake_zotero.get_item_by_doi
        monkeypatch.setattr(fake_zotero, "get_item_by_doi",
                            lambda doi: (calls.append(doi) or orig(doi)))

        res = convert_to_zotero(path, out=out, managers=("mendeley",))
        assert res["deduped"] >= 1
        # the widgets DOI was looked up exactly once despite two citations
        assert calls.count("10.1000/widgets.2001") == 1
        # both Mendeley cites now reference the single MENKEY1 item
        keys = [it["key"] for cit in scan_citations(res["out"]) for it in cit["items"]]
        assert keys.count("MENKEY1") == 2

    def test_dedup_against_existing_zotero(self, tmp_path, fake_zotero):
        # A Mendeley cite whose DOI is already Zotero-managed must NOT be converted.
        src = tmp_path / "s.docx"
        new_doc(src, ["Foreign dup of zotero item here.", "Existing zotero here."])
        doc = Docx(src)
        root = doc.tree(DOCUMENT)
        men = " ADDIN CSL_CITATION " + json.dumps({
            "citationItems": [{"id": "X", "itemData": {
                "title": "Already in Zotero", "DOI": "10.5555/already",
                "issued": {"date-parts": [["2020"]]}}, "uris": ["http://mendeley.com/x"]}],
            "mendeley": {"plainTextFormattedCitation": "(x)"},
            "schema": "https://github.com/citation-style-language/schema/raw/master/csl-citation.json",
        }) + " "
        _append_complex_field(_find_para(root, "Foreign dup of zotero item"), men)
        _append_complex_field(_find_para(root, "Existing zotero here"), ZOTERO_INSTR)
        p = tmp_path / "p.docx"; doc.save(p)

        out = tmp_path / "o.docx"
        res = convert_to_zotero(p, out=out, managers=("mendeley",))
        assert res["deduped"] >= 1
        # nothing converted (the only foreign cite was a dup of an existing item)
        assert res["converted"] == []

    def test_unmatched_recorded_not_fabricated(self, tmp_path, fake_zotero):
        # Mendeley cite to a DOI NOT in the fake library -> unmatched, not converted.
        src = tmp_path / "s.docx"
        new_doc(src, ["Missing claim here."])
        doc = Docx(src)
        root = doc.tree(DOCUMENT)
        men = " ADDIN CSL_CITATION " + json.dumps({
            "citationItems": [{"id": "X", "itemData": {
                "title": "Nowhere to be found", "DOI": "10.0000/missing"}, "uris": ["u"]}],
            "mendeley": {"plainTextFormattedCitation": "(x)"},
            "schema": "https://github.com/citation-style-language/schema/raw/master/csl-citation.json",
        }) + " "
        _append_complex_field(_find_para(root, "Missing claim"), men)
        p = tmp_path / "p.docx"; doc.save(p)

        out = tmp_path / "o.docx"
        res = convert_to_zotero(p, out=out, managers=("mendeley",))
        assert res["converted"] == []
        assert len(res["unmatched"]) == 1
        assert res["unmatched"][0]["extracted"]["doi"] == "10.0000/missing"
        # the foreign field is left intact (not fabricated into a Zotero field)
        body = Docx(res["out"]).raw(DOCUMENT).decode()
        assert "ADDIN CSL_CITATION" in body and "ZOTERO_ITEM" not in body

    def test_manual_refs_in_manual_skipped(self, tmp_path, fake_zotero):
        path = build_mixed_doc(tmp_path, with_word=False)
        out = tmp_path / "o.docx"
        res = convert_to_zotero(path, out=out, managers=("endnote", "mendeley"))
        assert res["manual_skipped"]
        assert any(m["extracted"].get("doi") == "10.9999/manual.2018"
                   for m in res["manual_skipped"])

    def test_dry_run_writes_nothing(self, tmp_path, fake_zotero):
        path = build_mixed_doc(tmp_path, with_word=False, with_manual=False)
        res = convert_to_zotero(path, out=None, managers=("endnote", "mendeley"))
        assert res["out"] is None
        assert res["converted"]  # still reports what it would convert

    def test_tracked_conversion(self, tmp_path, fake_zotero):
        path = build_mixed_doc(tmp_path, with_word=False, with_manual=False,
                               with_zotero=False)
        out = tmp_path / "t.docx"
        res = convert_to_zotero(path, out=out, managers=("endnote", "mendeley"),
                                track=True)
        body = Docx(res["out"]).raw(DOCUMENT).decode()
        assert "<w:ins " in body and "<w:del " in body
        assert validate(res["out"]).ok

    def test_classification_in_result(self, tmp_path, fake_zotero):
        path = build_mixed_doc(tmp_path)
        out = tmp_path / "o.docx"
        res = convert_to_zotero(path, out=out)
        assert "classification" in res
        assert res["classification"]["counts"]["endnote"] == 1


# ===========================================================================
# cite_check surfaces the foreign mix
# ===========================================================================

def test_cite_check_surfaces_foreign(tmp_path):
    from zoterocite.citecheck import cite_check
    path = build_mixed_doc(tmp_path)
    findings = cite_check(path, rw_csv=Path("/nonexistent/rw.csv"))
    checks = [f.check for f in findings]
    assert "CITE-SOURCES" in checks
    assert "CITE-FOREIGN" in checks


# ===========================================================================
# FIX 4 — Mendeley/Zotero misclassification via JSON payload substring
# ===========================================================================

class TestZoteroClassificationByPrefix:
    """A bare Mendeley ADDIN CSL_CITATION whose JSON payload happens to contain
    the string 'zotero_item' (e.g. in a title, note, or URI) must NOT be
    misclassified as Zotero. Only the FIELD PREFIX 'ADDIN ZOTERO_ITEM' /
    'ADDIN ZOTERO_BIBL' should trigger Zotero classification."""

    def test_mendeley_with_zotero_item_in_payload_classifies_as_mendeley(self, tmp_path, fake_zotero):
        """ADDIN CSL_CITATION whose JSON contains 'zotero_item' in the title
        must classify as mendeley (and be convertible), not zotero."""
        # Build a Mendeley instruction whose title contains "zotero_item"
        mendeley_with_poisoned_title = " ADDIN CSL_CITATION " + json.dumps({
            "citationItems": [{
                "id": "ITEM-POISON",
                "itemData": {
                    "author": [{"family": "Jones", "given": "T"}],
                    "id": "ITEM-POISON",
                    "issued": {"date-parts": [["2020"]]},
                    # title intentionally contains the substring 'zotero_item'
                    "title": "A paper about zotero_item metadata schemas",
                    "DOI": "10.1000/widgets.2001",  # matches FAKE_LIB for conversion
                    "type": "article-journal",
                },
                "uris": ["http://www.mendeley.com/documents/?uuid=poisoned"],
            }],
            "mendeley": {"formattedCitation": "(Jones, 2020)"},
            "properties": {"noteIndex": 0},
            "schema": "https://github.com/citation-style-language/schema/raw/master/csl-citation.json",
        }) + " "

        src = tmp_path / "src.docx"
        new_doc(src, ["Poisoned sentence here."])
        doc = Docx(src)
        root = doc.tree(DOCUMENT)
        _append_complex_field(_find_para(root, "Poisoned sentence"), mendeley_with_poisoned_title)
        p = tmp_path / "p.docx"
        doc.save(p)

        res = classify_citation_sources(p)
        # Must be classified as mendeley, not zotero
        assert res["counts"]["mendeley"] == 1
        assert res["counts"]["zotero"] == 0

        # And must be convertible (not silently skipped as Zotero)
        out = tmp_path / "out.docx"
        conv = convert_to_zotero(p, out=out, managers=("mendeley",))
        assert len(conv["converted"]) == 1
        assert conv["converted"][0]["manager"] == "mendeley"

    def test_actual_zotero_prefix_still_classifies_as_zotero(self):
        """Sanity: ADDIN ZOTERO_ITEM ... is still Zotero."""
        from zoterocite.citeconvert import _classify
        assert _classify("ADDIN ZOTERO_ITEM CSL_CITATION {...}") == "zotero"
        assert _classify("ADDIN ZOTERO_BIBL {...} CSL_BIBLIOGRAPHY") == "zotero"

    def test_mendeley_without_zotero_substring_still_mendeley(self):
        """Sanity: normal Mendeley CSL_CITATION still classified as mendeley."""
        from zoterocite.citeconvert import _classify
        instr = " ADDIN CSL_CITATION " + json.dumps({
            "citationItems": [{"id": "X", "itemData": {"title": "Normal paper"}}],
            "schema": "...",
        })
        assert _classify(instr) == "mendeley"
