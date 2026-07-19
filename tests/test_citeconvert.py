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
            # A hand-typed inline citation in the body (no citation field) — this
            # is the real detection target: an author-year marker the user forgot
            # to manage via Zotero.  Placed BEFORE the reference heading so it
            # remains in the body after the Problem B split.
            "Prior work (Doe et al., 2018) demonstrated this finding. doi:10.9999/manual.2018",
            "References",
            # The rendered bibliography — should NOT be flagged after the GF-5 fix.
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

    # Production builds a real offline library_index (citeconvert line ~950) and
    # matches against it FIRST; the live get_item_by_doi scan is only a no-index
    # fallback. Model that: derive the fake's index from the SAME FAKE_LIB so the
    # matched SET is identical, just resolved via the offline index (step 1) rather
    # than the per-ref live scan (which production skips once the index answered).
    def library_index(strict=False, **_k):
        return {
            "doi": {kk[4:]: v for kk, v in FAKE_LIB.items() if kk.startswith("doi:")},
            "pmid": {},
            "title": {kk[6:]: v for kk, v in FAKE_LIB.items() if kk.startswith("title:")},
        }

    monkeypatch.setattr(zotero, "library_index", library_index)
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
        # Two Mendeley citations to the SAME DOI -> resolved by ONE library lookup
        # (the match_cache prevents a second), dedup>=1, both cite the one item.
        # Matching now resolves via the offline index (lookup_index_key), so the
        # "resolved exactly once" guard counts THAT — not the live get_item_by_doi.
        path = build_mixed_doc(tmp_path, with_dup=True, with_word=False,
                               with_zotero=False, with_manual=False)
        out = tmp_path / "d.docx"
        calls = []
        orig = fake_zotero.lookup_index_key

        def _counting(idx, *, doi=None, pmid=None, title=None):
            if doi and "widgets.2001" in doi.lower():
                calls.append(doi)
            return orig(idx, doi=doi, pmid=pmid, title=title)
        monkeypatch.setattr(fake_zotero, "lookup_index_key", _counting)

        res = convert_to_zotero(path, out=out, managers=("mendeley",))
        assert res["deduped"] >= 1
        # the widgets DOI was resolved exactly once despite two citations (match_cache)
        assert len(calls) == 1
        # both Mendeley cites now reference the single MENKEY1 item
        keys = [it["key"] for cit in scan_citations(res["out"]) for it in cit["items"]]
        assert keys.count("MENKEY1") == 2

    def test_same_item_cited_twice_gets_distinct_citation_ids(self, tmp_path, fake_zotero):
        # Regression: two foreign fields citing the SAME item must convert to two
        # live Zotero fields with DISTINCT citationIDs. Zotero requires every
        # field's citationID to be unique; deriving it from keys alone collides,
        # and Zotero then merges/renumbers the two citations on refresh.
        path = build_mixed_doc(tmp_path, with_dup=True, with_word=False,
                               with_zotero=False, with_manual=False)
        out = tmp_path / "ids.docx"
        res = convert_to_zotero(path, out=out, managers=("mendeley",))
        cits = scan_citations(res["out"])
        # both converted fields reference the same single item ...
        keys = [it["key"] for cit in cits for it in cit["items"]]
        assert keys.count("MENKEY1") == 2
        # ... but each field carries a distinct citationID (no collision).
        cids = [c["citationID"] for c in cits]
        assert len(cids) == len(set(cids)), f"duplicate citationID across fields: {cids}"

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

    def test_grouped_cite_keeps_item_already_zotero_elsewhere(self, tmp_path, fake_zotero):
        # A grouped foreign cite [A, B] where B is already cited via Zotero
        # elsewhere must convert to a field citing BOTH A and B — B must not be
        # silently dropped from the co-citation at this location.
        src = tmp_path / "g.docx"
        new_doc(src, ["Grouped foreign cite of two works here.",
                      "Existing zotero of work B here."])
        doc = Docx(src)
        root = doc.tree(DOCUMENT)
        grouped = " ADDIN CSL_CITATION " + json.dumps({
            "citationItems": [
                {"id": "A", "itemData": {"title": "Work A", "DOI": "10.1000/widgets.2001",
                                         "issued": {"date-parts": [["2019"]]}},
                 "uris": ["http://mendeley.com/a"]},
                {"id": "B", "itemData": {"title": "Work B", "DOI": "10.5555/already",
                                         "issued": {"date-parts": [["2020"]]}},
                 "uris": ["http://mendeley.com/b"]},
            ],
            "mendeley": {"plainTextFormattedCitation": "(a,b)"},
            "schema": "https://github.com/citation-style-language/schema/raw/master/csl-citation.json",
        }) + " "
        _append_complex_field(_find_para(root, "Grouped foreign cite"), grouped)
        _append_complex_field(_find_para(root, "Existing zotero of work B"), ZOTERO_INSTR)
        p = tmp_path / "p.docx"; doc.save(p)

        out = tmp_path / "o.docx"
        res = convert_to_zotero(p, out=out, managers=("mendeley",))
        cits = scan_citations(res["out"])
        keysets = [{it["key"] for it in c["items"]} for c in cits]
        # exactly one converted field cites BOTH A (MENKEY1) and B (ZKEY)
        assert {"MENKEY1", "ZKEY"} in keysets, keysets

    def test_grouped_partial_match_leaves_whole_field_foreign(self, tmp_path, fake_zotero):
        # FIX 1 — data loss: a GROUPED foreign cite (A, B) where A matches the
        # library but B does NOT must be left ENTIRELY foreign. The old behaviour
        # converted A alone, silently dropping B's co-citation — '(A,B)' -> '(A)'.
        # All-or-nothing (mirrors citelink.apply_cite_link): keep the group intact
        # until B is added to the library.
        src = tmp_path / "g.docx"
        new_doc(src, ["A grouped cite of one present and one missing work here."])
        doc = Docx(src)
        root = doc.tree(DOCUMENT)
        grouped = " ADDIN CSL_CITATION " + json.dumps({
            "citationItems": [
                {"id": "A", "itemData": {"title": "Work A Present",
                                         "DOI": "10.1000/widgets.2001",
                                         "issued": {"date-parts": [["2019"]]}},
                 "uris": ["http://mendeley.com/a"]},
                {"id": "B", "itemData": {"title": "Work B Missing",
                                         "DOI": "10.0000/missing",
                                         "issued": {"date-parts": [["2020"]]}},
                 "uris": ["http://mendeley.com/b"]},
            ],
            "mendeley": {"plainTextFormattedCitation": "(a,b)"},
            "schema": "https://github.com/citation-style-language/schema/raw/master/csl-citation.json",
        }) + " "
        _append_complex_field(_find_para(root, "grouped cite"), grouped)
        p = tmp_path / "p.docx"; doc.save(p)

        out = tmp_path / "o.docx"
        res = convert_to_zotero(p, out=out, managers=("mendeley",))

        # Nothing converted: the whole field is held back.
        assert res["converted"] == []
        # The matched member A must NOT have been converted alone (the data loss).
        keys = [it["key"] for cit in scan_citations(res["out"]) for it in cit["items"]]
        assert "MENKEY1" not in keys, f"co-cited member A dropped-and-converted alone: {keys}"
        # The foreign field survives intact for a human to fix.
        assert classify_citation_sources(res["out"])["counts"].get("mendeley", 0) >= 1
        # The field-level skip is reported with the explicit reason ...
        assert len(res["left_unconverted"]) == 1
        lu = res["left_unconverted"][0]
        assert lu["reason"] == "left unconverted: co-cited member unmatched"
        assert "MENKEY1" in lu["keys"]
        # ... and the missing member is still surfaced in unmatched.
        assert any(u["extracted"].get("doi") == "10.0000/missing" for u in res["unmatched"])

    def test_meta_cache_key_disambiguates_same_title_diff_year(self):
        # FIX 2 — reference merge: two DISTINCT same-title, DOI-less works must
        # get DIFFERENT per-run cache keys (old title-only key collapsed them).
        a = {"title": "Shared Title", "year": "2020", "authors": ["Smith J"]}
        b = {"title": "Shared Title", "year": "2021", "authors": ["Jones A"]}
        assert cc._meta_cache_key(a) != cc._meta_cache_key(b)
        # Same work (same title/year/surname token) still shares ONE key.
        b_same = {"title": "Shared Title", "year": "2020", "authors": ["Smith John"]}
        assert cc._meta_cache_key(a) == cc._meta_cache_key(b_same)

    def test_meta_cache_key_title_only_when_no_discriminators(self):
        # Both-absent fallback: no year/authors -> plain title key (unchanged).
        assert cc._meta_cache_key({"title": "Bare Title"}) == cc._tk("Bare Title")
        # A DOI always wins regardless of discriminators.
        assert cc._meta_cache_key({"title": "T", "doi": "10.1/x", "year": "2020"}) == cc._dk("10.1/x")

    def test_meta_disambiguators_match_matcher_derivation(self):
        # Digit-scanned 4-digit year + lowercased first-author surname token,
        # EXACTLY as _match_in_library derives them.
        assert cc._meta_disambiguators({"year": "2020-05", "authors": ["Smith J"]}) == ("2020", "smith")
        assert cc._meta_disambiguators({"year": "c2019", "authors": ["Doe RA"]}) == ("2019", "doe")
        assert cc._meta_disambiguators({"authors": ["Smith John"]}) == ("", "smith")
        assert cc._meta_disambiguators({"year": "20"}) == ("", "")   # <4 digits -> no year
        assert cc._meta_disambiguators({}) == ("", "")

    def test_distinct_same_title_works_not_reference_merged(self, tmp_path, fake_zotero, monkeypatch):
        # FIX 2 end-to-end: two DOI-less fields sharing a title but resolving to
        # DIFFERENT library items (via year/first-author disambiguation) must each
        # keep their OWN item. The old title-only cache key merged the second into
        # the first's match — a silent reference merge.
        from zoterocite import zotero
        # Empty offline index -> title lookup misses -> _match_in_library uses the
        # live search + year/first-author disambiguation below.
        monkeypatch.setattr(zotero, "library_index",
                            lambda **k: {"doi": {}, "pmid": {}, "title": {}})

        def search_items(query, qmode=None):
            if cc.normalize_title(query) == cc.normalize_title("Shared Title Alpha"):
                return [
                    {"key": "KEYA", "data": {"key": "KEYA", "title": "Shared Title Alpha",
                                             "date": "2020", "creators": [{"lastName": "Smith"}]}},
                    {"key": "KEYB", "data": {"key": "KEYB", "title": "Shared Title Alpha",
                                             "date": "2021", "creators": [{"lastName": "Jones"}]}},
                ]
            return []
        monkeypatch.setattr(zotero, "search_items", search_items)

        def _men(year, fam, given):
            return " ADDIN CSL_CITATION " + json.dumps({
                "citationItems": [{"id": "X", "itemData": {
                    "title": "Shared Title Alpha",
                    "author": [{"family": fam, "given": given}],
                    "issued": {"date-parts": [[year]]}}, "uris": ["u"]}],
                "mendeley": {"plainTextFormattedCitation": "(x)"},
                "schema": "https://github.com/citation-style-language/schema/raw/master/csl-citation.json",
            }) + " "

        src = tmp_path / "s.docx"
        new_doc(src, ["First cite of work one here.", "Second cite of work two here."])
        doc = Docx(src)
        root = doc.tree(DOCUMENT)
        _append_complex_field(_find_para(root, "work one"), _men("2020", "Smith", "John"))
        _append_complex_field(_find_para(root, "work two"), _men("2021", "Jones", "Alice"))
        p = tmp_path / "p.docx"; doc.save(p)

        out = tmp_path / "o.docx"
        res = convert_to_zotero(p, out=out, managers=("mendeley",))
        keys = sorted(it["key"] for cit in scan_citations(res["out"]) for it in cit["items"])
        assert keys == ["KEYA", "KEYB"], f"same-title works reference-merged in cache: {keys}"

    def test_already_zotero_titleonly_dedups_foreign_with_discriminators(self, tmp_path, fake_zotero, monkeypatch):
        # FIX 2 already_zotero interaction: an existing Zotero field whose CSL-JSON
        # lacks year/authors (a title-only key) must STILL dedup against a foreign
        # field of the same work that DOES carry year/first-author — else widening
        # the cache key would spuriously RE-CONVERT an already-converted cite.
        from zoterocite import zotero
        # Offline index resolves the DOI-less title, so the foreign field WOULD
        # convert absent the dedup guard.
        monkeypatch.setattr(zotero, "library_index",
                            lambda **k: {"doi": {}, "pmid": {},
                                         "title": {"recurrent work title": "RECKEY"}})

        src = tmp_path / "s.docx"
        new_doc(src, ["Foreign cite of the recurrent work here.",
                      "Existing zotero of the recurrent work here."])
        doc = Docx(src)
        root = doc.tree(DOCUMENT)
        foreign = " ADDIN CSL_CITATION " + json.dumps({
            "citationItems": [{"id": "X", "itemData": {
                "title": "Recurrent Work Title",
                "author": [{"family": "Smith", "given": "John"}],
                "issued": {"date-parts": [["2020"]]}}, "uris": ["u"]}],
            "mendeley": {"plainTextFormattedCitation": "(x)"},
            "schema": "https://github.com/citation-style-language/schema/raw/master/csl-citation.json",
        }) + " "
        zot = "ADDIN ZOTERO_ITEM CSL_CITATION " + json.dumps({
            "citationID": "ZID1",
            "properties": {"formattedCitation": "(1)", "plainCitation": "(1)", "noteIndex": 0},
            "citationItems": [{"id": "2504198/RECKEY", "uris": [GROUP + "RECKEY"],
                               "itemData": {"id": "2504198/RECKEY", "type": "article-journal",
                                            "title": "Recurrent Work Title"}}],
            "schema": "https://github.com/citation-style-language/schema/raw/master/csl-citation.json",
        })
        _append_complex_field(_find_para(root, "Foreign cite"), foreign)
        _append_complex_field(_find_para(root, "Existing zotero"), zot)
        p = tmp_path / "p.docx"; doc.save(p)

        out = tmp_path / "o.docx"
        res = convert_to_zotero(p, out=out, managers=("mendeley",))
        # No spurious re-convert: the foreign field is left alone (deduped).
        assert res["converted"] == []
        assert res["deduped"] >= 1
        keys = [it["key"] for cit in scan_citations(res["out"]) for it in cit["items"]]
        assert keys.count("RECKEY") == 1, f"already-converted cite re-converted: {keys}"

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

    def test_tracked_conversion_word_builtin_del_is_wellformed(self, tmp_path, fake_zotero):
        """Regression: tracking the deletion of a Word built-in (``<w:sdt>``)
        citation must convert the field text/code NESTED under ``<w:sdtContent>``
        to their tracked-deletion variants. The old ``_wrap_runs_as_del`` used
        ``findall`` on the sdt's DIRECT children (which has no ``<w:t>``/
        ``<w:instrText>``), leaving a LIVE ``<w:instrText> CITATION …>`` field and
        LIVE original ``<w:t>`` text inside the ``<w:del>`` — a malformed deletion
        (a live field surviving inside a deletion region).
        """
        path = build_mixed_doc(tmp_path, with_manual=False, with_zotero=False)
        out = tmp_path / "tw.docx"
        res = convert_to_zotero(path, out=out, managers=("word",), track=True)
        assert any(c["manager"] == "word" for c in res["converted"])
        assert validate(res["out"]).ok

        root = Docx(res["out"]).tree(DOCUMENT)
        dels = root.findall(".//" + qn("w:del"))
        assert dels, "expected a tracked deletion for the converted Word field"

        # (1) NO live <w:instrText> carrying a CITATION/ADDIN field code survives
        #     inside any <w:del> — it must have become <w:delInstrText>.
        for d in dels:
            for itx in d.iter(qn("w:instrText")):
                code = itx.text or ""
                assert "CITATION" not in code and "ADDIN" not in code, (
                    f"live <w:instrText> field code survived inside <w:del>: {code!r}"
                )
            # (2) NO live <w:t> original-citation text survives inside <w:del>;
            #     the struck display text must be <w:delText>.
            assert d.find(".//" + qn("w:t")) is None, (
                "live <w:t> original citation text survived inside <w:del>"
            )
            # The struck field code/text DID convert to their deletion variants.
            assert d.find(".//" + qn("w:delInstrText")) is not None
            assert d.find(".//" + qn("w:delText")) is not None

    def _word_only_doc(self, tmp_path: Path) -> Path:
        """A doc with ONLY a Word built-in (sdt) citation — so accept/reject views
        contain no other (untouched) foreign fields to confuse parsed-tree checks."""
        src = tmp_path / "wsrc.docx"
        new_doc(src, ["WordBuiltin sentence delta goes here."])
        doc = Docx(src)
        root = doc.tree(DOCUMENT)
        _append_word_sdt(_find_para(root, "sentence delta"), WORD_INSTR)
        _add_word_sources(doc)
        out = tmp_path / "wonly.docx"
        doc.save(out)
        return out

    def test_tracked_conversion_word_builtin_accept_reject_roundtrip(self, tmp_path, fake_zotero):
        """Accept and reject of the tracked Word-builtin conversion are each
        internally consistent: accepting drops the original content control and
        keeps the new Zotero field; rejecting restores the original citation text
        and removes the inserted Zotero field, with no orphaned/duplicated field.

        Checks operate on the PARSED tree (element presence), not raw-string
        substrings, so unrelated escaped XML text can't yield false matches.
        """
        from zoterocite.revisions import accept_all, reject_all

        def _has(root, tag):
            return root.find(".//" + qn(tag)) is not None

        def _instr_codes(root):
            return [(itx.text or "") for itx in root.iter(qn("w:instrText"))]

        # ACCEPT: original sdt (its <w:citation/> control + the Word CITATION field
        # code) is gone; the inserted live Zotero field remains.
        a_out = tmp_path / "wa.docx"
        convert_to_zotero(self._word_only_doc(tmp_path), out=a_out,
                          managers=("word",), track=True)
        da = Docx(a_out)
        accept_all(da)
        acc = tmp_path / "accepted.docx"
        da.save(acc)
        assert validate(acc).ok
        aroot = Docx(acc).tree(DOCUMENT)
        assert not _has(aroot, "w:del") and not _has(aroot, "w:ins")   # changes resolved
        assert not _has(aroot, "w:citation")                            # control gone
        assert all("CITATION Smith2020" not in c for c in _instr_codes(aroot))  # field code gone
        assert any("ZOTERO_ITEM" in c for c in _instr_codes(aroot))     # new field present

        # REJECT: inserted Zotero field gone; original citation's DISPLAY text and
        # its content control are restored; no leftover tracked-change wrappers.
        r_out = tmp_path / "wr.docx"
        convert_to_zotero(self._word_only_doc(tmp_path), out=r_out,
                          managers=("word",), track=True)
        dr = Docx(r_out)
        reject_all(dr)
        rej = tmp_path / "rejected.docx"
        dr.save(rej)
        assert validate(rej).ok
        rroot = Docx(rej).tree(DOCUMENT)
        assert not _has(rroot, "w:del") and not _has(rroot, "w:ins")   # changes resolved
        assert all("ZOTERO_ITEM" not in c for c in _instr_codes(rroot))  # inserted field gone
        assert _has(rroot, "w:citation")                                # control restored
        # original display text "(Smith, 2020)" is restored in a live <w:t>
        texts = "".join(t.text or "" for t in rroot.iter(qn("w:t")))
        assert "(Smith, 2020)" in texts

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


class TestIterFieldsIndexContract:
    """``_iter_fields`` assigns ``index`` by CARRIER GROUP (all sdt citations,
    then fldSimple, then complex), NOT document position. The only contract is
    that ``index`` is a UNIQUE per-field handle. This locks that behaviour so the
    (corrected) docstring stays honest and no caller starts assuming doc order."""

    def _body_with_complex_then_sdt(self):
        # Document order: a complex field FIRST, an sdt citation SECOND.
        body = etree.Element(qn("w:body"))
        p = etree.SubElement(body, qn("w:p"))
        r1 = etree.SubElement(p, qn("w:r"))
        etree.SubElement(r1, qn("w:fldChar")).set(qn("w:fldCharType"), "begin")
        r2 = etree.SubElement(p, qn("w:r"))
        etree.SubElement(r2, qn("w:instrText")).text = " ADDIN EN.CITE complexfield "
        r3 = etree.SubElement(p, qn("w:r"))
        etree.SubElement(r3, qn("w:fldChar")).set(qn("w:fldCharType"), "end")
        sdt = etree.SubElement(p, qn("w:sdt"))
        sdtpr = etree.SubElement(sdt, qn("w:sdtPr"))
        etree.SubElement(sdtpr, qn("w:citation"))
        etree.SubElement(sdt, qn("w:sdtContent"))
        return body

    def test_index_is_unique_per_field(self):
        body = self._body_with_complex_then_sdt()
        fields = cc._iter_fields(body)
        idxs = [f["index"] for f in fields]
        assert len(idxs) == len(set(idxs)), "each field needs a unique index handle"

    def test_index_is_carrier_grouped_not_document_order(self):
        # The sdt comes SECOND in the document but gets the LOWER index, because
        # sdt citations are emitted before complex fields. This is the documented
        # (carrier-grouped) behaviour; the index is not a document-position handle.
        body = self._body_with_complex_then_sdt()
        by_carrier = {f["carrier"]: f["index"] for f in cc._iter_fields(body)}
        assert by_carrier["sdt"] < by_carrier["complex"]


class TestGroupedCitationRendering:
    """A converted GROUPED multi-item foreign cite becomes ONE Zotero field. Its
    stored formattedCitation/plainCitation must NOT be the per-item markers joined
    with "; " (e.g. "(MENKEY1); (ZKEY)") — Zotero renders a single grouped field
    as one marker ("(1,2)"), and that spurious "; " corrupts both the pre-refresh
    display and the existing_renderings dedup guard. A single placeholder is the
    least-wrong, refresh-stable value when the true grouped marker is not
    obtainable from the per-item Zotero render API."""

    def _converted_field_props(self, tmp_path, fake_zotero):
        # Grouped Mendeley cite of TWO works that both resolve fresh in the fake
        # library (MENKEY1 via widgets DOI, ZKEY via the "already" DOI). Because
        # neither is cited via Zotero elsewhere, n_fresh > 0 and the whole field
        # converts to one grouped ZOTERO_ITEM field.
        src = tmp_path / "g.docx"
        new_doc(src, ["A grouped claim citing two works here."])
        doc = Docx(src)
        root = doc.tree(DOCUMENT)
        grouped = " ADDIN CSL_CITATION " + json.dumps({
            "citationItems": [
                {"id": "A", "itemData": {"title": "Work A",
                                         "DOI": "10.1000/widgets.2001",
                                         "issued": {"date-parts": [["2019"]]}},
                 "uris": ["http://mendeley.com/a"]},
                {"id": "B", "itemData": {"title": "Work B",
                                         "DOI": "10.5555/already",
                                         "issued": {"date-parts": [["2020"]]}},
                 "uris": ["http://mendeley.com/b"]},
            ],
            "mendeley": {"plainTextFormattedCitation": "(a,b)"},
            "schema": "https://github.com/citation-style-language/schema/raw/master/csl-citation.json",
        }) + " "
        _append_complex_field(_find_para(root, "grouped claim"), grouped)
        p = tmp_path / "p.docx"; doc.save(p)

        out = tmp_path / "o.docx"
        res = convert_to_zotero(p, out=out, managers=("mendeley",))
        # Pull the ZOTERO_ITEM field's JSON properties out of the output doc.
        body = Docx(res["out"]).raw(DOCUMENT).decode()
        marker = "ADDIN ZOTERO_ITEM CSL_CITATION "
        assert marker in body, "grouped cite must convert to a Zotero field"
        start = body.index(marker) + len(marker)
        depth, i = 0, start
        while i < len(body):
            if body[i] == "{":
                depth += 1
            elif body[i] == "}":
                depth -= 1
                if depth == 0:
                    i += 1
                    break
            i += 1
        import json as _json
        data = _json.loads(body[start:i])
        return data["properties"]

    def test_grouped_render_has_no_spurious_semicolon_join(self, tmp_path, fake_zotero):
        props = self._converted_field_props(tmp_path, fake_zotero)
        for k in ("formattedCitation", "plainCitation"):
            v = props.get(k, "")
            # The bug stored "(MENKEY1); (ZKEY)" — per-item markers joined with "; ".
            # A single grouped Zotero field never renders two parenthetical markers
            # joined by "; "; that separator is the fingerprint of the defect.
            assert "); (" not in v, f"{k} carries a fabricated '; ' group join: {v!r}"


# ===========================================================================
# GF-5 — _detect_manual_refs false-positive fixes
#
# Problem A: prose sentences that merely contain a 4-digit year must NOT be
#   flagged as suspected manual references.
# Problem B: the document's own rendered bibliography must NOT be flagged as
#   "add to Zotero and re-cite" — only the body is scanned.
# ===========================================================================

def _build_manual_ref_doc(tmp_path: Path, paras: list) -> Path:
    """Helper: create a minimal .docx with the given paragraph strings."""
    src = tmp_path / "src.docx"
    new_doc(src, paras)
    return src


class TestDetectManualRefsGF5:
    """Synthetic fixture tests for GF-5 — no real grant text / PHI."""

    # -----------------------------------------------------------------------
    # (i) Prose with years only -> NOT flagged
    # -----------------------------------------------------------------------

    def test_prose_year_only_not_flagged(self, tmp_path):
        """Body sentences that only contain a 4-digit year are NOT manual refs.

        Before the fix, _AUTHOR_YEAR_REF_RE matched sentences where an
        uppercase-starting word appeared before a year — e.g. "The RDCRN-DSC
        cohort began recruitment in 2015" and "The PREVeNT trial was completed
        in 2023" — causing false positives on plain narrative.
        """
        paras = [
            "The RDCRN-DSC cohort began recruitment in 2015 with an aim to include a wider age range.",
            "The PREVeNT trial was completed in 2023 and was designed as a phase IIb study.",
        ]
        path = _build_manual_ref_doc(tmp_path, paras)
        res = classify_citation_sources(path)
        manual = [it for it in res["items"] if it["manager"] == "manual"]
        assert manual == [], (
            f"Expected no manual refs from prose-with-year sentences, got: "
            f"{[m['instruction_excerpt'] for m in manual]}"
        )
        findings = classification_findings(res)
        assert not any(f.check == "CITE-MANUAL" for f in findings), (
            "CITE-MANUAL should not fire on prose-only-year sentences"
        )

    # -----------------------------------------------------------------------
    # (ii) Genuine inline citation in body -> STILL flagged (teeth check)
    # -----------------------------------------------------------------------

    def test_genuine_inline_citation_still_flagged(self, tmp_path):
        """A parenthetical author-year inline citation without a field is flagged.

        This is the real signal: a hand-typed "(Smith et al., 2020)" that lacks
        a Zotero/EndNote/Mendeley field and should be re-cited via Zotero.
        """
        paras = [
            "Prior work (Smith et al., 2020) showed that X leads to Y.",
        ]
        path = _build_manual_ref_doc(tmp_path, paras)
        res = classify_citation_sources(path)
        manual = [it for it in res["items"] if it["manager"] == "manual"]
        assert len(manual) >= 1, (
            "A genuine inline parenthetical citation must still be flagged as manual"
        )
        findings = classification_findings(res)
        assert any(f.check == "CITE-MANUAL" for f in findings), (
            "CITE-MANUAL must fire on a hand-typed inline citation"
        )

    # -----------------------------------------------------------------------
    # (iii) Trailing numbered reference list -> NOT flagged (Problem B)
    # -----------------------------------------------------------------------

    def test_bibliography_not_flagged_as_manual(self, tmp_path):
        """The document's own rendered bibliography is NOT re-flagged.

        Before the fix, every bibliography entry ("2. Peters JM...") was flagged
        as a "suspected manual/unmanaged reference — add to Zotero and re-cite."
        This was wrong: the bibliography is rendered output, not an inline citation
        that needs Zotero re-management.
        """
        paras = [
            # body
            "This study builds on prior work in the field.",
            "References",
            # >= 3 fake citation lines to trigger the bibliography boundary
            "1.\tDoe J, Smith A. A fake paper about things. J Fake Sci. 2020;1(1):1-10. doi:10.9999/fake.2020",
            "2.\tPeters JM, Roe R. Another fake paper. Neurology. 2019;5(2):20-30. PMID:12345678",
            "3.\tChou IJ, Wang X. Yet another fake reference. Brain. 2021;10(3):300. doi:10.9999/chou.2021",
        ]
        path = _build_manual_ref_doc(tmp_path, paras)
        res = classify_citation_sources(path)
        manual = [it for it in res["items"] if it["manager"] == "manual"]
        assert manual == [], (
            f"Bibliography entries must NOT be flagged as manual refs, got: "
            f"{[m['instruction_excerpt'] for m in manual]}"
        )
        findings = classification_findings(res)
        assert not any(f.check == "CITE-MANUAL" for f in findings), (
            "CITE-MANUAL must not fire on the document's own rendered bibliography"
        )

    # -----------------------------------------------------------------------
    # Teeth verification: confirm each assertion would fail under the OLD code
    # -----------------------------------------------------------------------

    def test_old_broad_regex_would_fail_prose_check(self):
        """Proof-of-teeth: the OLD _AUTHOR_YEAR_REF_RE matched prose-year sentences.

        The old pattern required two consecutive upper-case-starting tokens followed
        by a year, so "The RDCRN-DSC cohort began recruitment in 2015" matches
        (RDCRN-DSC is capitalised and cohort follows it before the year).
        """
        import re
        old_re = re.compile(r"[A-Z][A-Za-z''-]+,?\s+[A-Z].*\b(19|20)\d{2}\b")
        prose = "The RDCRN-DSC cohort began recruitment in 2015."
        assert old_re.search(prose) is not None, (
            "Sanity check: old regex DOES match prose-year sentences (this test "
            "confirms test (i) has teeth against the old code)"
        )

    def test_new_regex_does_not_match_prose_year(self):
        """After fix: the new _INLINE_CITE_RE does NOT match prose-year sentences."""
        from zoterocite.citeconvert import _INLINE_CITE_RE
        prose = "The trial began recruitment in 2015 and was completed in 2023."
        assert _INLINE_CITE_RE.search(prose) is None, (
            "_INLINE_CITE_RE must not match prose-only-year sentences"
        )

    def test_new_regex_matches_genuine_inline_cite(self):
        """After fix: _INLINE_CITE_RE matches a genuine parenthetical citation."""
        from zoterocite.citeconvert import _INLINE_CITE_RE
        inline = "Prior work (Smith et al., 2020) showed X."
        assert _INLINE_CITE_RE.search(inline) is not None, (
            "_INLINE_CITE_RE must match a genuine inline parenthetical citation"
        )


# ===========================================================================
# PART N — offline / fail-closed matching for unify-refs --apply
#
# Bug: convert_to_zotero -> _match_in_library -> zotero.get_item_by_doi /
# search_items -> build_request raised RuntimeError ("Zotero credentials not
# configured") with NO try/except, so `unify-refs --apply` CRASHED when Zotero
# was unreachable. Fix: match offline via the resilient cached library_index
# first; live search only as a reachable, crash-proof fallback; fail-closed
# (never create on a degraded read).
# ===========================================================================

def _unavailable_search(*_a, **_k):
    """Stand-in for a Zotero search when creds are missing / network down —
    exactly what zotero.build_request raises in that situation."""
    raise RuntimeError("Zotero credentials not configured; set env var(s): ZOTERO_API_KEY")


class TestOfflineMatchFailClosed:
    def _mendeley_only_doc(self, tmp_path):
        # EndNote(alpha) + Mendeley(beta); we process only the Mendeley field.
        return build_mixed_doc(tmp_path, with_word=False, with_zotero=False,
                               with_manual=False)

    # (a) Zotero unavailable (creds unset / network down) MUST NOT crash.
    def test_unavailable_zotero_does_not_raise(self, tmp_path, monkeypatch):
        from zoterocite import zotero
        # No offline index, and every live entrypoint raises like missing creds.
        monkeypatch.setattr(zotero, "library_index",
                            lambda **k: {"doi": {}, "pmid": {}, "title": {}})
        monkeypatch.setattr(zotero, "get_item_by_doi", _unavailable_search)
        monkeypatch.setattr(zotero, "search_items", _unavailable_search)
        monkeypatch.setattr(zotero, "csljson", _unavailable_search)
        monkeypatch.setattr(zotero, "item_uri", _unavailable_search)
        monkeypatch.setattr(zotero, "formatted_citations", _unavailable_search)

        out = tmp_path / "offline.docx"
        # Must complete without a RuntimeError traceback.
        res = convert_to_zotero(self._mendeley_only_doc(tmp_path), out=out,
                                managers=("mendeley",))
        # The Mendeley ref could not be matched (no index, search unreachable) →
        # left unconverted (fail-closed), and recorded.
        assert res["converted"] == []
        assert any(u["manager"] == "mendeley" for u in res["unmatched"])
        # A file was still written (the unchanged doc), and it validates.
        assert validate(res["out"]).ok

    # (b) A ref whose DOI is in a mocked cached library_index → matched OFFLINE,
    # field written, WITHOUT any live search call.
    def test_doi_in_cached_index_matches_offline(self, tmp_path, monkeypatch):
        from zoterocite import zotero
        live_calls = []
        monkeypatch.setattr(zotero, "library_index", lambda **k: {
            "doi": {"10.1000/widgets.2001": "CACHEKEY1"}, "pmid": {}, "title": {},
        })
        # If the offline path is correct, NEITHER of these is ever reached.
        monkeypatch.setattr(zotero, "get_item_by_doi",
                            lambda *a, **k: (live_calls.append("doi"), None)[1])
        monkeypatch.setattr(zotero, "search_items",
                            lambda *a, **k: (live_calls.append("title"), [])[1])
        # csljson/item_uri/formatted_citations are degrade-safe; make them
        # unreachable to prove a matched ref still writes its field offline.
        monkeypatch.setattr(zotero, "csljson", _unavailable_search)
        monkeypatch.setattr(zotero, "item_uri", _unavailable_search)
        monkeypatch.setattr(zotero, "formatted_citations", _unavailable_search)

        out = tmp_path / "matched.docx"
        res = convert_to_zotero(self._mendeley_only_doc(tmp_path), out=out,
                                managers=("mendeley",))

        assert live_calls == [], (
            "offline index hit must NOT trigger any live get_item_by_doi/"
            f"search_items call; saw {live_calls}"
        )
        keys = {it["key"] for cit in scan_citations(res["out"]) for it in cit["items"]}
        assert "CACHEKEY1" in keys, "matched-in-cache ref must get a live Zotero field"
        assert validate(res["out"]).ok

    # (c) Ref NOT in the index + Zotero unreachable → left unconverted, no crash.
    def test_index_miss_unreachable_left_unconverted(self, tmp_path, monkeypatch):
        from zoterocite import zotero
        monkeypatch.setattr(zotero, "library_index",
                            lambda **k: {"doi": {}, "pmid": {}, "title": {}})
        monkeypatch.setattr(zotero, "get_item_by_doi", _unavailable_search)
        monkeypatch.setattr(zotero, "search_items", _unavailable_search)

        out = tmp_path / "miss.docx"
        res = convert_to_zotero(self._mendeley_only_doc(tmp_path), out=out,
                                managers=("mendeley",))
        assert res["converted"] == []
        assert any(u["manager"] == "mendeley" for u in res["unmatched"])
        # No live field was written.
        keys = {it["key"] for cit in scan_citations(res["out"]) for it in cit["items"]}
        assert "MENKEY1" not in keys

    # (d) FAIL-CLOSED: a degraded read NEVER creates a library item.
    # Mutation-confirm: this guard has teeth because create_items is wired to
    # FAIL the test if it is ever invoked from the conversion path.
    def test_fail_closed_no_item_creation_on_degraded_read(self, tmp_path, monkeypatch):
        from zoterocite import zotero
        monkeypatch.setattr(zotero, "library_index",
                            lambda **k: {"doi": {}, "pmid": {}, "title": {}})
        monkeypatch.setattr(zotero, "get_item_by_doi", _unavailable_search)
        monkeypatch.setattr(zotero, "search_items", _unavailable_search)

        def _boom_create(*_a, **_k):
            raise AssertionError("convert_to_zotero must NEVER create a Zotero "
                                 "item on a degraded read (fail-closed)")
        monkeypatch.setattr(zotero, "create_items", _boom_create)

        out = tmp_path / "noadd.docx"
        # add_missing is a documented no-op in convert_to_zotero; assert it stays
        # one even on a degraded read (belt-and-suspenders).
        res = convert_to_zotero(self._mendeley_only_doc(tmp_path), out=out,
                                managers=("mendeley",), add_missing=True)
        assert res["converted"] == []
        assert any(u["manager"] == "mendeley" for u in res["unmatched"])

    # (e) PERF/leak guard (citeconvert:814): an index that is PRESENT but MISSES
    # the DOI must NOT trigger the live O(library) get_item_by_doi fetch_all scan —
    # step 1 already answered DOI presence against the whole-library index, so a
    # per-ref live rescan is the redundant slow-path (O(N·M) in the loop).
    # TEETH: drop the `and not lib_index` guard -> get_item_by_doi runs -> RED.
    def test_index_present_miss_skips_live_doi_scan(self, tmp_path, monkeypatch):
        from zoterocite import zotero
        monkeypatch.setattr(zotero, "library_index", lambda **k: {
            "doi": {"10.9/other": "OTHERKEY"}, "pmid": {}, "title": {}})

        def _must_not_scan(*_a, **_k):
            raise AssertionError("live get_item_by_doi fetch_all scan must NOT run "
                                 "when an offline index already answered DOI presence")
        monkeypatch.setattr(zotero, "get_item_by_doi", _must_not_scan)
        monkeypatch.setattr(zotero, "search_items", lambda *a, **k: [])  # title miss, degrade-safe

        out = tmp_path / "idxmiss.docx"
        res = convert_to_zotero(self._mendeley_only_doc(tmp_path), out=out,
                                managers=("mendeley",))
        assert res["converted"] == []
        assert any(u["manager"] == "mendeley" for u in res["unmatched"])

    # (f) When there is NO offline index at all (a hard library_index failure ->
    # lib_index is None), the live get_item_by_doi scan IS the sole DOI matcher and
    # must still resolve — the guard preserves it exactly there.
    def test_no_index_uses_live_doi_scan(self, tmp_path, monkeypatch):
        from zoterocite import zotero

        def _no_index(**_k):
            raise RuntimeError("index unavailable")  # -> lib_index None (caught in convert)
        monkeypatch.setattr(zotero, "library_index", _no_index)
        monkeypatch.setattr(zotero, "get_item_by_doi",
                            lambda doi, **k: {"key": "MENKEY1", "data": {"key": "MENKEY1"}}
                            if "widgets.2001" in doi else None)
        # degrade-safe post-match calls (a matched ref still writes its field offline)
        monkeypatch.setattr(zotero, "csljson", _unavailable_search)
        monkeypatch.setattr(zotero, "item_uri", _unavailable_search)
        monkeypatch.setattr(zotero, "formatted_citations", _unavailable_search)

        out = tmp_path / "noindex.docx"
        res = convert_to_zotero(self._mendeley_only_doc(tmp_path), out=out,
                                managers=("mendeley",))
        keys = {it["key"] for cit in scan_citations(res["out"]) for it in cit["items"]}
        assert "MENKEY1" in keys, "live DOI scan must resolve when no offline index exists"

    # Regression guard: lookup_index_key is the single owner — DOI -> PMID -> title.
    def test_lookup_index_key_precedence_offline(self):
        from zoterocite import zotero
        idx = {
            "doi": {"10.1/a": "DKEY"},
            "pmid": {"123456": "PKEY"},
            "title": {"a study of widgets": "TKEY"},
        }
        assert zotero.lookup_index_key(idx, doi="10.1/A") == "DKEY"
        assert zotero.lookup_index_key(idx, pmid="123456") == "PKEY"
        assert zotero.lookup_index_key(idx, title="A Study Of Widgets") == "TKEY"
        assert zotero.lookup_index_key(idx, doi="10.9/none") is None
        assert zotero.lookup_index_key(None, doi="10.1/a") is None
