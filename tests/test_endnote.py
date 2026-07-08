"""Tests for zoterocite.endnote — EndNote-library → Zotero migration + re-cite.

OFFLINE ONLY: no network, no real Zotero. The resolver, the library DOI index,
the retraction DB, and the Zotero write/convert are all monkeypatched. EndNote-XML
and RIS fixtures are inline strings; the document side is built with ``new_doc``
plus injected ``ADDIN EN.CITE`` complex fields.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from lxml import etree

from zoterocite import Docx, new_doc
from zoterocite.docxio import DOCUMENT
from zoterocite.ooxml import NS, qn
from zoterocite.endnote import (
    parse_endnote_library,
    plan_endnote_migration,
    apply_endnote_migration,
    validate_folder,
    WriteRefusedError,
)
from zoterocite import endnote as en

W = NS["w"]
GROUP = "http://zotero.org/groups/2504198/items/"


# ===========================================================================
# Fixtures — EndNote XML / RIS library exports
# ===========================================================================

# EndNote XML with: nested <style> wrapping on the title (and authors/year), a
# multi-author record, a DOI in <electronic-resource-num>, a PMID in
# <accession-num>.
ENDNOTE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<xml><records>
  <record>
    <rec-number>1</rec-number>
    <ref-type name="Journal Article">17</ref-type>
    <contributors><authors>
      <author><style face="normal">Smith, John A</style></author>
      <author><style face="normal">Doe, Jane B</style></author>
    </authors></contributors>
    <titles>
      <title><style face="normal" font="default" size="100%">Network localization of <style face="italic">tuberous sclerosis</style> phenotypes</style></title>
      <secondary-title>Annals of Neurology</secondary-title>
    </titles>
    <periodical><full-title>Annals of Neurology</full-title></periodical>
    <dates><year><style face="normal">2021</style></year></dates>
    <volume>89</volume>
    <pages>123-135</pages>
    <electronic-resource-num>10.1002/ana.99999</electronic-resource-num>
    <accession-num>34567890</accession-num>
  </record>
  <record>
    <rec-number>2</rec-number>
    <ref-type name="Journal Article">17</ref-type>
    <contributors><authors>
      <author>Brown, Carol</author>
    </authors></contributors>
    <titles><title>A single-author study of cortex</title></titles>
    <periodical><full-title>Brain</full-title></periodical>
    <dates><year>2019</year></dates>
    <electronic-resource-num>doi:10.1093/brain/awz123</electronic-resource-num>
  </record>
</records></xml>
"""

ENDNOTE_RIS = """TY  - JOUR
AU  - Smith, John A
AU  - Doe, Jane B
TI  - Network localization of tuberous sclerosis phenotypes
JO  - Annals of Neurology
PY  - 2021
VL  - 89
SP  - 123
EP  - 135
DO  - 10.1002/ana.99999
ER  -

TY  - JOUR
AU  - Brown, Carol
T1  - A single-author study of cortex
T2  - Brain
Y1  - 2019///
DO  - 10.1093/brain/awz123
ER  -
"""


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


# ===========================================================================
# Document fixture — a .docx with EndNote EN.CITE citation fields
# ===========================================================================

def _en_cite_instr(doi: str, title: str, author: str, year: str) -> str:
    """Build an ``ADDIN EN.CITE`` field code for one record (matches the shape
    citeconvert._extract_endnote_items parses)."""
    return (
        " ADDIN EN.CITE "
        f"<EndNote><Cite><Author>{author}</Author><Year>{year}</Year>"
        "<RecNum>1</RecNum>"
        "<DisplayText><style face=\"superscript\">[1]</style></DisplayText>"
        "<record><rec-number>1</rec-number>"
        "<ref-type name=\"Journal Article\">17</ref-type>"
        f"<contributors><authors><author>{author}</author></authors></contributors>"
        f"<titles><title>{title}</title></titles>"
        f"<dates><year>{year}</year></dates>"
        f"<electronic-resource-num>{doi}</electronic-resource-num>"
        "</record></Cite></EndNote> "
    )


def _append_complex_field(p, instr: str):
    def run():
        return etree.SubElement(p, qn("w:r"))
    r1 = run(); etree.SubElement(r1, qn("w:fldChar")).set(qn("w:fldCharType"), "begin")
    r2 = run(); it = etree.SubElement(r2, qn("w:instrText")); it.set(qn("xml:space"), "preserve"); it.text = instr
    r3 = run(); etree.SubElement(r3, qn("w:fldChar")).set(qn("w:fldCharType"), "separate")
    r4 = run(); t = etree.SubElement(r4, qn("w:t")); t.text = "[1]"
    r5 = run(); etree.SubElement(r5, qn("w:fldChar")).set(qn("w:fldCharType"), "end")


def _build_doc(tmp_path: Path) -> Path:
    """A .docx with two EndNote citations matching the two library records."""
    from zoterocite.paras import find_paragraph
    paras = [
        "Tubers localize to a network here alpha.",
        "Cortex develops abnormally here beta.",
    ]
    src = tmp_path / "draft.docx"
    new_doc(src, paras)
    doc = Docx(src)
    root = doc.tree(DOCUMENT)
    _append_complex_field(
        find_paragraph(root, "here alpha"),
        _en_cite_instr("10.1002/ana.99999", "Network localization of tuberous sclerosis phenotypes", "Smith", "2021"),
    )
    _append_complex_field(
        find_paragraph(root, "here beta"),
        _en_cite_instr("10.1093/brain/awz123", "A single-author study of cortex", "Brown", "2019"),
    )
    out = tmp_path / "draft_with_cites.docx"
    doc.save(out)
    return out


# ===========================================================================
# Offline stubs — resolver, library index, retraction DB, Zotero write/convert
# ===========================================================================

# Resolved "canonical" metadata keyed by the DOI present in the record query.
RESOLVED_BY_DOI = {
    "10.1002/ana.99999": {
        "doi": "10.1002/ana.99999",
        "title": "Network localization of tuberous sclerosis phenotypes",
        "authors": [{"family": "Smith", "given": "John A"}, {"family": "Doe", "given": "Jane B"}],
        "year": "2021", "journal": "Annals of Neurology", "type": "article-journal",
    },
    "10.1093/brain/awz123": {
        "doi": "10.1093/brain/awz123",
        "title": "A single-author study of cortex",
        "authors": [{"family": "Brown", "given": "Carol"}],
        "year": "2019", "journal": "Brain", "type": "article-journal",
    },
}


@pytest.fixture()
def offline(monkeypatch):
    """Stub every network/Zotero call so the plan/apply paths are deterministic."""
    from zoterocite import refresolve, zotero, citecheck, citeconvert

    def fake_resolve(text, *, fetch=True):
        # Identifier-first: find a known DOI substring in the query.
        for doi, meta in RESOLVED_BY_DOI.items():
            if doi in text:
                return {
                    "input": text, "metadata": dict(meta), "confidence": "high",
                    "source": "doi", "candidates": [dict(meta)], "identifiers": {},
                }
        return {"input": text, "metadata": None, "confidence": "none",
                "source": None, "candidates": [], "identifiers": {}}

    monkeypatch.setattr(refresolve, "resolve_reference", fake_resolve)
    monkeypatch.setattr(en.refresolve, "resolve_reference", fake_resolve)

    # Empty library + no retractions by default. The plan path now reads presence
    # via the combined DOI+PMID+title index (library_index_status, strict=False)
    # and the write path via library_index(strict=True); library_index_status is
    # a thin wrapper over library_index, so stubbing library_index covers both.
    # library_doi_index is kept stubbed for any residual/indirect caller.
    monkeypatch.setattr(zotero, "library_doi_index", lambda *a, **k: {})
    monkeypatch.setattr(en.zotero, "library_doi_index", lambda *a, **k: {})
    _empty_index = {"doi": {}, "pmid": {}, "title": {}}
    monkeypatch.setattr(zotero, "library_index", lambda *a, **k: dict(_empty_index))
    monkeypatch.setattr(en.zotero, "library_index", lambda *a, **k: dict(_empty_index))
    monkeypatch.setattr(citecheck, "ensure_retraction_db", lambda *a, **k: (None, None))
    monkeypatch.setattr(en.citecheck, "ensure_retraction_db", lambda *a, **k: (None, None))

    return monkeypatch


# ===========================================================================
# 1. parse_endnote_library — XML
# ===========================================================================

class TestParseXML:
    def test_style_stripped_and_fields(self, tmp_path):
        p = _write(tmp_path, "lib.xml", ENDNOTE_XML)
        recs = parse_endnote_library(p)
        assert len(recs) == 2
        r0 = recs[0]
        # Title: nested <style> (incl. an inner italic span) stripped to plain text.
        assert r0["title"] == "Network localization of tuberous sclerosis phenotypes"
        # Multi-author list, style-stripped.
        assert r0["authors"] == ["Smith, John A", "Doe, Jane B"]
        assert r0["doi"] == "10.1002/ana.99999"
        assert r0["year"] == "2021"
        assert r0["journal"] == "Annals of Neurology"
        assert r0["volume"] == "89"
        assert r0["pages"] == "123-135"
        # PMID mined from <accession-num> (all-digits).
        assert r0["pmid"] == "34567890"
        assert r0["ref_type"] == "Journal Article"

    def test_doi_prefix_stripped(self, tmp_path):
        p = _write(tmp_path, "lib.xml", ENDNOTE_XML)
        recs = parse_endnote_library(p)
        # Second record's DOI was given as "doi:10.1093/..." — prefix stripped.
        assert recs[1]["doi"] == "10.1093/brain/awz123"
        assert recs[1]["authors"] == ["Brown, Carol"]
        assert recs[1]["pmid"] is None


# ===========================================================================
# 1. parse_endnote_library — RIS
# ===========================================================================

class TestParseRIS:
    def test_multi_author_and_doi(self, tmp_path):
        p = _write(tmp_path, "lib.ris", ENDNOTE_RIS)
        recs = parse_endnote_library(p)
        assert len(recs) == 2
        r0 = recs[0]
        assert r0["authors"] == ["Smith, John A", "Doe, Jane B"]
        assert r0["title"] == "Network localization of tuberous sclerosis phenotypes"
        assert r0["journal"] == "Annals of Neurology"
        assert r0["year"] == "2021"
        assert r0["volume"] == "89"
        assert r0["pages"] == "123-135"
        assert r0["doi"] == "10.1002/ana.99999"
        assert r0["ref_type"] == "JOUR"

    def test_t1_t2_y1_aliases(self, tmp_path):
        p = _write(tmp_path, "lib.ris", ENDNOTE_RIS)
        recs = parse_endnote_library(p)
        r1 = recs[1]
        assert r1["title"] == "A single-author study of cortex"   # T1
        assert r1["journal"] == "Brain"                            # T2
        assert r1["year"] == "2019"                                # Y1 with ///
        assert r1["doi"] == "10.1093/brain/awz123"

    def test_ris_ID_tag_is_not_a_pmid(self, tmp_path):
        """TEETH: the RIS ``ID`` tag is EndNote's arbitrary internal Reference-ID
        (record number), NOT a PMID. Mis-parsing it as a pmid fabricates a
        high-confidence PubMed resolution to an UNRELATED paper (record numbers
        routinely land in the thousands, colliding with real low PMIDs) which
        then gets written into the shared Zotero group.

        RED before the fix (``ID`` populated ``pmid`` = "8412"); GREEN after
        (``ID`` is ignored → ``pmid`` is None, and the refresolve query carries
        no fabricated ``PMID:`` token)."""
        ris = (
            "TY  - JOUR\n"
            "AU  - Smith, J\n"
            "TI  - Some paper\n"
            "PY  - 2019\n"
            "ID  - 8412\n"
            "ER  -\n"
        )
        p = _write(tmp_path, "recnum.ris", ris)
        recs = parse_endnote_library(p)
        assert len(recs) == 1
        rec = recs[0]
        # The record number MUST NOT become a pmid.
        assert rec["pmid"] is None, "RIS ID (record number) must not be used as a PMID"
        # And it must not leak into the refresolve query as a PMID token, which is
        # what would drive the wrong high-confidence PubMed fetch/write.
        assert "PMID" not in en._record_query(rec)

    def test_ris_C2_still_carries_pmid(self, tmp_path):
        """Guard the KEPT branch: a legitimate PMID carried in ``C2`` is still
        parsed (only the ``ID`` record-number branch was removed)."""
        ris = (
            "TY  - JOUR\n"
            "AU  - Smith, J\n"
            "TI  - Some paper\n"
            "PY  - 2019\n"
            "C2  - 34567890\n"
            "ER  -\n"
        )
        p = _write(tmp_path, "withc2.ris", ris)
        recs = parse_endnote_library(p)
        assert recs[0]["pmid"] == "34567890"


# ===========================================================================
# 1. parse_endnote_library — tolerance
# ===========================================================================

class TestParseTolerance:
    def test_empty_file(self, tmp_path):
        p = _write(tmp_path, "empty.xml", "")
        assert parse_endnote_library(p) == []

    def test_malformed_xml(self, tmp_path):
        p = _write(tmp_path, "bad.xml", "<xml><records><record><titles><title>oops")
        # Unterminated XML → [] (no raise).
        assert parse_endnote_library(p) == []

    def test_missing_file(self, tmp_path):
        assert parse_endnote_library(tmp_path / "nope.ris") == []

    def test_garbage_content(self, tmp_path):
        p = _write(tmp_path, "junk.txt", "this is not a library at all\njust prose\n")
        assert parse_endnote_library(p) == []

    def test_ris_missing_fields_no_raise(self, tmp_path):
        ris = "TY  - JOUR\nAU  - Lone, Author\nER  -\n"
        p = _write(tmp_path, "thin.ris", ris)
        recs = parse_endnote_library(p)
        assert len(recs) == 1
        assert recs[0]["authors"] == ["Lone, Author"]
        assert recs[0]["title"] is None
        assert recs[0]["doi"] is None


# ===========================================================================
# 4. validate_folder
# ===========================================================================

class TestValidateFolder:
    def test_complete_record_no_finding(self, offline):
        rec = {
            "authors": ["Smith, J"], "title": "T", "year": "2021",
            "journal": "J", "volume": None, "pages": None,
            "doi": "10.1/x", "pmid": None, "ref_type": "JOUR",
        }
        findings = validate_folder([rec])
        assert findings == []

    def test_flags_missing_fields_and_no_id(self, offline):
        rec = {
            "authors": [], "title": None, "year": None,
            "journal": None, "volume": None, "pages": None,
            "doi": None, "pmid": None, "ref_type": None,
        }
        findings = validate_folder([rec])
        checks = {f.check for f in findings}
        assert "ENDNOTE-INCOMPLETE" in checks
        assert "ENDNOTE-NO-ID" in checks


# ===========================================================================
# 2. plan_endnote_migration — DRY RUN, no writes
# ===========================================================================

class TestPlan:
    def test_plan_counts_and_keys(self, tmp_path, offline):
        lib = _write(tmp_path, "lib.xml", ENDNOTE_XML)
        doc = _build_doc(tmp_path)

        # Guard: any Zotero WRITE in the plan path is a bug — make it explode.
        from zoterocite import zotero
        def _boom(*a, **k):  # noqa: ANN001
            raise AssertionError("plan must not write to Zotero")
        offline.setattr(zotero, "create_items", _boom)
        offline.setattr(en.zotero, "create_items", _boom)
        offline.setattr(zotero, "key_can_write", _boom)
        offline.setattr(en.zotero, "key_can_write", _boom)
        offline.setattr(zotero, "key_can_write_status", _boom)
        offline.setattr(en.zotero, "key_can_write_status", _boom)

        plan = plan_endnote_migration(doc, lib)

        assert plan["summary"]["n_records"] == 2
        assert len(plan["records"]) == 2
        # Empty library (stub) → both records would be created.
        assert plan["to_create"] == 2
        assert plan["to_match"] == 0
        # Both records resolved at high confidence via the DOI stub.
        assert all(r["confidence"] == "high" for r in plan["records"])
        # The document carries two EndNote citation fields, both matching a record.
        assert plan["doc_citations"] == 2
        assert plan["unmatched"] == []
        assert "to_create" in plan and "doc_citations" in plan
        assert plan["collection_name"] == en.IMPORTED_COLLECTION

    def test_plan_records_already_in_library(self, tmp_path, offline):
        lib = _write(tmp_path, "lib.xml", ENDNOTE_XML)
        doc = _build_doc(tmp_path)
        # Pretend one record is already present in the group library (by DOI).
        from zoterocite import zotero
        idx = {"doi": {"10.1002/ana.99999": "EXISTING1"}, "pmid": {}, "title": {}}
        offline.setattr(zotero, "library_index", lambda *a, **k: dict(idx))
        offline.setattr(en.zotero, "library_index", lambda *a, **k: dict(idx))

        plan = plan_endnote_migration(doc, lib)
        assert plan["to_match"] == 1
        assert plan["to_create"] == 1
        matched = [r for r in plan["records"] if r["in_library"]]
        assert len(matched) == 1
        assert matched[0]["existing_key"] == "EXISTING1"

    def test_plan_doi_less_present_by_pmid_not_missing(self, tmp_path, offline):
        """TEETH (the bug 59c4d64 fixed): a ref already in the shared group but
        whose library index entry has NO DOI (present only by PMID) must read
        PRESENT — not "missing". A DOI-only presence test false-flagged it
        missing, and a confident --apply then re-created it as a DUPLICATE.

        Library index: DOI map EMPTY, the item keyed ONLY under its PMID.
        Record 1 carries PMID 34567890 (EndNote <accession-num>); the resolver
        stub still hands back a DOI, so the OLD DOI-only path would NOT find it.
        """
        lib = _write(tmp_path, "lib.xml", ENDNOTE_XML)
        doc = _build_doc(tmp_path)
        from zoterocite import zotero
        idx = {"doi": {}, "pmid": {"34567890": "PMIDKEY1"}, "title": {}}
        offline.setattr(zotero, "library_index", lambda *a, **k: dict(idx))
        offline.setattr(en.zotero, "library_index", lambda *a, **k: dict(idx))

        plan = plan_endnote_migration(doc, lib)
        matched = [r for r in plan["records"] if r["in_library"]]
        assert len(matched) == 1, "DOI-less-but-PMID-present ref must read PRESENT"
        assert matched[0]["existing_key"] == "PMIDKEY1"
        assert plan["to_match"] == 1
        assert plan["to_create"] == 1, "only the genuinely-absent record is created"

    def test_plan_doi_less_present_by_title_not_missing(self, tmp_path, offline):
        """TEETH: same bug, matched by normalized TITLE when neither DOI nor PMID
        is in the index. Title precedence is the last line of defense against a
        duplicate write for a DOI-less group item."""
        lib = _write(tmp_path, "lib.xml", ENDNOTE_XML)
        doc = _build_doc(tmp_path)
        from zoterocite import zotero
        nt = zotero._normalize_title(
            "Network localization of tuberous sclerosis phenotypes"
        )
        idx = {"doi": {}, "pmid": {}, "title": {nt: "TITLEKEY1"}}
        offline.setattr(zotero, "library_index", lambda *a, **k: dict(idx))
        offline.setattr(en.zotero, "library_index", lambda *a, **k: dict(idx))

        plan = plan_endnote_migration(doc, lib)
        matched = [r for r in plan["records"] if r["in_library"]]
        assert len(matched) == 1, "DOI-less-but-title-present ref must read PRESENT"
        assert matched[0]["existing_key"] == "TITLEKEY1"

    def test_apply_threads_pmid_index_and_skips_doi_less_present(
        self, tmp_path, offline
    ):
        """TEETH (write path): apply must (a) thread the PMID index into
        create_items as defense-in-depth and (b) NOT create a record that is
        present in the library only by PMID — it is matched, not re-created."""
        lib = _write(tmp_path, "lib.xml", ENDNOTE_XML)
        doc = _build_doc(tmp_path)
        from zoterocite import zotero, citeconvert

        offline.setattr(zotero, "key_can_write", lambda: True)
        offline.setattr(en.zotero, "key_can_write", lambda: True)
        offline.setattr(zotero, "key_can_write_status", lambda: True)
        offline.setattr(en.zotero, "key_can_write_status", lambda: True)

        # Record 1 present ONLY by PMID; record 2 genuinely absent.
        idx = {"doi": {}, "pmid": {"34567890": "PMIDKEY1"}, "title": {}}
        offline.setattr(zotero, "library_index", lambda *a, **k: dict(idx))
        offline.setattr(en.zotero, "library_index", lambda *a, **k: dict(idx))

        captured = {}

        def fake_create_items(metas, *, collection=None, tags=None, dedup=True,
                              doi_index=None, pmid_index=None, attach_pdfs=False):
            captured["pmid_index"] = pmid_index
            captured["n"] = len(metas)
            return {"created": [{"title": m.get("title", ""), "key": f"K{i}",
                                 "doi": m.get("doi", "")}
                                for i, m in enumerate(metas)],
                    "skipped_existing": [], "failed": []}

        def fake_convert(path, *, out=None, managers=("endnote",), track=False, **kw):
            return {"out": str(out), "converted": [], "unmatched": [],
                    "manual_skipped": [], "deduped": 0,
                    "classification": {"counts": {}, "items": []}}

        offline.setattr(zotero, "create_items", fake_create_items)
        offline.setattr(en.zotero, "create_items", fake_create_items)
        offline.setattr(citeconvert, "convert_to_zotero", fake_convert)
        offline.setattr(en.citeconvert, "convert_to_zotero", fake_convert)

        report = apply_endnote_migration(doc, lib, out=tmp_path / "out.docx")
        # PMID index threaded for defense-in-depth dedup.
        assert captured.get("pmid_index") == {"34567890": "PMIDKEY1"}
        # Only the genuinely-absent record 2 is sent to create_items.
        assert captured.get("n") == 1, "DOI-less-but-PMID-present ref must NOT be re-created"
        assert len(report["created"]) == 1
        assert any(m.get("key") == "PMIDKEY1" for m in report["matched"])

    def test_plan_retracted_not_counted_for_create(self, tmp_path, offline):
        lib = _write(tmp_path, "lib.xml", ENDNOTE_XML)
        doc = _build_doc(tmp_path)
        from zoterocite import endnote
        # Force the first DOI to read as retracted.
        rw = {"10.1002/ana.99999": {"nature": "Retraction"}}
        offline.setattr(endnote, "_load_retraction_db", lambda: rw)

        plan = plan_endnote_migration(doc, lib)
        assert plan["summary"]["n_retracted"] == 1
        # Retracted record is excluded from to_create.
        assert plan["to_create"] == 1

    def test_plan_doi_surface_form_mismatch_still_matches(self, tmp_path, offline):
        """Regression: a doc DOI with https://doi.org/ prefix must match a bare
        library DOI — guards the single-normalizer contract on both sides of the
        lib_dois ⟷ ex_doi comparison."""
        # Library record carries a bare DOI.
        lib_xml = """<?xml version="1.0" encoding="UTF-8"?>
<xml><records>
  <record>
    <rec-number>1</rec-number>
    <ref-type name="Journal Article">17</ref-type>
    <contributors><authors><author>Smith, John A</author></authors></contributors>
    <titles><title>Network localization of tuberous sclerosis phenotypes</title></titles>
    <periodical><full-title>Annals of Neurology</full-title></periodical>
    <dates><year>2021</year></dates>
    <electronic-resource-num>10.1002/ana.99999</electronic-resource-num>
  </record>
</records></xml>
"""
        lib = _write(tmp_path, "lib_bare.xml", lib_xml)

        # Build a doc whose EN.CITE citation has the same DOI but with the full
        # https://doi.org/ prefix — surface-form mismatch that previously caused
        # a silent false-negative when the two normalizers diverged.
        from zoterocite.paras import find_paragraph
        src = tmp_path / "draft_url_doi.docx"
        new_doc(src, ["Surface form test here."])
        from zoterocite import Docx
        from zoterocite.docxio import DOCUMENT
        doc_obj = Docx(src)
        root = doc_obj.tree(DOCUMENT)
        _append_complex_field(
            find_paragraph(root, "here"),
            _en_cite_instr(
                "https://doi.org/10.1002/ana.99999",
                "Network localization of tuberous sclerosis phenotypes",
                "Smith",
                "2021",
            ),
        )
        doc = tmp_path / "draft_url_doi_with_cites.docx"
        doc_obj.save(doc)

        plan = plan_endnote_migration(doc, lib)

        # The citation must match — unmatched must be empty.
        assert plan["unmatched"] == [], (
            "DOI with https://doi.org/ prefix did not match bare library DOI "
            "— normalizer inconsistency still present"
        )


# ===========================================================================
# 3. apply_endnote_migration — gated write (stubbed)
# ===========================================================================

class TestApply:
    def test_below_high_confidence_imports_parsed_not_resolved_metadata(self, tmp_path, offline):
        # A record that resolves only at MEDIUM confidence (a possible mismatch)
        # must be migrated with the EndNote export's OWN parsed metadata, never an
        # uncertain (possibly wrong) resolved work's title/DOI.
        ris = ("TY  - JOUR\nTI  - Parsed Authoritative Title\n"
               "DO  - 10.123/parsed\nPY  - 2020\nER  -\n")
        lib = _write(tmp_path, "lib.ris", ris)
        doc = _build_doc(tmp_path)

        def wrong_medium_resolve(text, *, fetch=True):
            return {"input": text,
                    "metadata": {"title": "WRONG Resolved Work",
                                 "doi": "10.999/wrong", "year": "1999"},
                    "confidence": "medium", "source": "crossref",
                    "candidates": [], "identifiers": {}}
        offline.setattr(en.refresolve, "resolve_reference", wrong_medium_resolve)
        offline.setattr(en.zotero, "key_can_write", lambda: True)
        offline.setattr(en.zotero, "key_can_write_status", lambda: True)

        captured = {}

        def fake_create_items(metas, *, collection=None, tags=None, dedup=True, doi_index=None, pmid_index=None, attach_pdfs=False):
            captured["metas"] = metas
            return {"created": [{"title": m.get("title", ""), "key": f"K{i}",
                                 "doi": m.get("doi", "")} for i, m in enumerate(metas)],
                    "skipped_existing": [], "failed": []}

        def fake_convert(path, *, out=None, managers=("endnote",), track=False, **kw):
            return {"out": str(out), "converted": [], "unmatched": [],
                    "deduped": 0, "classification": {"counts": {}, "items": []}}

        offline.setattr(en.zotero, "create_items", fake_create_items)
        offline.setattr(en.citeconvert, "convert_to_zotero", fake_convert)

        apply_endnote_migration(doc, lib, out=tmp_path / "o.docx")
        metas = captured["metas"]
        assert len(metas) == 1
        m = metas[0]
        assert m["title"] == "Parsed Authoritative Title", m
        assert m["doi"] == "10.123/parsed", m
        assert "WRONG" not in m["title"] and m["doi"] != "10.999/wrong"

    def test_refuses_without_write_key(self, tmp_path, offline):
        lib = _write(tmp_path, "lib.xml", ENDNOTE_XML)
        doc = _build_doc(tmp_path)
        from zoterocite import zotero
        offline.setattr(zotero, "key_can_write", lambda: False)
        offline.setattr(en.zotero, "key_can_write", lambda: False)
        # Definitive no-write (F6 tri-state plain False, not UNKNOWN).
        offline.setattr(zotero, "key_can_write_status", lambda: False)
        offline.setattr(en.zotero, "key_can_write_status", lambda: False)

        with pytest.raises(WriteRefusedError, match="no write access"):
            apply_endnote_migration(doc, lib, out=tmp_path / "out.docx")

    def test_apply_calls_create_then_convert_in_order(self, tmp_path, offline):
        lib = _write(tmp_path, "lib.xml", ENDNOTE_XML)
        doc = _build_doc(tmp_path)
        from zoterocite import zotero, citeconvert

        calls = []

        offline.setattr(zotero, "key_can_write", lambda: True)
        offline.setattr(en.zotero, "key_can_write", lambda: True)
        offline.setattr(zotero, "key_can_write_status", lambda: True)
        offline.setattr(en.zotero, "key_can_write_status", lambda: True)

        def fake_create_items(metas, *, collection=None, tags=None, dedup=True, doi_index=None, pmid_index=None, attach_pdfs=False):
            calls.append(("create", collection, tuple(tags or ()), len(metas)))
            return {
                "created": [{"title": m.get("title", ""), "key": f"K{i}", "doi": m.get("doi", "")}
                            for i, m in enumerate(metas)],
                "skipped_existing": [], "failed": [],
            }

        def fake_convert(path, *, out=None, managers=("endnote",), track=False, **kw):
            calls.append(("convert", str(out), tuple(managers)))
            return {"out": str(out), "converted": [{"manager": "endnote"}],
                    "unmatched": [], "manual_skipped": [], "deduped": 0,
                    "classification": {"counts": {}, "items": []}}

        offline.setattr(zotero, "create_items", fake_create_items)
        offline.setattr(en.zotero, "create_items", fake_create_items)
        offline.setattr(citeconvert, "convert_to_zotero", fake_convert)
        offline.setattr(en.citeconvert, "convert_to_zotero", fake_convert)

        out = tmp_path / "recited.docx"
        report = apply_endnote_migration(doc, lib, out=out)

        # Order: create THEN convert.
        kinds = [c[0] for c in calls]
        assert kinds == ["create", "convert"]
        # create was tagged + targeted at the imported collection.
        assert calls[0][1] == en.IMPORTED_COLLECTION
        assert en.ADDED_TAG in calls[0][2]
        # convert restricted to endnote manager, writing to `out`.
        assert calls[1][2] == ("endnote",)
        assert calls[1][1] == str(out)

        assert len(report["created"]) == 2
        assert report["collection"] == en.IMPORTED_COLLECTION
        assert report["doc_out"] == str(out)

    def test_apply_skips_retracted_record(self, tmp_path, offline):
        lib = _write(tmp_path, "lib.xml", ENDNOTE_XML)
        doc = _build_doc(tmp_path)
        from zoterocite import zotero, citeconvert, endnote

        offline.setattr(zotero, "key_can_write", lambda: True)
        offline.setattr(en.zotero, "key_can_write", lambda: True)
        offline.setattr(zotero, "key_can_write_status", lambda: True)
        offline.setattr(en.zotero, "key_can_write_status", lambda: True)
        rw = {"10.1002/ana.99999": {"nature": "Retraction"}}
        offline.setattr(endnote, "_load_retraction_db", lambda: rw)

        created_metas = {}

        def fake_create_items(metas, *, collection=None, tags=None, dedup=True, doi_index=None, pmid_index=None, attach_pdfs=False):
            created_metas["n"] = len(metas)
            return {"created": [{"title": m.get("title", ""), "key": "K", "doi": ""} for m in metas],
                    "skipped_existing": [], "failed": []}

        def fake_convert(path, *, out=None, managers=("endnote",), track=False, **kw):
            return {"out": str(out), "converted": [], "unmatched": [],
                    "manual_skipped": [], "deduped": 0, "classification": {"counts": {}, "items": []}}

        offline.setattr(zotero, "create_items", fake_create_items)
        offline.setattr(en.zotero, "create_items", fake_create_items)
        offline.setattr(citeconvert, "convert_to_zotero", fake_convert)
        offline.setattr(en.citeconvert, "convert_to_zotero", fake_convert)

        report = apply_endnote_migration(doc, lib, out=tmp_path / "out.docx")
        # Only the non-retracted record is sent to create_items.
        assert created_metas["n"] == 1
        assert len(report["skipped_retracted"]) == 1
        assert report["skipped_retracted"][0]["doi"] == "10.1002/ana.99999"

    def test_degraded_library_read_refuses_migration(self, tmp_path, offline):
        """F2: a degraded library read (LibraryUnavailableError) must REFUSE the
        migration write — raise WriteRefusedError, never call create_items.

        Otherwise every record looks 'missing' and we'd create duplicates in the
        shared group library.
        """
        lib = _write(tmp_path, "lib.xml", ENDNOTE_XML)
        doc = _build_doc(tmp_path)
        from zoterocite import zotero

        offline.setattr(zotero, "key_can_write", lambda: True)
        offline.setattr(en.zotero, "key_can_write", lambda: True)
        offline.setattr(zotero, "key_can_write_status", lambda: True)
        offline.setattr(en.zotero, "key_can_write_status", lambda: True)

        def _raise(*a, **k):
            raise zotero.LibraryUnavailableError("read failed")

        offline.setattr(zotero, "library_index", _raise)
        offline.setattr(en.zotero, "library_index", _raise)
        offline.setattr(zotero, "library_doi_index", _raise)
        offline.setattr(en.zotero, "library_doi_index", _raise)

        write_attempts = []
        offline.setattr(zotero, "create_items",
                        lambda *a, **k: write_attempts.append(1) or {
                            "created": [], "skipped_existing": [], "failed": []})
        offline.setattr(en.zotero, "create_items",
                        lambda *a, **k: write_attempts.append(1) or {
                            "created": [], "skipped_existing": [], "failed": []})

        with pytest.raises(WriteRefusedError) as ei:
            apply_endnote_migration(doc, lib, out=tmp_path / "out.docx")

        assert "duplicate" in str(ei.value).lower()
        assert write_attempts == [], "create_items must NOT be called on a degraded read"

    def test_plan_marks_library_unavailable(self, tmp_path, offline):
        """F2: the read-only plan still succeeds but flags library_available=False
        when the DOI index could not be loaded (so apply can refuse)."""
        lib = _write(tmp_path, "lib.xml", ENDNOTE_XML)
        doc = _build_doc(tmp_path)
        from zoterocite import zotero

        def _raise(*a, **k):
            raise zotero.LibraryUnavailableError("read failed")

        offline.setattr(zotero, "library_index", _raise)
        offline.setattr(en.zotero, "library_index", _raise)
        offline.setattr(zotero, "library_doi_index", _raise)
        offline.setattr(en.zotero, "library_doi_index", _raise)

        plan = plan_endnote_migration(doc, lib)
        assert plan["library_available"] is False
        assert plan["summary"]["library_available"] is False
        # Plan still resolved records (degraded gracefully), just none in_library.
        assert plan["records"], "plan must still enumerate records"
        assert all(not r["in_library"] for r in plan["records"])

    def test_empty_library_still_migrates(self, tmp_path, offline):
        """F2 control: an EMPTY but readable library (index == {}) is not a
        failure — apply proceeds to create records normally."""
        lib = _write(tmp_path, "lib.xml", ENDNOTE_XML)
        doc = _build_doc(tmp_path)
        from zoterocite import zotero, citeconvert

        offline.setattr(zotero, "key_can_write", lambda: True)
        offline.setattr(en.zotero, "key_can_write", lambda: True)
        offline.setattr(zotero, "key_can_write_status", lambda: True)
        offline.setattr(en.zotero, "key_can_write_status", lambda: True)
        # `offline` fixture already sets library_doi_index -> {} (readable, empty).

        created_n = {}

        def fake_create_items(metas, *, collection=None, tags=None, dedup=True, doi_index=None, pmid_index=None, attach_pdfs=False):
            created_n["n"] = len(metas)
            return {"created": [{"title": m.get("title", ""), "key": f"K{i}", "doi": m.get("doi", "")}
                                for i, m in enumerate(metas)],
                    "skipped_existing": [], "failed": []}

        def fake_convert(path, *, out=None, managers=("endnote",), track=False, **kw):
            return {"out": str(out), "converted": [], "unmatched": [],
                    "manual_skipped": [], "deduped": 0, "classification": {"counts": {}, "items": []}}

        offline.setattr(zotero, "create_items", fake_create_items)
        offline.setattr(en.zotero, "create_items", fake_create_items)
        offline.setattr(citeconvert, "convert_to_zotero", fake_convert)
        offline.setattr(en.citeconvert, "convert_to_zotero", fake_convert)

        report = apply_endnote_migration(doc, lib, out=tmp_path / "out.docx")
        assert created_n.get("n") == 2, "both records created into an empty library"
        assert len(report["created"]) == 2


# ---------------------------------------------------------------------------
# F6: _looks_like_pmid recognizes PMID_RE forms
# ---------------------------------------------------------------------------

class TestLooksLikePmid:
    """_looks_like_pmid must recognize prefixed PMID forms via textpatterns.PMID_RE."""

    def setup_method(self):
        from zoterocite.endnote import _looks_like_pmid
        self._fn = _looks_like_pmid

    def test_bare_digits_still_recognized(self):
        assert self._fn("12345678") == "12345678"

    def test_pmid_colon_prefix(self):
        assert self._fn("pmid:12345678") == "12345678"

    def test_pmid_space_no_colon(self):
        """'PMID 12345678' (no colon) must be recognized."""
        result = self._fn("PMID 12345678")
        assert result == "12345678", f"Expected '12345678', got {result!r}"

    def test_pubmed_pmid_colon(self):
        """'PubMed PMID: 12345678' must be recognized."""
        result = self._fn("PubMed PMID: 12345678")
        assert result == "12345678", f"Expected '12345678', got {result!r}"

    def test_non_pmid_string_returns_none(self):
        assert self._fn("not-a-pmid") is None

    def test_empty_returns_none(self):
        assert self._fn("") is None
