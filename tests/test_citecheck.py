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


def _make_itemdata(key: str, doi: str = "", title: str = "",
                   authors: list = None, pmid: str = "",
                   extra: str = "") -> dict:
    d: dict = {"id": f"2504198/{key}", "type": "article-journal", "title": title or f"Paper {key}"}
    if doi:
        d["DOI"] = doi
    if authors is not None:
        d["author"] = authors
    if pmid:
        d["PMID"] = pmid
    if extra:
        d["extra"] = extra
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
        authors = cit.get("authors", [None] * len(keys))
        pmids = cit.get("pmids", [""] * len(keys))
        extras = cit.get("extras", [""] * len(keys))
        itemdata = [
            _make_itemdata(k, doi=d, title=t or "", authors=au, pmid=pm, extra=ex)
            for k, d, t, au, pm, ex in zip(keys, dois, titles, authors, pmids, extras)
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
    # refresh_retraction_db defaults to the public GitLab raw CSV; only an
    # explicitly empty url is rejected (no network call happens here).
    from zoterocite.citecheck import refresh_retraction_db, RETRACTION_WATCH_URL
    assert RETRACTION_WATCH_URL.startswith(
        "https://gitlab.com/crossref/retraction-watch-data/-/raw/main/")
    with pytest.raises(ValueError):
        refresh_retraction_db(url="")


# ---------------------------------------------------------------------------
# The Retraction Watch CSV now comes from the canonical public GitLab raw blob
# (Crossref deprecated the api.labs.crossref.org Labs endpoint and points to
# GitLab). The GitLab raw blob needs no polite-pool mailto and no auth, and
# serves the byte-identical 21-column schema the parser already expects.
# ---------------------------------------------------------------------------

def test_retraction_watch_url_is_gitlab_raw_csv():
    from zoterocite.citecheck import RETRACTION_WATCH_URL
    assert RETRACTION_WATCH_URL == (
        "https://gitlab.com/crossref/retraction-watch-data/-/raw/main/retraction_watch.csv")
    assert "mailto=" not in RETRACTION_WATCH_URL
    assert "api.labs.crossref.org" not in RETRACTION_WATCH_URL


def test_refresh_default_url_is_gitlab_raw_no_mailto(monkeypatch):
    # The default download URL must be the GitLab raw CSV — no mailto query, even
    # with a contact-email override set. Capture the URL that reaches urlopen.
    import zoterocite.citecheck as cc

    monkeypatch.delenv("NCBI_EMAIL", raising=False)
    monkeypatch.setenv("ZOTERO_WORD_CITE_CONTACT_EMAIL", "lab@example.org")
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        raise RuntimeError("stop after URL capture")  # avoid reading a body

    monkeypatch.setattr(cc.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError):
        cc.refresh_retraction_db(dest="/tmp/does-not-matter.csv")
    assert captured["url"] == (
        "https://gitlab.com/crossref/retraction-watch-data/-/raw/main/retraction_watch.csv")
    assert "mailto=" not in captured["url"]


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


# ---------------------------------------------------------------------------
# CITE-DOI-MISMATCH + CITE-AUTHOR-MISMATCH
#
# DOI<->metadata identity and family-by-family author cross-check. A live DOI is
# NOT verification: the resolved title/authors must MATCH the cited ones. Both
# fetch Crossref-by-DOI, so they ride behind check_existence=True. All network
# is monkeypatched at refresolve._crossref_doi_fetch (the single Crossref-by-DOI
# path) — no real requests.
# ---------------------------------------------------------------------------

def _resolved(doi="", title="", authors=None, year=None, journal=None,
              item_type="journal-article"):
    """Build a refresolve._parse_crossref_item-shaped record (what
    _crossref_doi_fetch returns)."""
    return {
        "doi": doi or None,
        "title": title or None,
        "authors": authors or [],
        "year": year,
        "journal": journal,
        "type": item_type,
        "score": None,
        "preprint": False,
    }


def _au(*families):
    """CSL-JSON author list from family names (given derived from family)."""
    return [{"family": f, "given": f[:1]} for f in families]


class TestDoiMetadataIdentity:
    """Check A — DOI must resolve to the cited paper, not merely be live."""

    def _patch_fetch(self, monkeypatch, mapping):
        """Map normalised DOI -> resolved record."""
        import zoterocite.refresolve as rr
        from zoterocite.citecheck import _normalise_doi

        def fake(doi):
            return mapping.get(_normalise_doi(doi))

        monkeypatch.setattr(rr, "_crossref_doi_fetch", fake)

    def test_matching_title_and_authors_is_silent(self, tmp_path, monkeypatch):
        """(1) Resolved title+authors match the cited ones → no findings."""
        self._patch_fetch(monkeypatch, {
            "10.1/match": _resolved(
                doi="10.1/match",
                title="Lesion network mapping of focal brain lesions",
                authors=_au("Cohen", "Fox"),
            ),
        })
        out = _build_doc(
            tmp_path,
            ["Claim here."],
            [{
                "anchor": "Claim here",
                "keys": ["MATCH"],
                "dois": ["10.1/match"],
                "titles": ["Lesion network mapping of focal brain lesions"],
                "authors": [_au("Cohen", "Fox")],
                "add_bibliography": True,
            }],
        )
        findings = cite_check(out, check_existence=True,
                              rw_csv=Path("/nonexistent/rw.csv"))
        bad = [f for f in findings
               if f.check in ("CITE-DOI-MISMATCH", "CITE-AUTHOR-MISMATCH")]
        assert bad == [], bad

    def test_doi_resolves_to_different_paper_warns(self, tmp_path, monkeypatch):
        """(2) The DOI is live but resolves to a totally unrelated paper →
        CITE-DOI-MISMATCH WARN (DOI-misdirection)."""
        self._patch_fetch(monkeypatch, {
            "10.1/wrong": _resolved(
                doi="10.1/wrong",
                title="Photosynthesis in tropical orchids under drought",
                authors=_au("Mendez"),
            ),
        })
        out = _build_doc(
            tmp_path,
            ["Claim here."],
            [{
                "anchor": "Claim here",
                "keys": ["WRONG"],
                "dois": ["10.1/wrong"],
                "titles": ["Lesion network mapping of focal brain lesions"],
                "authors": [_au("Cohen")],
                "add_bibliography": True,
            }],
        )
        findings = cite_check(out, check_existence=True,
                              rw_csv=Path("/nonexistent/rw.csv"))
        mism = [f for f in findings if f.check == "CITE-DOI-MISMATCH"]
        assert len(mism) == 1
        assert mism[0].severity == "WARN"
        # Message names both titles + the DOI, and folds in the author mismatch.
        assert "10.1/wrong" in mism[0].message
        assert "Lesion network mapping" in mism[0].message
        assert "Photosynthesis in tropical orchids" in mism[0].message
        assert "First author also differs" in mism[0].message


class TestAuthorFamilyCrossCheck:
    """Check B — family-by-family author cross-check against Crossref-by-DOI."""

    def _patch_fetch(self, monkeypatch, mapping):
        import zoterocite.refresolve as rr
        from zoterocite.citecheck import _normalise_doi

        def fake(doi):
            return mapping.get(_normalise_doi(doi))

        monkeypatch.setattr(rr, "_crossref_doi_fetch", fake)

    def _doc(self, tmp_path, doi, title, cited_authors):
        return _build_doc(
            tmp_path,
            ["Claim here."],
            [{
                "anchor": "Claim here",
                "keys": ["K"],
                "dois": [doi],
                "titles": [title],
                "authors": [cited_authors],
                "add_bibliography": True,
            }],
        )

    def test_fabricated_coauthor_at_position_3_warns(self, tmp_path, monkeypatch):
        """(3) A cited co-author at position #3 differs from the source →
        CITE-AUTHOR-MISMATCH WARN."""
        title = "Network localization of neurological symptoms"
        self._patch_fetch(monkeypatch, {
            "10.2/paper": _resolved(
                doi="10.2/paper", title=title,
                authors=_au("Cohen", "Drysdale", "Fox", "Pascual-Leone"),
            ),
        })
        out = self._doc(
            tmp_path, "10.2/paper", title,
            # position #3 fabricated: "Ghost" replaces "Fox"
            _au("Cohen", "Drysdale", "Ghost", "Pascual-Leone"),
        )
        findings = cite_check(out, check_existence=True,
                              rw_csv=Path("/nonexistent/rw.csv"))
        mism = [f for f in findings if f.check == "CITE-AUTHOR-MISMATCH"]
        assert len(mism) == 1
        assert mism[0].severity == "WARN"
        assert "author #3" in mism[0].message
        assert "Ghost" in mism[0].message and "Fox" in mism[0].message

    def test_corporate_collective_author_is_silent(self, tmp_path, monkeypatch):
        """(4) A collective/corporate author is skipped from family comparison →
        no false CITE-AUTHOR-MISMATCH (the guard works)."""
        title = "Outcomes of a multicentre randomized trial"
        # Source has a collective byline at position #2; the cited record carries
        # the same collective. A naive surname compare would explode here.
        self._patch_fetch(monkeypatch, {
            "10.3/trial": _resolved(
                doi="10.3/trial", title=title,
                authors=[
                    {"family": "Cohen", "given": "A"},
                    {"family": "EASL Study Group", "given": ""},
                ],
            ),
        })
        out = self._doc(
            tmp_path, "10.3/trial", title,
            [
                {"family": "Cohen", "given": "A"},
                {"family": "EASL Study Group", "given": ""},
            ],
        )
        findings = cite_check(out, check_existence=True,
                              rw_csv=Path("/nonexistent/rw.csv"))
        mism = [f for f in findings if f.check == "CITE-AUTHOR-MISMATCH"]
        assert mism == [], mism

    def test_cited_fewer_authors_is_silent(self, tmp_path, monkeypatch):
        """(5) Cited FEWER authors than the source (CSL et-al truncation) →
        SILENT by default (no bare count-mismatch finding)."""
        title = "A long-author-list consortium paper"
        self._patch_fetch(monkeypatch, {
            "10.4/long": _resolved(
                doi="10.4/long", title=title,
                authors=_au("Cohen", "Drysdale", "Fox", "Pascual-Leone",
                            "Grafman", "Boes"),
            ),
        })
        # Cited only the first three (et-al truncation in the rendered cite).
        out = self._doc(
            tmp_path, "10.4/long", title,
            _au("Cohen", "Drysdale", "Fox"),
        )
        findings = cite_check(out, check_existence=True,
                              rw_csv=Path("/nonexistent/rw.csv"))
        mism = [f for f in findings if f.check == "CITE-AUTHOR-MISMATCH"]
        assert mism == [], mism

    def test_diacritic_fold_is_silent_teeth(self, tmp_path, monkeypatch):
        """(6) TEETH: cited 'Çolakoğlu' vs source 'Colakoglu' must be SILENT.

        Proves the normalizer earns its place — without NFKD/diacritic folding
        these would false-MISMATCH on a purely cosmetic Unicode difference.
        """
        title = "A neuroimaging study"
        self._patch_fetch(monkeypatch, {
            "10.5/dia": _resolved(
                doi="10.5/dia", title=title,
                authors=[{"family": "Colakoglu", "given": "M"}],  # ASCII source
            ),
        })
        out = self._doc(
            tmp_path, "10.5/dia", title,
            [{"family": "Çolakoğlu", "given": "M"}],  # Çolakoğlu cited
        )
        findings = cite_check(out, check_existence=True,
                              rw_csv=Path("/nonexistent/rw.csv"))
        mism = [f for f in findings if f.check == "CITE-AUTHOR-MISMATCH"]
        assert mism == [], mism

    def test_extra_cited_author_beyond_source_warns(self, tmp_path, monkeypatch):
        """A cited author BEYOND the source list length cannot be et-al
        truncation → CITE-AUTHOR-MISMATCH WARN."""
        title = "A two-author paper"
        self._patch_fetch(monkeypatch, {
            "10.6/two": _resolved(
                doi="10.6/two", title=title,
                authors=_au("Cohen", "Fox"),
            ),
        })
        out = self._doc(
            tmp_path, "10.6/two", title,
            _au("Cohen", "Fox", "Phantom"),  # 3rd has no counterpart
        )
        findings = cite_check(out, check_existence=True,
                              rw_csv=Path("/nonexistent/rw.csv"))
        mism = [f for f in findings if f.check == "CITE-AUTHOR-MISMATCH"]
        assert len(mism) == 1
        assert mism[0].severity == "WARN"
        assert "no counterpart" in mism[0].message


# ---------------------------------------------------------------------------
# CITE-AUTHOR-MISMATCH via PMID (PubMed authoritative source)
#
# Round-2 extension: when a cited ref has NO DOI (or Crossref-by-DOI returns no
# authors) but DOES carry a PMID, the family-by-family cross-check fetches
# STRUCTURED authors from PubMed (entrez.efetch_pubmed_authors — the single
# owner) and runs the SAME refresolve._author_family_compare.  All network is
# monkeypatched at entrez.efetch_pubmed_authors and refresolve._crossref_doi_fetch
# — no real requests.
# ---------------------------------------------------------------------------

class TestAuthorFamilyCrossCheckPubMed:
    """Check B, PMID branch — authoritative author list from PubMed."""

    def _patch_pubmed(self, monkeypatch, mapping):
        """Map PMID -> structured author list ([{family, given}])."""
        import zoterocite.entrez as ent

        def fake(pmids):
            return {p: mapping.get(str(p)) for p in pmids if str(p) in mapping}

        monkeypatch.setattr(ent, "efetch_pubmed_authors", fake)

    def _patch_crossref(self, monkeypatch, mapping):
        """Map normalised DOI -> resolved record (None when absent)."""
        import zoterocite.refresolve as rr
        from zoterocite.citecheck import _normalise_doi

        def fake(doi):
            return mapping.get(_normalise_doi(doi))

        monkeypatch.setattr(rr, "_crossref_doi_fetch", fake)

    def _pmid_doc(self, tmp_path, pmid, cited_authors, *, doi="", extra=""):
        return _build_doc(
            tmp_path,
            ["Claim here."],
            [{
                "anchor": "Claim here",
                "keys": ["K"],
                "dois": [doi],
                "pmids": [pmid] if not extra else [""],
                "extras": [extra],
                "titles": ["A PMID-only paper"],
                "authors": [cited_authors],
                "add_bibliography": True,
            }],
        )

    def test_pmid_only_fabricated_coauthor_warns(self, tmp_path, monkeypatch):
        """PMID-only ref (no DOI) with a fabricated co-author #3 vs the PubMed
        authoritative list → exactly one CITE-AUTHOR-MISMATCH WARN.

        This is also the TEETH test: it would only pass if the PMID branch is
        actually taken (no DOI is present, so the Crossref path cannot fire).
        """
        self._patch_pubmed(monkeypatch, {
            "26980150": _au("Cohen", "Drysdale", "Fox", "Pascual-Leone"),
        })
        out = self._pmid_doc(
            tmp_path, "26980150",
            # position #3 fabricated: "Ghost" replaces "Fox"
            _au("Cohen", "Drysdale", "Ghost", "Pascual-Leone"),
        )
        findings = cite_check(out, check_existence=True,
                              rw_csv=Path("/nonexistent/rw.csv"))
        mism = [f for f in findings if f.check == "CITE-AUTHOR-MISMATCH"]
        assert len(mism) == 1
        assert mism[0].severity == "WARN"
        assert "author #3" in mism[0].message
        assert "Ghost" in mism[0].message and "Fox" in mism[0].message
        # The message must name PubMed/PMID as the authoritative source.
        assert "PMID 26980150" in mism[0].message
        assert mism[0].source == "PubMed"

    def test_pmid_only_clean_is_silent(self, tmp_path, monkeypatch):
        """PMID-only ref whose cited authors match the PubMed record → silent."""
        self._patch_pubmed(monkeypatch, {
            "26980150": _au("Cohen", "Drysdale", "Fox"),
        })
        out = self._pmid_doc(
            tmp_path, "26980150",
            _au("Cohen", "Drysdale", "Fox"),
        )
        findings = cite_check(out, check_existence=True,
                              rw_csv=Path("/nonexistent/rw.csv"))
        mism = [f for f in findings if f.check == "CITE-AUTHOR-MISMATCH"]
        assert mism == [], mism

    def test_pmid_collective_author_is_silent(self, tmp_path, monkeypatch):
        """A PubMed CollectiveName author (flagged {family, given=''}) is skipped
        by the corporate guard → no false CITE-AUTHOR-MISMATCH."""
        # PubMed record: one named author + a collective at position #2, exactly
        # as entrez.efetch_pubmed_authors emits a <CollectiveName>.
        self._patch_pubmed(monkeypatch, {
            "99999999": [
                {"family": "Jones", "given": "BK"},
                {"family": "Brain Imaging Consortium", "given": ""},
            ],
        })
        out = self._pmid_doc(
            tmp_path, "99999999",
            [
                {"family": "Jones", "given": "BK"},
                {"family": "Brain Imaging Consortium", "given": ""},
            ],
        )
        findings = cite_check(out, check_existence=True,
                              rw_csv=Path("/nonexistent/rw.csv"))
        mism = [f for f in findings if f.check == "CITE-AUTHOR-MISMATCH"]
        assert mism == [], mism

    def test_doi_present_does_not_consult_pmid(self, tmp_path, monkeypatch):
        """A ref WITH a DOI uses the Crossref path; PubMed is NOT consulted when
        Crossref returns authors (existing behaviour unchanged)."""
        title = "A PMID-only paper"
        # Crossref returns a clean matching author list for the DOI.
        self._patch_crossref(monkeypatch, {
            "10.7/has-doi": _resolved(
                doi="10.7/has-doi", title=title,
                authors=_au("Cohen", "Fox"),
            ),
        })
        # PubMed, if (wrongly) consulted, would report a DIFFERENT list that
        # would trip a mismatch — so a clean result proves PubMed was skipped.
        pubmed_calls = []

        def fake_pubmed(pmids):
            pubmed_calls.append(list(pmids))
            return {"26980150": _au("Ghost", "Phantom")}

        import zoterocite.entrez as ent
        monkeypatch.setattr(ent, "efetch_pubmed_authors", fake_pubmed)

        out = self._pmid_doc(
            tmp_path, "26980150",
            _au("Cohen", "Fox"),
            doi="10.7/has-doi",
        )
        findings = cite_check(out, check_existence=True,
                              rw_csv=Path("/nonexistent/rw.csv"))
        mism = [f for f in findings if f.check == "CITE-AUTHOR-MISMATCH"]
        assert mism == [], mism
        # PubMed must not have been consulted at all.
        assert pubmed_calls == [], pubmed_calls

    def test_doi_crossref_empty_falls_back_to_pmid(self, tmp_path, monkeypatch):
        """DOI present but Crossref returns NO authors → fall back to PMID, and
        a fabricated co-author there still trips a WARN naming PubMed."""
        title = "A PMID-only paper"
        # Crossref resolves but with an empty author list.
        self._patch_crossref(monkeypatch, {
            "10.8/no-authors": _resolved(
                doi="10.8/no-authors", title=title, authors=[],
            ),
        })
        self._patch_pubmed(monkeypatch, {
            "26980150": _au("Cohen", "Fox"),
        })
        out = self._pmid_doc(
            tmp_path, "26980150",
            _au("Cohen", "Ghost"),  # #2 fabricated
            doi="10.8/no-authors",
        )
        findings = cite_check(out, check_existence=True,
                              rw_csv=Path("/nonexistent/rw.csv"))
        mism = [f for f in findings if f.check == "CITE-AUTHOR-MISMATCH"]
        assert len(mism) == 1
        assert "PMID 26980150" in mism[0].message
        assert mism[0].source == "PubMed"

    def test_pmid_from_extra_field(self, tmp_path, monkeypatch):
        """A PMID carried in the Zotero 'extra' field as 'PMID: 12345678' is
        parsed and drives the PubMed cross-check."""
        self._patch_pubmed(monkeypatch, {
            "26980150": _au("Cohen", "Fox"),
        })
        out = self._pmid_doc(
            tmp_path, "",  # no first-class PMID field
            _au("Cohen", "Ghost"),  # #2 fabricated
            extra="DOI: none\nPMID: 26980150\nsome other note",
        )
        findings = cite_check(out, check_existence=True,
                              rw_csv=Path("/nonexistent/rw.csv"))
        mism = [f for f in findings if f.check == "CITE-AUTHOR-MISMATCH"]
        assert len(mism) == 1
        assert "PMID 26980150" in mism[0].message


# ---------------------------------------------------------------------------
# _extract_pmid unit coverage (field + extra/note conventions)
# ---------------------------------------------------------------------------

class TestExtractPmid:
    def test_first_class_pmid_field(self):
        from zoterocite.citecheck import _extract_pmid
        assert _extract_pmid({"PMID": "12345678"}) == "12345678"

    def test_pmid_field_numeric(self):
        from zoterocite.citecheck import _extract_pmid
        assert _extract_pmid({"PMID": 12345678}) == "12345678"

    def test_pmid_from_extra_labelled(self):
        from zoterocite.citecheck import _extract_pmid
        assert _extract_pmid({"extra": "PMID: 26980150"}) == "26980150"

    def test_pmid_from_extra_no_space(self):
        from zoterocite.citecheck import _extract_pmid
        assert _extract_pmid({"extra": "PMID:26980150"}) == "26980150"

    def test_pmid_from_note_among_other_lines(self):
        from zoterocite.citecheck import _extract_pmid
        blob = "DOI: 10.1/x\nPMID: 30000001\nPMCID: PMC123"
        assert _extract_pmid({"note": blob}) == "30000001"

    def test_first_class_field_beats_extra(self):
        from zoterocite.citecheck import _extract_pmid
        assert _extract_pmid({"PMID": "111", "extra": "PMID: 222"}) == "111"

    def test_no_pmid_returns_empty(self):
        from zoterocite.citecheck import _extract_pmid
        assert _extract_pmid({"extra": "DOI: 10.1/x"}) == ""
        assert _extract_pmid({}) == ""


# ---------------------------------------------------------------------------
# load_retraction_map / is_retraction — the single-owner consolidation API.
# These are the primitives litsearch / endnote / unify / biocheck / biotailor
# now route their DB-load + per-DOI verdict through, instead of re-wrapping
# ensure_retraction_db + load_retraction_db each.
# ---------------------------------------------------------------------------

class TestIsRetraction:
    """is_retraction is THE per-DOI verdict; mirror check_retractions exactly."""

    DB = {
        "10.1234/retracted": {"nature": "Retraction"},
        "10.5678/concern": {"nature": "Expression of concern"},
        "10.9/correction": {"nature": "Correction"},
        "10.3/reinstated": {"nature": "Reinstatement"},
    }

    def test_retracted_doi_is_flagged(self):
        from zoterocite.citecheck import is_retraction
        # TEETH: a genuine retraction must be caught.
        assert is_retraction("10.1234/retracted", self.DB) is True

    def test_clean_doi_is_false(self):
        from zoterocite.citecheck import is_retraction
        assert is_retraction("10.0/never-heard-of-it", self.DB) is False

    def test_concern_correction_reinstatement_are_not_retractions(self):
        from zoterocite.citecheck import is_retraction
        # Only "retraction" is a hard retraction (the ERROR predicate); the rest
        # are advisory and must NOT be excluded.
        assert is_retraction("10.5678/concern", self.DB) is False
        assert is_retraction("10.9/correction", self.DB) is False
        assert is_retraction("10.3/reinstated", self.DB) is False

    def test_predicate_matches_check_retractions_exactly(self):
        """is_retraction must agree with check_retractions' ERROR verdict for the
        SAME DOI/db — proving the two share one predicate (mutation guard: if the
        predicate were flipped/loosened, these would diverge)."""
        from zoterocite.citecheck import is_retraction, check_retractions
        for doi, rec in self.DB.items():
            findings = check_retractions([doi], {doi: rec})
            is_error = any(f.severity == "ERROR" for f in findings)
            assert is_retraction(doi, {doi: rec}) is is_error, doi

    def test_nature_is_case_and_whitespace_insensitive(self):
        from zoterocite.citecheck import is_retraction
        db = {"10.1/x": {"nature": "  RETRACTION  "}}
        assert is_retraction("10.1/x", db) is True

    def test_normalises_doi_before_lookup(self):
        """A DOI with a URL prefix / trailing punctuation still matches (same
        _normalise_doi the DB is keyed with)."""
        from zoterocite.citecheck import is_retraction
        assert is_retraction("https://doi.org/10.1234/RETRACTED.", self.DB) is True

    def test_empty_doi_or_db_is_false(self):
        from zoterocite.citecheck import is_retraction
        assert is_retraction("", self.DB) is False
        assert is_retraction(None, self.DB) is False  # type: ignore[arg-type]
        assert is_retraction("10.1234/retracted", {}) is False


class TestLoadRetractionMap:
    """load_retraction_map is THE DB-load entrypoint with a per-caller network
    policy.  allow_network=False must NEVER touch the network (biocheck policy);
    allow_network=True routes through ensure_retraction_db (endnote/unify policy)."""

    def _seed_cache(self, tmp_path, monkeypatch):
        import zoterocite.citecheck as cc
        csv_path = tmp_path / "rw.csv"
        _write_rw_csv(csv_path, [
            {"OriginalPaperDOI": "10.1/retracted", "RetractionNature": "Retraction",
             "Title": "A retracted paper"},
            {"OriginalPaperDOI": "10.2/concern", "RetractionNature": "Expression of concern",
             "Title": "A concerning paper"},
        ])
        # Point default_rw_path at our seeded cache so the no-network branch finds it.
        monkeypatch.setattr(cc, "default_rw_path", lambda: csv_path)
        return cc, csv_path

    def test_no_network_loads_cached_map_without_touching_network(self, tmp_path, monkeypatch):
        cc, _ = self._seed_cache(tmp_path, monkeypatch)
        # REFUSE the network entirely: ensure_retraction_db AND refresh both blow up.
        def _refuse(*a, **k):
            raise AssertionError("allow_network=False must NOT hit the network")
        monkeypatch.setattr(cc, "ensure_retraction_db", _refuse)
        monkeypatch.setattr(cc, "refresh_retraction_db", _refuse)

        db = cc.load_retraction_map(allow_network=False)

        assert db["10.1/retracted"]["nature"] == "Retraction"
        # And the verdict gates correctly off this map.
        assert cc.is_retraction("10.1/retracted", db) is True
        assert cc.is_retraction("10.2/concern", db) is False

    def test_no_network_missing_cache_is_empty_no_network(self, tmp_path, monkeypatch):
        import zoterocite.citecheck as cc
        missing = tmp_path / "does-not-exist.csv"
        monkeypatch.setattr(cc, "default_rw_path", lambda: missing)
        def _refuse(*a, **k):
            raise AssertionError("allow_network=False must NOT hit the network")
        monkeypatch.setattr(cc, "ensure_retraction_db", _refuse)
        assert cc.load_retraction_map(allow_network=False) == {}

    def test_network_branch_routes_through_ensure(self, tmp_path, monkeypatch):
        cc, csv_path = self._seed_cache(tmp_path, monkeypatch)
        calls = {"ensure": 0}
        def _spy_ensure(*a, **k):
            calls["ensure"] += 1
            return csv_path, None
        monkeypatch.setattr(cc, "ensure_retraction_db", _spy_ensure)

        db = cc.load_retraction_map(allow_network=True)

        assert calls["ensure"] == 1            # the network path IS consulted
        assert db["10.1/retracted"]["nature"] == "Retraction"

    def test_network_branch_degrades_to_empty_when_unavailable(self, monkeypatch):
        import zoterocite.citecheck as cc
        monkeypatch.setattr(cc, "ensure_retraction_db", lambda *a, **k: (None, None))
        assert cc.load_retraction_map(allow_network=True) == {}

    def test_never_raises_on_internal_failure(self, monkeypatch):
        import zoterocite.citecheck as cc
        def _boom(*a, **k):
            raise RuntimeError("kaboom")
        monkeypatch.setattr(cc, "ensure_retraction_db", _boom)
        # Must degrade to {}, never propagate.
        assert cc.load_retraction_map(allow_network=True) == {}


class TestRetractionNetworkPolicyPreserved:
    """PRESERVATION guard for the network-policy divergences the consolidation
    must keep: endnote/unify ALWAYS allow a network refresh.  Spy on
    ensure_retraction_db at the citecheck seam.

    (grant-forge also pins the biocheck "cached-only, no auto-refresh" divergence
    here; biocheck is a grant-only biosketch module absent from the public
    citation engine, so only the shared endnote/unify consumers are exercised.)"""

    def test_endnote_allows_refresh(self, monkeypatch):
        import zoterocite.citecheck as cc
        import zoterocite.endnote as endnote
        calls = {"ensure": 0}
        monkeypatch.setattr(
            cc, "ensure_retraction_db",
            lambda *a, **k: (calls.__setitem__("ensure", calls["ensure"] + 1) or (None, None)),
        )
        endnote._load_retraction_db()
        assert calls["ensure"] == 1   # endnote DID consult the auto-refresh path

    def test_unify_allows_refresh(self, monkeypatch):
        import zoterocite.citecheck as cc
        calls = {"ensure": 0}
        monkeypatch.setattr(
            cc, "ensure_retraction_db",
            lambda *a, **k: (calls.__setitem__("ensure", calls["ensure"] + 1) or (None, None)),
        )
        # unify loads the db via citecheck.load_retraction_map(allow_network=True)
        # inside plan_unification; exercise that path directly through the owner.
        cc.load_retraction_map(allow_network=True)
        assert calls["ensure"] == 1
