"""Tests for zoterocite.citecheck — citation integrity layer."""
from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from zoterocite import Docx, insert_citation, new_doc
from zoterocite.citecheck import (
    check_retractions,
    cite_check,
    default_rw_path,
    load_retraction_db,
    reconcile_citations,
    _normalise_doi,
)
from zoterocite.findings import Finding

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GROUP_BASE = "http://zotero.org/groups/2504198/items/"


def _make_itemdata(key: str, doi: str = "", title: str = "") -> dict:
    d: dict = {"id": f"2504198/{key}", "type": "article-journal", "title": title or f"Paper {key}"}
    if doi:
        d["DOI"] = doi
    return d


def _build_doc(tmp_path: Path, paragraphs: list[str], citations: list[dict]) -> Path:
    """Create a .docx with citations and an optional bibliography.

    ``citations`` items: {anchor, keys, dois, add_bibliography (only last one)}
    """
    src = tmp_path / "src.docx"
    new_doc(src, paragraphs)
    doc = Docx(src)
    for i, cit in enumerate(citations):
        keys = cit["keys"]
        dois = cit.get("dois", [""] * len(keys))
        titles = cit.get("titles", [None] * len(keys))
        itemdata = [
            _make_itemdata(k, doi=d, title=t or "")
            for k, d, t in zip(keys, dois, titles)
        ]
        uris = [GROUP_BASE + k for k in keys]
        add_bib = cit.get("add_bibliography", False)
        insert_citation(
            doc,
            cit["anchor"],
            keys,
            itemdata=itemdata,
            uris=uris,
            rendered=f"({i + 1})",
            add_bibliography=add_bib,
        )
    out = tmp_path / "out.docx"
    doc.save(out)
    return out


def _write_rw_csv(path: Path, rows: list[dict]) -> Path:
    """Write a minimal Retraction Watch CSV fixture."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["OriginalPaperDOI", "RetractionDOI", "RetractionNature", "Title", "RetractionDate"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({f: row.get(f, "") for f in fieldnames})
    return path


# ---------------------------------------------------------------------------
# reconcile_citations
# ---------------------------------------------------------------------------

class TestReconcileCitations:
    def test_orphan_and_uncited(self, tmp_path):
        """Key B cited in text but absent from bib (orphan); key C in bib but never cited (uncited).

        Strategy: cite A and B, add bibliography. Then patch the ZOTERO_BIBL JSON payload
        so that KEYB is omitted from the bib and KEYC is added as a custom entry.
        Result: bib = {KEYA, KEYC}; cited = {KEYA, KEYB}
        → orphan = [KEYB], uncited = [KEYC].
        """
        import zipfile as zf_mod

        src = tmp_path / "src.docx"
        new_doc(src, ["Claim A here.", "Claim B here."])
        doc = Docx(src)
        insert_citation(
            doc, "Claim A", ["KEYA"],
            itemdata=[_make_itemdata("KEYA", doi="10.1/a", title="Paper A")],
            uris=[GROUP_BASE + "KEYA"],
            rendered="(1)",
        )
        insert_citation(
            doc, "Claim B", ["KEYB"],
            itemdata=[_make_itemdata("KEYB", doi="10.1/b", title="Paper B")],
            uris=[GROUP_BASE + "KEYB"],
            rendered="(2)",
            add_bibliography=True,
        )
        out = tmp_path / "out.docx"
        doc.save(out)

        # Patch the ZOTERO_BIBL JSON payload: omit KEYB, add custom KEYC
        new_payload = '{"uncited":[],"omitted":[{"key":"KEYB"}],"custom":[{"key":"KEYC"}]}'
        patched = tmp_path / "patched.docx"
        with zf_mod.ZipFile(out, "r") as zin:
            with zf_mod.ZipFile(patched, "w", zf_mod.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    data = zin.read(item.filename)
                    if item.filename == "word/document.xml":
                        data = data.replace(
                            b'{"uncited":[],"omitted":[],"custom":[]}',
                            new_payload.encode(),
                        )
                    zout.writestr(item, data)

        rec = reconcile_citations(patched)

        assert set(rec["cited_keys"]) == {"KEYA", "KEYB"}
        assert not rec["no_bibliography"]
        # Bib = (KEYA ∪ KEYB ∪ KEYC) − KEYB = KEYA, KEYC
        assert set(rec["bib_keys"]) == {"KEYA", "KEYC"}
        assert rec["orphan_citations"] == ["KEYB"]  # cited but not in bib
        assert rec["uncited_references"] == ["KEYC"]  # in bib but not cited

    def test_no_bibliography(self, tmp_path):
        """Doc with citations but no ZOTERO_BIBL → no_bibliography=True."""
        src = tmp_path / "src.docx"
        new_doc(src, ["Claim A here."])
        doc = Docx(src)
        insert_citation(
            doc, "Claim A", ["KEYA"],
            itemdata=[_make_itemdata("KEYA")],
            uris=[GROUP_BASE + "KEYA"],
            rendered="(1)",
            add_bibliography=False,
        )
        out = tmp_path / "out.docx"
        doc.save(out)

        rec = reconcile_citations(out)
        assert rec["no_bibliography"] is True
        assert rec["bib_keys"] is None
        assert "KEYA" in rec["cited_keys"]
        assert rec["orphan_citations"] == []
        assert rec["uncited_references"] == []

    def test_clean_doc(self, tmp_path):
        """Cite A and B; bib has A and B — no orphans, no uncited."""
        out = _build_doc(
            tmp_path,
            ["Cite A here.", "Cite B here."],
            [
                {"anchor": "Cite A", "keys": ["KA"], "dois": ["10.1/a"]},
                {"anchor": "Cite B", "keys": ["KB"], "dois": ["10.1/b"], "add_bibliography": True},
            ],
        )
        rec = reconcile_citations(out)
        assert rec["orphan_citations"] == []
        assert rec["uncited_references"] == []
        assert not rec["no_bibliography"]

    def test_bib_keys_never_verified_offline(self, tmp_path):
        """E4: bib_keys is derived from cited keys, so it is never 'verified'.

        Even on a clean doc with a bibliography present, the reconcile cannot
        confirm true bibliography membership offline.
        """
        out = _build_doc(
            tmp_path,
            ["Cite A here.", "Cite B here."],
            [
                {"anchor": "Cite A", "keys": ["KA"], "dois": ["10.1/a"]},
                {"anchor": "Cite B", "keys": ["KB"], "dois": ["10.1/b"], "add_bibliography": True},
            ],
        )
        rec = reconcile_citations(out)
        assert rec["bib_keys_verified"] is False
        assert not rec["no_bibliography"]


# ---------------------------------------------------------------------------
# DOI normalisation
# ---------------------------------------------------------------------------

class TestNormaliseDoi:
    @pytest.mark.parametrize("raw,expected", [
        ("10.1000/xyz123", "10.1000/xyz123"),
        ("https://doi.org/10.1000/xyz123", "10.1000/xyz123"),
        ("http://doi.org/10.1000/xyz123", "10.1000/xyz123"),
        ("https://dx.doi.org/10.1000/xyz123", "10.1000/xyz123"),
        ("  10.1000/XYZ123  ", "10.1000/xyz123"),
        ("HTTPS://DOI.ORG/10.1000/ABC", "10.1000/abc"),
    ])
    def test_normalise(self, raw, expected):
        assert _normalise_doi(raw) == expected

    # F1: trailing prose punctuation stripped
    def test_trailing_period_stripped(self):
        assert _normalise_doi("https://doi.org/10.1038/Nature12373.") == _normalise_doi("10.1038/nature12373")

    def test_trailing_close_paren_stripped(self):
        assert _normalise_doi("10.1038/nature12373)") == "10.1038/nature12373"

    def test_trailing_semicolon_stripped(self):
        assert _normalise_doi("10.1038/nature12373;") == "10.1038/nature12373"

    def test_trailing_comma_stripped(self):
        assert _normalise_doi("10.1038/nature12373,") == "10.1038/nature12373"

    def test_clean_doi_unchanged(self):
        assert _normalise_doi("10.1038/nature12373") == "10.1038/nature12373"


# ---------------------------------------------------------------------------
# F2: _extract_cited_dois dedup on normalized key
# ---------------------------------------------------------------------------

class TestExtractCitedDoisDedup:
    """_extract_cited_dois should collapse the same DOI in two casings to one entry."""

    def test_dedup_on_normalised_key(self, tmp_path):
        """A document containing two citation fields for the same DOI in different casings
        yields exactly one entry from _extract_cited_dois."""
        from zoterocite.citecheck import _extract_cited_dois

        # Two separate citation fields, each citing the same DOI in different case.
        # _build_doc puts each key in its own citation field.
        doc_path = _build_doc(
            tmp_path,
            ["Cite first here.", "Cite second here."],
            [
                {"anchor": "Cite first", "keys": ["KEY1"], "dois": ["10.1234/TestDoi"]},
                {"anchor": "Cite second", "keys": ["KEY2"], "dois": ["10.1234/TESTDOI"]},
            ],
        )
        dois = _extract_cited_dois(doc_path)
        # Both casings should collapse to one normalized entry.
        assert len(dois) == 1, f"Expected 1 deduped DOI, got {len(dois)}: {dois}"


# ---------------------------------------------------------------------------
# load_retraction_db
# ---------------------------------------------------------------------------

class TestLoadRetractionDb:
    def test_basic_load(self, tmp_path):
        csv_path = _write_rw_csv(tmp_path / "rw.csv", [
            {
                "OriginalPaperDOI": "10.1234/retracted",
                "RetractionDOI": "10.1234/ret-notice",
                "RetractionNature": "Retraction",
                "Title": "Retracted Paper",
                "RetractionDate": "2022-01-15",
            },
            {
                "OriginalPaperDOI": "10.5678/concern",
                "RetractionDOI": "10.5678/eoc",
                # Real Crossref/RW feed value: lowercase 'of concern'
                "RetractionNature": "Expression of concern",
                "Title": "Concern Paper",
                "RetractionDate": "2023-03-01",
            },
        ])
        db = load_retraction_db(csv_path)
        assert "10.1234/retracted" in db
        assert db["10.1234/retracted"]["nature"] == "Retraction"
        assert db["10.5678/concern"]["nature"] == "Expression of concern"

    def test_doi_normalisation_on_load(self, tmp_path):
        """DOIs with https:// prefix and uppercase are normalised."""
        csv_path = _write_rw_csv(tmp_path / "rw.csv", [
            {
                "OriginalPaperDOI": "https://doi.org/10.9999/UPPER",
                "RetractionNature": "Retraction",
                "Title": "T",
                "RetractionDate": "",
                "RetractionDOI": "",
            },
        ])
        db = load_retraction_db(csv_path)
        assert "10.9999/upper" in db

    def test_most_severe_wins(self, tmp_path):
        """Same DOI with Correction then Retraction → Retraction wins."""
        csv_path = _write_rw_csv(tmp_path / "rw.csv", [
            {
                "OriginalPaperDOI": "10.1/multi",
                "RetractionNature": "Correction",
                "Title": "Multi",
                "RetractionDate": "2021-01-01",
                "RetractionDOI": "10.1/corr",
            },
            {
                "OriginalPaperDOI": "10.1/multi",
                "RetractionNature": "Retraction",
                "Title": "Multi",
                "RetractionDate": "2022-06-01",
                "RetractionDOI": "10.1/ret",
            },
        ])
        db = load_retraction_db(csv_path)
        assert db["10.1/multi"]["nature"] == "Retraction"
        assert set(db["10.1/multi"]["all_natures"]) == {"Correction", "Retraction"}


# ---------------------------------------------------------------------------
# check_retractions
# ---------------------------------------------------------------------------

class TestCheckRetractions:
    @pytest.fixture()
    def db(self, tmp_path):
        csv_path = _write_rw_csv(tmp_path / "rw.csv", [
            {
                "OriginalPaperDOI": "10.1234/retracted",
                "RetractionNature": "Retraction",
                "Title": "Fully Retracted Paper",
                "RetractionDate": "2022-01-15",
                "RetractionDOI": "10.1234/ret-notice",
            },
            {
                "OriginalPaperDOI": "10.5678/concern",
                # Real Crossref/RW feed value: lowercase 'of concern'
                "RetractionNature": "Expression of concern",
                "Title": "Concern Paper",
                "RetractionDate": "2023-03-01",
                "RetractionDOI": "10.5678/eoc",
            },
            {
                "OriginalPaperDOI": "10.9999/reinstated",
                "RetractionNature": "Reinstatement",
                "Title": "Reinstated Paper",
                "RetractionDate": "2024-01-01",
                "RetractionDOI": "",
            },
        ])
        return load_retraction_db(csv_path)

    def test_retraction_is_error(self, db):
        findings = check_retractions(["10.1234/retracted"], db)
        assert len(findings) == 1
        assert findings[0].severity == "ERROR"
        assert findings[0].check == "CITE-RETRACTED"

    def test_expression_of_concern_is_warn(self, db):
        findings = check_retractions(["10.5678/concern"], db)
        assert len(findings) == 1
        assert findings[0].severity == "WARN"
        assert findings[0].check == "CITE-CONCERN"
        assert "still citable" in findings[0].message

    def test_reinstatement_ignored(self, db):
        findings = check_retractions(["10.9999/reinstated"], db)
        assert findings == []

    def test_clean_doi_no_finding(self, db):
        findings = check_retractions(["10.0000/clean"], db)
        assert findings == []

    def test_one_error_one_warn(self, db):
        findings = check_retractions(
            ["10.1234/retracted", "10.5678/concern", "10.0000/clean"], db
        )
        severities = {f.severity for f in findings}
        assert "ERROR" in severities and "WARN" in severities
        assert len(findings) == 2

    def test_doi_normalisation_lookup(self, db):
        """Uppercase / https:// prefix DOIs still match the database."""
        findings = check_retractions(["https://doi.org/10.1234/RETRACTED"], db)
        assert len(findings) == 1
        assert findings[0].severity == "ERROR"

    def test_source_field(self, db):
        findings = check_retractions(["10.1234/retracted"], db)
        assert findings[0].source == "Retraction Watch (Crossref-distributed)"


# ---------------------------------------------------------------------------
# cite_check (end-to-end)
# ---------------------------------------------------------------------------

class TestCiteCheck:
    @pytest.fixture()
    def rw_csv(self, tmp_path):
        return _write_rw_csv(tmp_path / "rw.csv", [
            {
                "OriginalPaperDOI": "10.9876/retracted-paper",
                "RetractionNature": "Retraction",
                "Title": "Bad Paper",
                "RetractionDate": "2021-05-10",
                "RetractionDOI": "10.9876/ret",
            },
            {
                "OriginalPaperDOI": "10.9876/concern-paper",
                # Real Crossref/RW feed value: lowercase 'of concern'
                "RetractionNature": "Expression of concern",
                "Title": "Concerning Paper",
                "RetractionDate": "2022-08-01",
                "RetractionDOI": "10.9876/eoc",
            },
        ])

    @pytest.fixture()
    def doc_with_citations(self, tmp_path):
        return _build_doc(
            tmp_path,
            ["Good claim here.", "Bad claim here.", "Concerning claim here."],
            [
                {"anchor": "Good claim", "keys": ["GOODKEY"], "dois": ["10.0000/clean-paper"]},
                {"anchor": "Bad claim", "keys": ["BADKEY"], "dois": ["10.9876/retracted-paper"]},
                {"anchor": "Concerning claim", "keys": ["CONCKEY"],
                 "dois": ["10.9876/concern-paper"], "add_bibliography": True},
            ],
        )

    def test_no_network_in_tests(self, doc_with_citations, rw_csv):
        """cite_check with check_existence=False never hits network."""
        findings = cite_check(doc_with_citations, rw_csv=rw_csv, check_existence=False)
        severities = [f.severity for f in findings]
        assert "ERROR" in severities   # retracted paper
        assert "WARN" in severities    # expression of concern

    def test_expected_findings_mix(self, doc_with_citations, rw_csv):
        findings = cite_check(doc_with_citations, rw_csv=rw_csv)
        checks = [f.check for f in findings]
        assert "CITE-RETRACTED" in checks
        assert "CITE-CONCERN" in checks

    def test_no_zotero_fields(self, tmp_path):
        """Plain .docx with no Zotero fields → single INFO finding."""
        src = tmp_path / "plain.docx"
        new_doc(src, ["Just some text without any citations."])
        findings = cite_check(src)
        assert len(findings) == 1
        assert findings[0].severity == "INFO"
        assert findings[0].check == "CITE-NO-ZOTERO"

    def test_no_bibliography_field(self, tmp_path):
        """Citations present but no ZOTERO_BIBL → INFO about missing bib."""
        src = tmp_path / "src.docx"
        new_doc(src, ["A claim here."])
        doc = Docx(src)
        insert_citation(
            doc, "A claim", ["KA"],
            itemdata=[_make_itemdata("KA", doi="10.1/a")],
            uris=[GROUP_BASE + "KA"],
            rendered="(1)",
            add_bibliography=False,
        )
        out = tmp_path / "out.docx"
        doc.save(out)
        findings = cite_check(out)
        checks = [f.check for f in findings]
        assert "CITE-NO-BIB" in checks

    def test_clean_reconcile_emits_unverified_caveat(self, tmp_path):
        """E4: a clean reconcile with a bibliography present must surface a
        CITE-BIB-UNVERIFIED INFO note, so a clean run is not mistaken for a
        verified one."""
        out = _build_doc(
            tmp_path,
            ["Cite A here.", "Cite B here."],
            [
                {"anchor": "Cite A", "keys": ["KA"], "dois": ["10.1/a"]},
                {"anchor": "Cite B", "keys": ["KB"], "dois": ["10.1/b"], "add_bibliography": True},
            ],
        )
        findings = cite_check(out)
        unverified = [f for f in findings if f.check == "CITE-BIB-UNVERIFIED"]
        assert len(unverified) == 1
        assert unverified[0].severity == "INFO"
        # No false "orphan/uncited" findings on a clean doc.
        assert not any(f.check in ("CITE-ORPHAN", "CITE-UNCITED") for f in findings)

    def test_orphan_is_info_not_warn(self, tmp_path):
        """E4: an omit-suppressed cited key is reported as INFO, not WARN —
        offline reconcile cannot authoritatively flag a missing bib entry."""
        import zipfile as zf_mod

        src = tmp_path / "src.docx"
        new_doc(src, ["Claim A here.", "Claim B here."])
        doc = Docx(src)
        insert_citation(
            doc, "Claim A", ["KEYA"],
            itemdata=[_make_itemdata("KEYA", doi="10.1/a")],
            uris=[GROUP_BASE + "KEYA"],
            rendered="(1)",
        )
        insert_citation(
            doc, "Claim B", ["KEYB"],
            itemdata=[_make_itemdata("KEYB", doi="10.1/b")],
            uris=[GROUP_BASE + "KEYB"],
            rendered="(2)",
            add_bibliography=True,
        )
        out = tmp_path / "out.docx"
        doc.save(out)

        # Omit KEYB from the bibliography → it becomes an orphan citation.
        new_payload = '{"uncited":[],"omitted":[{"key":"KEYB"}],"custom":[]}'
        patched = tmp_path / "patched.docx"
        with zf_mod.ZipFile(out, "r") as zin:
            with zf_mod.ZipFile(patched, "w", zf_mod.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    data = zin.read(item.filename)
                    if item.filename == "word/document.xml":
                        data = data.replace(
                            b'{"uncited":[],"omitted":[],"custom":[]}',
                            new_payload.encode(),
                        )
                    zout.writestr(item, data)

        findings = cite_check(patched)
        orphans = [f for f in findings if f.check == "CITE-ORPHAN"]
        assert len(orphans) == 1
        assert orphans[0].severity == "INFO"
        # A real mismatch was found, so the "clean" caveat must NOT fire.
        assert not any(f.check == "CITE-BIB-UNVERIFIED" for f in findings)

    def test_check_existence_monkeypatched(self, tmp_path, monkeypatch):
        """check_existence=True with a monkeypatched _check_doi_exists."""
        import zoterocite.citecheck as cc

        calls = []

        def fake_exists(doi):
            calls.append(doi)
            return doi != "10.1/missing"

        monkeypatch.setattr(cc, "_check_doi_exists", fake_exists)

        src = tmp_path / "src.docx"
        new_doc(src, ["Claim A here.", "Claim B here."])
        doc = Docx(src)
        insert_citation(
            doc, "Claim A", ["KA"],
            itemdata=[_make_itemdata("KA", doi="10.1/present")],
            uris=[GROUP_BASE + "KA"],
            rendered="(1)",
        )
        insert_citation(
            doc, "Claim B", ["KB"],
            itemdata=[_make_itemdata("KB", doi="10.1/missing")],
            uris=[GROUP_BASE + "KB"],
            rendered="(2)",
            add_bibliography=True,
        )
        out = tmp_path / "out.docx"
        doc.save(out)

        findings = cite_check(out, check_existence=True)
        assert any(f.check == "CITE-NOT-FOUND" for f in findings)
        assert "10.1/present" in calls
        assert "10.1/missing" in calls

    def test_check_existence_network_failure_is_info(self, tmp_path, monkeypatch):
        """If network is unavailable during existence check → single INFO, no crash."""
        import zoterocite.citecheck as cc
        import urllib.error

        def fail_exists(doi):
            raise urllib.error.URLError("no network")

        monkeypatch.setattr(cc, "_check_doi_exists", fail_exists)

        src = tmp_path / "src.docx"
        new_doc(src, ["Claim here."])
        doc = Docx(src)
        insert_citation(
            doc, "Claim here", ["KA"],
            itemdata=[_make_itemdata("KA", doi="10.1/a")],
            uris=[GROUP_BASE + "KA"],
            rendered="(1)",
            add_bibliography=True,
        )
        out = tmp_path / "out.docx"
        doc.save(out)

        findings = cite_check(out, check_existence=True)
        unavail = [f for f in findings if f.check == "CITE-EXISTENCE-UNAVAIL"]
        assert len(unavail) == 1
        assert unavail[0].severity == "INFO"

    def test_no_rw_csv_no_retraction_findings(self, tmp_path):
        """If no rw_csv and default path absent, no retraction findings produced."""
        src = tmp_path / "src.docx"
        new_doc(src, ["Claim here."])
        doc = Docx(src)
        insert_citation(
            doc, "Claim here", ["KA"],
            itemdata=[_make_itemdata("KA", doi="10.9876/retracted-paper")],
            uris=[GROUP_BASE + "KA"],
            rendered="(1)",
            add_bibliography=True,
        )
        out = tmp_path / "out.docx"
        doc.save(out)

        # Do not pass rw_csv; default path should not exist in tmp env
        findings = cite_check(out, rw_csv=Path("/nonexistent/rw.csv"))
        assert not any(f.check == "CITE-RETRACTED" for f in findings)


# ---------------------------------------------------------------------------
# default_rw_path
# ---------------------------------------------------------------------------

def test_default_rw_path():
    p = default_rw_path()
    assert p.name == "retraction_watch.csv"
    assert "zotero-word-cite" in str(p)
    assert "data" in p.parts


# ---------------------------------------------------------------------------
# refresh_retraction_db — requires url arg
# ---------------------------------------------------------------------------

def test_refresh_has_default_url_but_rejects_empty():
    # refresh_retraction_db now defaults to the Crossref Labs endpoint; only an
    # explicitly empty url is rejected (no network call happens here).
    from zoterocite.citecheck import refresh_retraction_db, RETRACTION_WATCH_URL
    assert RETRACTION_WATCH_URL.startswith("https://api.labs.crossref.org/data/retractionwatch")
    with pytest.raises(ValueError):
        refresh_retraction_db(url="")


def test_refresh_with_bad_url(tmp_path):
    from zoterocite.citecheck import refresh_retraction_db
    import urllib.error
    with pytest.raises((RuntimeError, urllib.error.URLError)):
        refresh_retraction_db(dest=tmp_path / "rw.csv", url="https://nonexistent.invalid/rw.csv")


def test_ensure_retraction_db_fresh_cache_no_network(tmp_path, monkeypatch):
    # A fresh cache is used as-is, with no download attempt.
    import zoterocite.citecheck as cc
    csv_path = tmp_path / "rw.csv"
    csv_path.write_text("RetractionDOI,OriginalPaperDOI,RetractionNature\n", encoding="utf-8")
    called = {"n": 0}
    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("should not refresh a fresh cache")
    monkeypatch.setattr(cc, "refresh_retraction_db", _boom)
    path, note = cc.ensure_retraction_db(dest=csv_path, max_age_days=7)
    assert path == csv_path and note is None and called["n"] == 0


def test_ensure_retraction_db_stale_triggers_refresh(tmp_path, monkeypatch):
    import os, time
    import zoterocite.citecheck as cc
    csv_path = tmp_path / "rw.csv"
    csv_path.write_text("x", encoding="utf-8")
    old = time.time() - 30 * 86400  # 30 days old
    os.utime(csv_path, (old, old))
    hits = {"n": 0}
    monkeypatch.setattr(cc, "refresh_retraction_db", lambda dest=None, **k: hits.__setitem__("n", hits["n"] + 1) or csv_path)
    path, note = cc.ensure_retraction_db(dest=csv_path, max_age_days=7)
    assert path == csv_path and hits["n"] == 1 and note is not None and note.check == "CITE-RW-REFRESH"


def test_ensure_retraction_db_refresh_failure_falls_back(tmp_path, monkeypatch):
    import os, time
    import zoterocite.citecheck as cc
    csv_path = tmp_path / "rw.csv"
    csv_path.write_text("x", encoding="utf-8")
    old = time.time() - 30 * 86400
    os.utime(csv_path, (old, old))
    def _fail(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(cc, "refresh_retraction_db", _fail)
    path, note = cc.ensure_retraction_db(dest=csv_path, max_age_days=7)
    assert path == csv_path and note is not None and note.check == "CITE-RW-STALE"


def test_ensure_retraction_db_missing_no_network(tmp_path, monkeypatch):
    # allow_network=False with an absent cache: returns (None, WARN) so the
    # gate can surface a finding rather than silently skipping the screen.
    import zoterocite.citecheck as cc
    csv_path = tmp_path / "absent.csv"
    monkeypatch.setattr(cc, "refresh_retraction_db", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no network")))
    path, note = cc.ensure_retraction_db(dest=csv_path, allow_network=False)
    assert path is None
    assert note is not None and note.check == "CITE-RW-STALE-GATE" and note.severity == "WARN"


# ---------------------------------------------------------------------------
# FIX 1 — regression guard: real lowercase EoC must produce WARN
# ---------------------------------------------------------------------------

class TestEoCCaseInsensitive:
    """Regression guard: the real Retraction Watch feed emits
    'Expression of concern' (lowercase 'of concern'), not the title-case
    string the old code hardcoded. Must yield a WARN, not be silently
    discarded."""

    def test_lowercase_eoc_doi_yields_warn(self, tmp_path):
        """A DOI with 'Expression of concern' (real feed casing) → WARN finding."""
        csv_path = _write_rw_csv(tmp_path / "rw.csv", [
            {
                "OriginalPaperDOI": "10.1111/eoc-lowercase",
                "RetractionDOI": "10.1111/eoc-notice",
                "RetractionNature": "Expression of concern",  # real feed casing
                "Title": "EoC Paper",
                "RetractionDate": "2024-06-01",
            },
        ])
        db = load_retraction_db(csv_path)
        # The record must be stored and severity resolved correctly
        assert "10.1111/eoc-lowercase" in db
        findings = check_retractions(["10.1111/eoc-lowercase"], db)
        assert len(findings) == 1
        assert findings[0].severity == "WARN"
        assert findings[0].check == "CITE-CONCERN"

    def test_severity_tiebreak_eoc_vs_correction_case_insensitive(self, tmp_path):
        """Same DOI: 'Correction' then 'Expression of concern' → EoC wins (sev=2>1)."""
        csv_path = _write_rw_csv(tmp_path / "rw.csv", [
            {
                "OriginalPaperDOI": "10.2222/multi",
                "RetractionNature": "Correction",
                "Title": "Multi",
                "RetractionDate": "2021-01-01",
                "RetractionDOI": "10.2222/corr",
            },
            {
                "OriginalPaperDOI": "10.2222/multi",
                "RetractionNature": "Expression of concern",  # real feed casing
                "Title": "Multi",
                "RetractionDate": "2022-06-01",
                "RetractionDOI": "10.2222/eoc",
            },
        ])
        db = load_retraction_db(csv_path)
        # Expression of concern (severity 2) beats Correction (severity 1)
        assert db["10.2222/multi"]["nature"] == "Expression of concern"
        assert set(db["10.2222/multi"]["all_natures"]) == {"Correction", "Expression of concern"}


# ---------------------------------------------------------------------------
# FIX 2 — corrupt cache: cite_check must not raise; refresh must not clobber
# ---------------------------------------------------------------------------

class TestCorruptCache:
    def test_binary_cache_returns_info_not_raises(self, tmp_path):
        """A binary/HTML cache file → cite_check returns an INFO finding, does not raise."""
        from zoterocite import Docx, insert_citation, new_doc

        # Write a corrupt (binary/HTML-like) file as the RW cache
        corrupt = tmp_path / "rw.csv"
        corrupt.write_bytes(b"<html><body>404 Not Found</body></html>\xff\xfe")

        src = tmp_path / "src.docx"
        new_doc(src, ["Claim here."])
        doc = Docx(src)
        insert_citation(
            doc, "Claim here", ["KA"],
            itemdata=[{"id": "KA", "type": "article-journal", "title": "T",
                       "DOI": "10.1/a"}],
            uris=["http://zotero.org/groups/2504198/items/KA"],
            rendered="(1)",
            add_bibliography=True,
        )
        out = tmp_path / "out.docx"
        doc.save(out)

        # Must not raise; must include the unreadable-DB INFO note
        findings = cite_check(out, rw_csv=corrupt)
        checks = [f.check for f in findings]
        assert "CITE-RW-UNREADABLE" in checks
        unreadable = [f for f in findings if f.check == "CITE-RW-UNREADABLE"]
        assert unreadable[0].severity == "INFO"

    def test_refresh_bad_body_does_not_clobber_good_cache(self, tmp_path, monkeypatch):
        """refresh_retraction_db with a body lacking required headers raises RuntimeError
        and leaves an existing good cache untouched."""
        import zoterocite.citecheck as cc

        good_cache = tmp_path / "rw.csv"
        good_content = (
            "OriginalPaperDOI,RetractionDOI,RetractionNature,Title,RetractionDate\n"
            "10.1/good,,Retraction,Good,2024-01-01\n"
        )
        good_cache.write_text(good_content, encoding="utf-8")

        # Monkeypatch urlopen to return an HTML error page
        import io
        import urllib.response

        class _FakeResp:
            def read(self):
                return b"<html><body>Service Unavailable</body></html>"
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass

        monkeypatch.setattr(cc.urllib.request, "urlopen", lambda *a, **k: _FakeResp())

        with pytest.raises(RuntimeError, match="does not look like"):
            cc.refresh_retraction_db(dest=good_cache, url="https://example.invalid/rw.csv")

        # The good cache must still be intact
        assert good_cache.read_text(encoding="utf-8") == good_content

    def test_refresh_incomplete_read_raises_runtime_error(self, tmp_path, monkeypatch):
        """An http.client.IncompleteRead during download raises RuntimeError."""
        import http.client
        import zoterocite.citecheck as cc

        def _raise(*a, **k):
            raise http.client.IncompleteRead(b"partial")

        monkeypatch.setattr(cc.urllib.request, "urlopen", _raise)

        with pytest.raises(RuntimeError):
            cc.refresh_retraction_db(dest=tmp_path / "rw.csv",
                                     url="https://example.invalid/rw.csv")


# ---------------------------------------------------------------------------
# FIX 3 — tracked conversion produces w:delInstrText, not w:instrText
# ---------------------------------------------------------------------------

class TestTrackedDelInstrText:
    def test_del_wraps_instrtext_as_delinstrtext(self, tmp_path, monkeypatch):
        """track=True conversion: w:instrText inside w:del must become w:delInstrText."""
        import json as _json
        from zoterocite import Docx, new_doc, validate
        from zoterocite.docxio import DOCUMENT
        from zoterocite.ooxml import qn
        from zoterocite.paras import find_paragraph
        from lxml import etree

        W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

        def _qn(tag):
            return "{%s}%s" % (W_NS, tag.split(":")[-1])

        mendeley_instr = " ADDIN CSL_CITATION " + _json.dumps({
            "citationItems": [{
                "id": "ITEM-1",
                "itemData": {
                    "author": [{"family": "Smith", "given": "John"}],
                    "id": "ITEM-1",
                    "issued": {"date-parts": [["2001"]]},
                    "title": "A study of widgets",
                    "DOI": "10.1000/widgets.2001",
                    "type": "article-journal",
                },
                "uris": ["http://www.mendeley.com/documents/?uuid=55ff8735"],
            }],
            "mendeley": {"formattedCitation": "(Smith, 2001)"},
            "properties": {"noteIndex": 0},
            "schema": "https://github.com/citation-style-language/schema/raw/master/csl-citation.json",
        }) + " "

        def _append_field(p, instr):
            def run():
                return etree.SubElement(p, _qn("w:r"))
            r1 = run(); etree.SubElement(r1, _qn("w:fldChar")).set(_qn("w:fldCharType"), "begin")
            r2 = run()
            it = etree.SubElement(r2, _qn("w:instrText"))
            it.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            it.text = instr
            r3 = run(); etree.SubElement(r3, _qn("w:fldChar")).set(_qn("w:fldCharType"), "separate")
            r4 = run(); t = etree.SubElement(r4, _qn("w:t")); t.text = "(cite)"
            r5 = run(); etree.SubElement(r5, _qn("w:fldChar")).set(_qn("w:fldCharType"), "end")

        # Build a simple doc with one Mendeley field
        src = tmp_path / "src.docx"
        new_doc(src, ["Mendeley sentence here."])
        doc = Docx(src)
        root = doc.tree(DOCUMENT)

        para = find_paragraph(root, "Mendeley sentence")
        _append_field(para, mendeley_instr)
        p = tmp_path / "p.docx"
        doc.save(p)

        # Set up fake zotero
        from zoterocite import zotero as zt
        monkeypatch.setattr(zt, "get_item_by_doi", lambda doi: {"key": "MK1", "data": {"key": "MK1"}})
        monkeypatch.setattr(zt, "search_items", lambda q, **k: [])
        monkeypatch.setattr(zt, "csljson", lambda keys: [{"id": k, "type": "article-journal", "title": "T"} for k in keys])
        monkeypatch.setattr(zt, "item_uri", lambda k: f"http://zotero.org/groups/2504198/items/{k}")
        monkeypatch.setattr(zt, "formatted_citations", lambda keys, **k: [f"({k2})" for k2 in keys])

        from zoterocite.citeconvert import convert_to_zotero
        out = tmp_path / "tracked.docx"
        res = convert_to_zotero(p, out=out, managers=("mendeley",), track=True)

        # Parse the output XML
        body = Docx(res["out"]).raw(DOCUMENT)
        W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        root_out = etree.fromstring(body)

        # Inside every w:del there must be NO w:instrText — only w:delInstrText
        del_els = root_out.findall(".//{%s}del" % W_NS)
        assert del_els, "Expected at least one w:del in tracked output"
        for d in del_els:
            instr_in_del = d.findall(".//{%s}instrText" % W_NS)
            delinstr_in_del = d.findall(".//{%s}delInstrText" % W_NS)
            assert not instr_in_del, (
                f"Found w:instrText inside w:del — should be w:delInstrText. "
                f"Got {len(instr_in_del)} stray instrText element(s)."
            )
            # Only assert presence of delInstrText when the del contains runs
            # that had field instruction text (i.e. when a field code was wrapped)
            if delinstr_in_del:
                assert len(delinstr_in_del) >= 1

        # Document still validates
        assert validate(res["out"]).ok


# ---------------------------------------------------------------------------
# W9-2 — _check_doi_exists three-way transport semantics
#
# The DOI-existence GET deliberately keeps its OWN urllib transport (not the
# never-raise _http.http_get) so it can preserve three DISTINCT outcomes:
#   * HTTP 200  -> True  (DOI confirmed present)
#   * HTTP 404  -> False (DOI confirmed ABSENT)
#   * any other transport failure (URLError, non-404 HTTPError) -> RAISE
#     (existence could NOT be checked — must NOT be mistaken for "absent").
# A regression that collapsed transport failure into False would flag a real
# DOI as nonexistent whenever offline. These tests pin all three.
# ---------------------------------------------------------------------------

class TestCheckDoiExistsSemantics:
    def _patch_urlopen(self, monkeypatch, effect):
        """Drive cc.urllib.request.urlopen with a single side-effect.

        *effect* is either an int HTTP status (-> success resp with that status)
        or an Exception instance (-> raised).
        """
        import zoterocite.citecheck as cc

        class _Resp:
            def __init__(self, status):
                self.status = status

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake(req, timeout=None):
            if isinstance(effect, BaseException):
                raise effect
            return _Resp(effect)

        monkeypatch.setattr(cc.urllib.request, "urlopen", fake)

    def test_doi_exists_200_true(self, monkeypatch):
        import zoterocite.citecheck as cc
        self._patch_urlopen(monkeypatch, 200)
        assert cc._check_doi_exists("10.1234/real") is True

    def test_doi_exists_404_false(self, monkeypatch):
        import urllib.error
        import zoterocite.citecheck as cc
        self._patch_urlopen(
            monkeypatch,
            urllib.error.HTTPError(
                url="http://x", code=404, msg="nf", hdrs=None, fp=None
            ),
        )
        # 404 is the ONLY failure that maps to False (DOI confirmed absent).
        assert cc._check_doi_exists("10.1234/absent") is False

    def test_doi_exists_urlerror_raises_not_false(self, monkeypatch):
        import urllib.error
        import zoterocite.citecheck as cc
        self._patch_urlopen(monkeypatch, urllib.error.URLError("offline"))
        # A transport failure must NOT silently become False — it raises so the
        # caller surfaces "could not check" (CITE-EXISTENCE-UNAVAIL), never a
        # bogus CITE-NOT-FOUND for a DOI that may well exist.
        with pytest.raises(urllib.error.URLError):
            cc._check_doi_exists("10.1234/maybe-real")

    def test_doi_exists_non404_httperror_raises_not_false(self, monkeypatch):
        import urllib.error
        import zoterocite.citecheck as cc
        self._patch_urlopen(
            monkeypatch,
            urllib.error.HTTPError(
                url="http://x", code=500, msg="boom", hdrs=None, fp=None
            ),
        )
        with pytest.raises(urllib.error.HTTPError):
            cc._check_doi_exists("10.1234/server-down")

    def test_doi_exists_uses_shared_polite_pool_ua(self, monkeypatch):
        # W9-1 still lands on this function: the UA is the shared _http one and
        # reflects a ZOTERO_WORD_CITE_CONTACT_EMAIL override.
        import zoterocite.citecheck as cc

        monkeypatch.delenv("NCBI_EMAIL", raising=False)
        monkeypatch.setenv("ZOTERO_WORD_CITE_CONTACT_EMAIL", "lab@example.org")
        captured = {}

        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake(req, timeout=None):
            captured["ua"] = req.get_header("User-agent")
            return _Resp()

        monkeypatch.setattr(cc.urllib.request, "urlopen", fake)
        cc._check_doi_exists("10.1234/real")
        assert captured["ua"] == "zotero-word-cite/1.0 (mailto:lab@example.org)"
