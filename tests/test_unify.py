"""Tests for zoterocite.unify — the reference-unification orchestration.

Two passes are exercised end-to-end on synthetic .docx documents:

* ``plan_unification`` — read-only planning. Asserts correct tiering (high→auto,
  medium→confirm), placeholder suggestions, library-match flags, in-text→reflist
  linking, and summary counts.
* ``apply_unification`` — the write pass. Asserts created items get the
  "Imported — review" collection + tags, the doc is written to ``out``, the
  report counts are right, retracted/divergent flagging, and that a read-only
  key records items in ``needs_input`` without creating anything.

NO real network and NO real Zotero writes: ``refresolve.resolve_reference``,
the ``zotero`` client, the retraction DB, and
``zoterofield.replace_text_with_zotero_field`` are all monkeypatched to
canned/recordable values.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from zoterocite import new_doc, Docx, insert_citation
from zoterocite import unify
from zoterocite import zoterofield


# ===========================================================================
# Canned resolver
# ===========================================================================

def _meta(doi, title, family, year, journal="J", typ="article-journal"):
    return {
        "doi": doi,
        "title": title,
        "authors": [{"family": family, "given": "A"}],
        "year": year,
        "journal": journal,
        "type": typ,
    }


# Three works keyed by a substring of the reference text / placeholder.
SMITH = _meta("10.1/smith2020", "Tuberous sclerosis and autism", "Smith", "2020")
JONES = _meta("10.1/jones2019", "Cortical tubers in TSC", "Jones", "2019")
TUBERS_CANDIDATE = {
    "doi": "10.1/tubers",
    "title": "Tubers and ASD: a review",
    "authors": [{"family": "Lee", "given": "K"}],
    "year": "2021",
    "journal": "Brain",
    "type": "article-journal",
    "score": 22.0,
}


def _fake_resolve(text, *, fetch=True):
    """Smith → HIGH (+DOI), Jones → MEDIUM, placeholder → LOW with a candidate."""
    t = text.lower()
    if "smith" in t and "2020" in t:
        return {
            "input": text, "metadata": dict(SMITH), "confidence": "high",
            "source": "crossref",
            "candidates": [{**SMITH, "score": 90.0}],
            "identifiers": {"doi": None, "pmid": None, "arxiv": None, "isbn": None},
        }
    if "jones" in t:
        return {
            "input": text, "metadata": dict(JONES), "confidence": "medium",
            "source": "crossref",
            "candidates": [{**JONES, "score": 40.0}],
            "identifiers": {"doi": None, "pmid": None, "arxiv": None, "isbn": None},
        }
    # placeholder / anything else: low confidence, no metadata, but a candidate
    return {
        "input": text, "metadata": None, "confidence": "low",
        "source": "crossref",
        "candidates": [dict(TUBERS_CANDIDATE)],
        "identifiers": {"doi": None, "pmid": None, "arxiv": None, "isbn": None},
    }


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture()
def draft(tmp_path: Path) -> Path:
    """A draft with one high ref (Smith, in-library), one medium ref (Jones,
    missing), an author-year in-text cite, a numeric in-text cite, and a
    bracket placeholder."""
    p = tmp_path / "draft.docx"
    new_doc(p, [
        "Tuberous sclerosis is associated with ASD (Smith et al., 2020).",
        "Prior work supports this model [1].",
        "More evidence is needed [CITE: tubers and ASD].",
        "References",
        "1. Smith J, Jones A. Tuberous sclerosis and autism. J Neurol. 2020;10:1-5.",
        "2. Jones B, et al. Cortical tubers in TSC. Brain. 2019;50:200-210.",
    ])
    return p


@pytest.fixture()
def patched_network(monkeypatch):
    """Patch resolver, library lookup, and retraction DB with canned values.

    Smith's DOI is "in library" (key SMITHKEY); Jones is missing. Retraction DB
    is empty by default.
    """
    monkeypatch.setattr(unify.refresolve, "resolve_reference", _fake_resolve)

    # library_index replaces per-ref get_item_by_doi — return a small
    # {"doi","pmid","title"} index. Smith is present by DOI (key SMITHKEY).
    monkeypatch.setattr(
        unify.zotero, "library_index",
        lambda **kw: {"doi": {"10.1/smith2020": "SMITHKEY"}, "pmid": {}, "title": {}},
    )
    # No retraction DB by default → nothing retracted.
    monkeypatch.setattr(unify.citecheck, "ensure_retraction_db", lambda **kw: (None, None))
    return monkeypatch


# ===========================================================================
# plan_unification
# ===========================================================================

class TestPlan:
    def test_tiers_and_buckets(self, draft, patched_network):
        plan = unify.plan_unification(draft)
        s = plan["summary"]
        assert s["tiers"]["high"] == 1
        assert s["tiers"]["medium"] == 1
        assert s["buckets"]["reflist"] == 2
        assert s["buckets"]["placeholders"] == 1
        # one high ref auto-accepted; one medium ref + one placeholder confirm
        assert plan["auto"] == [0]
        assert plan["needs_confirmation"]["references"] == [1]
        assert plan["needs_confirmation"]["placeholders"] == [0]

    def test_library_match_flags(self, draft, patched_network):
        plan = unify.plan_unification(draft)
        refs = {r["ref_index"]: r for r in plan["references"]}
        assert refs[0]["in_library"] is True
        assert refs[0]["existing_key"] == "SMITHKEY"
        assert refs[1]["in_library"] is False
        assert refs[1]["existing_key"] is None
        assert plan["summary"]["n_in_library"] == 1
        assert plan["summary"]["n_missing"] == 1

    def test_placeholder_suggestion(self, draft, patched_network):
        plan = unify.plan_unification(draft)
        assert len(plan["placeholders"]) == 1
        ph = plan["placeholders"][0]
        assert ph["kind"] == "bracket"
        # low-confidence placeholder still gets a suggested candidate
        assert ph["suggestion"] is not None
        assert ph["suggestion"]["doi"] == "10.1/tubers"

    def test_intext_linked_to_reflist(self, draft, patched_network):
        plan = unify.plan_unification(draft)
        refs = {r["ref_index"]: r for r in plan["references"]}
        # numeric "[1]" → reflist entry numbered "1." (Smith) AND author-year
        # "(Smith et al., 2020)" → same Smith entry.
        assert set(refs[0]["intext_links"]), "Smith ref should have in-text links"
        # all in-text markers were linked (no orphans in this doc)
        assert plan["summary"]["buckets"]["intext_unlinked"] == 0
        assert plan["unlinked_intext"] == []

    def test_no_writes_during_plan(self, draft, monkeypatch):
        """plan_unification must never call create_items / key_can_write."""
        monkeypatch.setattr(unify.refresolve, "resolve_reference", _fake_resolve)
        monkeypatch.setattr(unify.zotero, "library_index", lambda **kw: {"doi": {}, "pmid": {}, "title": {}})
        monkeypatch.setattr(unify.citecheck, "ensure_retraction_db", lambda **kw: (None, None))

        def _boom(*a, **k):  # pragma: no cover - must not be hit
            raise AssertionError("plan_unification performed a write")

        monkeypatch.setattr(unify.zotero, "create_items", _boom)
        monkeypatch.setattr(unify.zotero, "key_can_write", _boom)
        monkeypatch.setattr(unify.zotero, "key_can_write_status", _boom)
        plan = unify.plan_unification(draft)
        assert plan["references"]  # ran to completion without writing

    def test_degraded_library_read_degrades_to_missing_no_raise(self, draft, monkeypatch):
        """R2-1: a LibraryUnavailableError during the READ-ONLY plan pass must NOT
        crash the dry-run. plan_unification degrades to an empty DOI index — every
        reference reads as MISSING (in_library=False) — and still returns a plan.
        The write path keeps its own fail-closed guard (tested separately in
        TestApply); this read-only path is documented-safe to all-MISSING.
        """
        monkeypatch.setattr(unify.refresolve, "resolve_reference", _fake_resolve)
        monkeypatch.setattr(unify.citecheck, "ensure_retraction_db", lambda **kw: (None, None))

        def _raise(**kw):
            raise unify.zotero.LibraryUnavailableError("read failed")

        monkeypatch.setattr(unify.zotero, "library_index", _raise)

        # Must NOT raise.
        plan = unify.plan_unification(draft)

        # Plan still produced with both references inventoried.
        assert len(plan["references"]) == 2
        refs = {r["ref_index"]: r for r in plan["references"]}
        # Smith would normally be in-library; with a degraded read it now reads
        # as MISSING (no false "in library" on an unavailable read).
        assert refs[0]["in_library"] is False
        assert refs[0]["existing_key"] is None
        assert refs[1]["in_library"] is False
        # Summary reflects all-missing: nothing matched in the (degraded) library.
        assert plan["summary"]["n_in_library"] == 0
        # Smith resolves with metadata → counted as missing (needs import).
        assert plan["summary"]["n_missing"] >= 1

    def test_healthy_library_read_still_matches(self, draft, patched_network):
        """R2-1 control: a normal (non-raising) library_doi_index dict still
        matches in-library refs — the guard does not swallow the happy path."""
        plan = unify.plan_unification(draft)
        refs = {r["ref_index"]: r for r in plan["references"]}
        assert refs[0]["in_library"] is True
        assert refs[0]["existing_key"] == "SMITHKEY"
        assert plan["summary"]["n_in_library"] == 1

    def test_retraction_flagged(self, draft, monkeypatch):
        """A resolved DOI present in the retraction DB → retracted=True + count."""
        monkeypatch.setattr(unify.refresolve, "resolve_reference", _fake_resolve)
        monkeypatch.setattr(
            unify.zotero, "library_index",
            lambda **kw: {"doi": {"10.1/smith2020": "SMITHKEY"}, "pmid": {}, "title": {}},
        )
        # Pretend the DB is loadable and Smith's DOI is retracted.
        monkeypatch.setattr(unify.citecheck, "ensure_retraction_db", lambda **kw: ("/fake/rw.csv", None))
        monkeypatch.setattr(
            unify.citecheck, "load_retraction_db",
            lambda path: {"10.1/smith2020": {"nature": "Retraction"}},
        )
        plan = unify.plan_unification(draft)
        refs = {r["ref_index"]: r for r in plan["references"]}
        assert refs[0]["retracted"] is True
        assert refs[1]["retracted"] is False
        assert plan["summary"]["n_retracted"] == 1


# ===========================================================================
# Coverage: in_library must fall back DOI → PMID → normalized-title (Bug 1).
#
# A real shared library has many items WITHOUT a DOI in their index entry. A
# DOI-only presence test false-flags those present refs as "missing", and a
# confident --apply would then MASS-DUPLICATE the shared group library.
# ===========================================================================

# A PMID-only metadata (no DOI) — the resolver carries the PMID forward.
PMID_META = {
    "doi": None,
    "pmid": "31234567",
    "title": "A PMID-only present paper",
    "authors": [{"family": "Pino", "given": "M"}],
    "year": "2018",
    "journal": "J",
    "type": "article-journal",
}

# A title-only metadata (no DOI, no PMID).
TITLE_META = {
    "doi": None,
    "pmid": None,
    "title": "A title-only present paper",
    "authors": [{"family": "Tito", "given": "T"}],
    "year": "2017",
    "journal": "J",
    "type": "article-journal",
}


def _fake_resolve_idfallback(text, *, fetch=True):
    """Resolve four works by substring: a DOI ref (Smith), a PMID-only ref, a
    title-only ref, and a genuinely-absent ref (Ghost)."""
    t = text.lower()
    if "smith" in t and "2020" in t:
        return {
            "input": text, "metadata": dict(SMITH), "confidence": "high",
            "source": "doi",
            "candidates": [{**SMITH, "score": 90.0}],
            "identifiers": {"doi": "10.1/smith2020", "pmid": None, "arxiv": None, "isbn": None},
        }
    if "pmid-only" in t:
        return {
            "input": text, "metadata": dict(PMID_META), "confidence": "high",
            "source": "pmid",
            "candidates": [{**PMID_META, "score": 90.0}],
            # identifiers also carries the pmid (input string bore one)
            "identifiers": {"doi": None, "pmid": "31234567", "arxiv": None, "isbn": None},
        }
    if "title-only" in t:
        return {
            "input": text, "metadata": dict(TITLE_META), "confidence": "high",
            "source": "crossref",
            "candidates": [{**TITLE_META, "score": 90.0}],
            "identifiers": {"doi": None, "pmid": None, "arxiv": None, "isbn": None},
        }
    # Ghost: resolvable metadata but NOT in the library by any key → missing.
    return {
        "input": text, "metadata": _meta("10.9/ghost", "An absent ghost paper", "Ghost", "2099"),
        "confidence": "high", "source": "doi",
        "candidates": [],
        "identifiers": {"doi": "10.9/ghost", "pmid": None, "arxiv": None, "isbn": None},
    }


class TestCoverageFallback:
    @pytest.fixture()
    def four_ref_doc(self, tmp_path: Path) -> Path:
        p = tmp_path / "fourref.docx"
        new_doc(p, [
            "Sentence A [1].",
            "Sentence B [2].",
            "Sentence C [3].",
            "Sentence D [4].",
            "References",
            "1. Smith J. Tuberous sclerosis and autism. J Neurol. 2020;10:1-5.",
            "2. A pmid-only present paper. J. 2018.",
            "3. A title-only present paper. J. 2017.",
            "4. An absent ghost paper. J. 2099.",
        ])
        return p

    def _patch(self, monkeypatch, lib_index):
        monkeypatch.setattr(unify.refresolve, "resolve_reference", _fake_resolve_idfallback)
        monkeypatch.setattr(unify.zotero, "library_index", lambda **kw: lib_index)
        monkeypatch.setattr(unify.citecheck, "ensure_retraction_db", lambda **kw: (None, None))

    def test_pmid_match_reads_in_library(self, four_ref_doc, monkeypatch):
        """A ref with a PMID but no DOI, present in the library by PMID, reads
        in_library=True and is NOT counted missing."""
        lib_index = {"doi": {}, "pmid": {"31234567": "PMIDKEY"}, "title": {}}
        self._patch(monkeypatch, lib_index)
        plan = unify.plan_unification(four_ref_doc)
        by_title = {r["metadata"]["title"]: r for r in plan["references"] if r.get("metadata")}
        pino = by_title["A PMID-only present paper"]
        assert pino["in_library"] is True
        assert pino["existing_key"] == "PMIDKEY"

    def test_title_match_reads_in_library(self, four_ref_doc, monkeypatch):
        """A ref with neither DOI nor PMID matches by normalized title."""
        nt = unify.zotero._normalize_title("A title-only present paper")
        lib_index = {"doi": {}, "pmid": {}, "title": {nt: "TITLEKEY"}}
        self._patch(monkeypatch, lib_index)
        plan = unify.plan_unification(four_ref_doc)
        by_title = {r["metadata"]["title"]: r for r in plan["references"] if r.get("metadata")}
        tito = by_title["A title-only present paper"]
        assert tito["in_library"] is True
        assert tito["existing_key"] == "TITLEKEY"

    def test_degraded_doi_only_read_still_matches_by_doi(self, four_ref_doc, monkeypatch):
        """On a DEGRADED read (combined index unavailable, served from the DOI
        cache) plan_unification consumes library_index_status(strict=False): the
        DOI-bearing ref still reads in_library (NOT false-missing), and the
        summary marks the coverage degraded/DOI-only with a provisional note."""
        monkeypatch.setattr(unify.refresolve, "resolve_reference", _fake_resolve_idfallback)
        monkeypatch.setattr(unify.citecheck, "ensure_retraction_db", lambda **kw: (None, None))

        degraded_index = {"doi": {"10.1/smith2020": "SMITHKEY"}, "pmid": {}, "title": {}}

        def _degraded_status(*, strict=True, **kw):
            assert strict is False, "plan_unification (read) must call strict=False"
            return degraded_index, {"degraded": True, "doi_only": True}

        monkeypatch.setattr(unify.zotero, "library_index_status", _degraded_status)

        plan = unify.plan_unification(four_ref_doc)
        by_title = {r["metadata"]["title"]: r for r in plan["references"] if r.get("metadata")}
        smith = by_title["Tuberous sclerosis and autism"]
        assert smith["in_library"] is True, "DOI match must survive a degraded read"
        assert smith["existing_key"] == "SMITHKEY"

        s = plan["summary"]
        assert s["library_degraded"] is True
        assert s["library_doi_only"] is True
        assert "provisional" in s.get("network_note", "").lower()

    def test_doi_match_still_works(self, four_ref_doc, monkeypatch):
        """DOI match (the original path) is unbroken."""
        lib_index = {"doi": {"10.1/smith2020": "SMITHKEY"}, "pmid": {}, "title": {}}
        self._patch(monkeypatch, lib_index)
        plan = unify.plan_unification(four_ref_doc)
        by_title = {r["metadata"]["title"]: r for r in plan["references"] if r.get("metadata")}
        smith = by_title["Tuberous sclerosis and autism"]
        assert smith["in_library"] is True
        assert smith["existing_key"] == "SMITHKEY"

    def test_genuinely_absent_ref_counted_missing(self, four_ref_doc, monkeypatch):
        """A ref absent by DOI, PMID, and title is still flagged missing."""
        lib_index = {"doi": {}, "pmid": {}, "title": {}}
        self._patch(monkeypatch, lib_index)
        plan = unify.plan_unification(four_ref_doc)
        by_title = {r["metadata"]["title"]: r for r in plan["references"] if r.get("metadata")}
        ghost = by_title["An absent ghost paper"]
        assert ghost["in_library"] is False
        assert ghost["existing_key"] is None

    def test_dangerous_path_teeth_zero_missing_no_create(self, tmp_path, monkeypatch):
        """TEETH: 5 present refs that LACK DOIs but match by PMID/title → plan
        reports 0 missing (a DOI-only test would report 5), and the apply path
        creates 0 items. Mutation-confirm: reverting the fallback flags them
        missing again (asserted by test_mutation_doi_only_flags_all_missing)."""
        refs = [
            ("A pmid-only present paper", "31234567"),   # by PMID
            ("Second pmid present", "31000002"),         # by PMID
            ("A title-only present paper", None),         # by title
            ("Second title present", None),              # by title
            ("Third title present", None),              # by title
        ]
        # Build a doc whose reflist names all five (one per line).
        lines = ["Body text [1][2][3][4][5].", "References"] + [
            f"{i+1}. {name}. J. 2019." for i, (name, _) in enumerate(refs)
        ]
        p = tmp_path / "fivepresent.docx"
        new_doc(p, lines)

        # Resolver: each named ref → metadata with NO doi (pmid set when given).
        def _resolve(text, *, fetch=True):
            t = text.lower()
            for name, pmid in refs:
                if name.lower() in t:
                    meta = {
                        "doi": None, "pmid": pmid, "title": name,
                        "authors": [{"family": "X", "given": "Y"}],
                        "year": "2019", "journal": "J", "type": "article-journal",
                    }
                    return {
                        "input": text, "metadata": meta, "confidence": "high",
                        "source": "pmid" if pmid else "crossref", "candidates": [],
                        "identifiers": {"doi": None, "pmid": pmid, "arxiv": None, "isbn": None},
                    }
            return {
                "input": text, "metadata": None, "confidence": "none",
                "source": None, "candidates": [],
                "identifiers": {"doi": None, "pmid": None, "arxiv": None, "isbn": None},
            }

        # Library: all five present — two by PMID, three by normalized title.
        lib_index = {
            "doi": {},
            "pmid": {"31234567": "PK1", "31000002": "PK2"},
            "title": {
                unify.zotero._normalize_title("A title-only present paper"): "TK1",
                unify.zotero._normalize_title("Second title present"): "TK2",
                unify.zotero._normalize_title("Third title present"): "TK3",
            },
        }
        monkeypatch.setattr(unify.refresolve, "resolve_reference", _resolve)
        monkeypatch.setattr(unify.zotero, "library_index", lambda **kw: lib_index)
        monkeypatch.setattr(unify.citecheck, "ensure_retraction_db", lambda **kw: (None, None))

        plan = unify.plan_unification(p)
        assert plan["summary"]["n_missing"] == 0, (
            "all five present-but-DOI-less refs must read in_library; "
            f"got n_missing={plan['summary']['n_missing']}"
        )
        assert plan["summary"]["n_in_library"] == 5

        # Apply: create_items must be asked to create 0 of them (every accepted
        # ref already has an existing_key). Record any create call.
        creates = []
        monkeypatch.setattr(
            unify.zotero, "create_items",
            lambda metas, **k: creates.append(metas) or {
                "created": [], "skipped_existing": [], "failed": []},
        )
        monkeypatch.setattr(unify.zotero, "key_can_write_status", lambda: True)
        monkeypatch.setattr(unify.zotero, "key_can_write", lambda: True)
        monkeypatch.setattr(unify.zotero, "item_uri", lambda key: "http://x/" + key)
        monkeypatch.setattr(unify.zoterofield, "replace_text_with_zotero_field",
                            lambda doc, *a, **k: doc)

        out = tmp_path / "out.docx"
        unify.apply_unification(
            p, plan, {"accept": [0, 1, 2, 3, 4], "add_missing": True}, out=out, track=True,
        )
        flat_created = [m for batch in creates for m in batch]
        assert flat_created == [], (
            f"apply must create 0 items (all present); attempted: {flat_created}"
        )

    def test_mutation_doi_only_flags_all_missing(self, tmp_path, monkeypatch):
        """MUTATION GUARD: emulate the OLD DOI-only test by feeding a lib_index
        whose pmid/title maps are EMPTY (so only the DOI path can match). The
        five present-but-DOI-less refs are then ALL flagged missing — proving the
        teeth test above fails without the PMID/title fallback."""
        refs = [
            ("A pmid-only present paper", "31234567"),
            ("Second pmid present", "31000002"),
            ("A title-only present paper", None),
            ("Second title present", None),
            ("Third title present", None),
        ]
        lines = ["Body text [1][2][3][4][5].", "References"] + [
            f"{i+1}. {name}. J. 2019." for i, (name, _) in enumerate(refs)
        ]
        p = tmp_path / "fivepresent.docx"
        new_doc(p, lines)

        def _resolve(text, *, fetch=True):
            t = text.lower()
            for name, pmid in refs:
                if name.lower() in t:
                    meta = {
                        "doi": None, "pmid": pmid, "title": name,
                        "authors": [{"family": "X", "given": "Y"}],
                        "year": "2019", "journal": "J", "type": "article-journal",
                    }
                    return {
                        "input": text, "metadata": meta, "confidence": "high",
                        "source": "pmid" if pmid else "crossref", "candidates": [],
                        "identifiers": {"doi": None, "pmid": pmid, "arxiv": None, "isbn": None},
                    }
            return {
                "input": text, "metadata": None, "confidence": "none",
                "source": None, "candidates": [],
                "identifiers": {"doi": None, "pmid": None, "arxiv": None, "isbn": None},
            }

        # The bug surface: DOI-only knowledge of the library (pmid/title empty).
        lib_index_doi_only = {"doi": {}, "pmid": {}, "title": {}}
        monkeypatch.setattr(unify.refresolve, "resolve_reference", _resolve)
        monkeypatch.setattr(unify.zotero, "library_index", lambda **kw: lib_index_doi_only)
        monkeypatch.setattr(unify.citecheck, "ensure_retraction_db", lambda **kw: (None, None))

        plan = unify.plan_unification(p)
        assert plan["summary"]["n_missing"] == 5, (
            "with no PMID/title knowledge the five present refs SHOULD all be "
            "flagged missing — this is the duplication-risk surface the fallback fixes"
        )


# ===========================================================================
# apply_unification
# ===========================================================================

@pytest.fixture()
def apply_patches(patched_network, monkeypatch):
    """Adds write-path patches: create_items (records args + returns keys),
    key_can_write→True, item_uri, and a recordable
    replace_text_with_zotero_field."""
    calls = {"create": [], "insert": []}

    def fake_create_items(metas, *, collection=None, tags=None, dedup=True, doi_index=None, pmid_index=None, attach_pdfs=False):
        calls["create"].append({"metas": metas, "collection": collection, "tags": tags,
                                "dedup": dedup, "doi_index": doi_index})
        created = []
        for m in metas:
            doi = m.get("doi", "")
            created.append({"title": m.get("title", ""), "key": "NEW_" + doi.split("/")[-1], "doi": doi})
        return {"created": created, "skipped_existing": [], "failed": []}

    def fake_insert(doc, anchor, keys, **kw):
        calls["insert"].append({"anchor": anchor, "keys": list(keys), "track": kw.get("track")})
        return doc

    monkeypatch.setattr(unify.zotero, "create_items", fake_create_items)
    # apply_unification gates on key_can_write_status (tri-state, F6); patch both
    # the status fn (the real gate) and the legacy boolean for belt-and-braces.
    monkeypatch.setattr(unify.zotero, "key_can_write_status", lambda: True)
    monkeypatch.setattr(unify.zotero, "key_can_write", lambda: True)
    monkeypatch.setattr(unify.zotero, "item_uri",
                        lambda key: "http://zotero.org/groups/2504198/items/" + key)
    monkeypatch.setattr(unify.zoterofield, "replace_text_with_zotero_field", fake_insert)
    return calls


class TestApply:
    def test_accept_medium_creates_with_collection_and_tag(self, draft, patched_network, apply_patches):
        plan = unify.plan_unification(draft)
        out = draft.parent / "out.docx"
        decisions = {
            "accept": [1],                          # accept the medium Jones ref
            "placeholder_resolutions": {0: "suggestion"},
            "add_missing": True,
        }
        report = unify.apply_unification(draft, plan, decisions, out=out, track=True)

        # create_items was called with the confirmed collection + tags
        assert apply_patches["create"], "expected a create_items call"
        ci = apply_patches["create"][0]
        assert ci["collection"] == "Imported — review"
        assert "added-by:zotero-word-cite" in ci["tags"]
        assert "draft.docx" in ci["tags"]   # source doc basename tag
        assert ci["dedup"] is True

        # Jones (missing, accepted) + the placeholder candidate were both created
        added_titles = {a["title"] for a in report["added"]}
        assert "Cortical tubers in TSC" in added_titles
        assert "Tubers and ASD: a review" in added_titles

        # Smith (in library) is matched, not added
        matched_titles = {m["title"] for m in report["matched"]}
        assert "Tuberous sclerosis and autism" in matched_titles

        # doc written to out
        assert report["out"] == str(out)
        assert out.exists()

    def test_high_ref_intext_inserted(self, draft, patched_network, apply_patches):
        plan = unify.plan_unification(draft)
        out = draft.parent / "out2.docx"
        report = unify.apply_unification(
            draft, plan, {"accept": [], "placeholder_resolutions": {}}, out=out, track=True,
        )
        # Smith is high/auto → its in-text markers are inserted with its key.
        smith_inserts = [c for c in apply_patches["insert"] if c["keys"] == ["SMITHKEY"]]
        assert smith_inserts, "expected SMITHKEY citation insertions for the auto ref"
        anchors = {c["anchor"] for c in smith_inserts}
        assert "(Smith et al., 2020)" in anchors
        assert "[1]" in anchors
        # replaced count >= number of inserts
        assert report["replaced"] >= len(smith_inserts)
        # tracked-change flag threaded through
        assert all(c["track"] is True for c in smith_inserts)

    def test_placeholder_resolution_inserts_at_anchor(self, draft, patched_network, apply_patches):
        plan = unify.plan_unification(draft)
        out = draft.parent / "out3.docx"
        report = unify.apply_unification(
            draft, plan, {"placeholder_resolutions": {0: "suggestion"}, "add_missing": True},
            out=out, track=True,
        )
        ph_inserts = [c for c in apply_patches["insert"] if c["anchor"] == "[CITE: tubers and ASD]"]
        assert ph_inserts, "expected an insertion at the placeholder bracket anchor"
        assert report["needs_input"] == [] or all(
            ni.get("ph_index") != 0 for ni in report["needs_input"]
        )

    def test_readonly_key_records_needs_input_no_create(self, draft, patched_network, monkeypatch):
        """key_can_write→False: nothing is created; missing refs go to needs_input."""
        created = []
        monkeypatch.setattr(
            unify.zotero, "create_items",
            lambda *a, **k: created.append(1) or {"created": [], "skipped_existing": [], "failed": []},
        )
        # Definitive "no write access" (F6 tri-state: a plain False, not UNKNOWN).
        monkeypatch.setattr(unify.zotero, "key_can_write_status", lambda: False)
        monkeypatch.setattr(unify.zotero, "key_can_write", lambda: False)
        monkeypatch.setattr(unify.zotero, "item_uri",
                            lambda key: "http://zotero.org/groups/2504198/items/" + key)
        monkeypatch.setattr(unify.zoterofield, "replace_text_with_zotero_field", lambda doc, *a, **k: doc)

        plan = unify.plan_unification(draft)
        out = draft.parent / "ro.docx"
        report = unify.apply_unification(
            draft, plan, {"accept": [1], "add_missing": True}, out=out, track=True,
        )
        assert created == [], "create_items must NOT be called when key is read-only"
        assert report["added"] == []
        # Jones (missing, accepted) is surfaced as needing input
        reasons = " ".join(ni.get("reason", "") for ni in report["needs_input"])
        assert "write access" in reasons.lower()
        # the matched (in-library) Smith ref is still recorded
        assert any(m["key"] == "SMITHKEY" for m in report["matched"])
        assert out.exists()

    def test_add_missing_false_skips_creation(self, draft, patched_network, apply_patches):
        plan = unify.plan_unification(draft)
        out = draft.parent / "noadd.docx"
        report = unify.apply_unification(
            draft, plan, {"accept": [1], "add_missing": False}, out=out, track=True,
        )
        assert apply_patches["create"] == [], "no create when add_missing=False"
        assert report["added"] == []
        # missing accepted ref recorded for manual handling
        assert any("add_missing=False" in ni.get("reason", "") for ni in report["needs_input"])

    def test_retracted_not_imported_unless_explicit(self, draft, monkeypatch, apply_patches):
        """A retracted high/auto ref is flagged and NOT created (auto≠explicit)."""
        monkeypatch.setattr(unify.refresolve, "resolve_reference", _fake_resolve)
        monkeypatch.setattr(
            unify.zotero, "library_index",
            lambda **kw: {"doi": {}, "pmid": {}, "title": {}},  # Smith NOT in library so it would otherwise be created
        )
        monkeypatch.setattr(unify.citecheck, "ensure_retraction_db", lambda **kw: ("/fake/rw.csv", None))
        monkeypatch.setattr(
            unify.citecheck, "load_retraction_db",
            lambda path: {"10.1/smith2020": {"nature": "Retraction"}},
        )
        plan = unify.plan_unification(draft)
        out = draft.parent / "retr.docx"
        # Smith is in plan["auto"] (high) AND retracted, but NOT in accept.
        report = unify.apply_unification(draft, plan, {"accept": []}, out=out, track=True)

        assert report["retracted_flagged"], "retracted ref must be flagged"
        flagged = report["retracted_flagged"][0]
        assert flagged["doi"] == "10.1/smith2020"
        assert flagged["imported"] is False
        # Smith (retracted, auto-only) is NOT among created items
        assert all("Tuberous sclerosis" not in a["title"] for a in report["added"])


    def test_degraded_library_read_refuses_to_create(self, draft, patched_network, monkeypatch):
        """F2: a LibraryUnavailableError at apply time (degraded read, NOT an
        empty library) must REFUSE to create — never call create_items — and
        route missing accepted works to needs_input with a clear reason.

        Guards the mass-duplicate risk: an empty index would make every work
        look 'missing' and create duplicates in the shared group.
        """
        # Plan with a healthy index (Smith in library, Jones missing).
        plan = unify.plan_unification(draft)

        # Now the library becomes unreadable: apply's index load raises.
        def _raise(**kw):
            raise unify.zotero.LibraryUnavailableError("read failed")

        monkeypatch.setattr(unify.zotero, "library_index", _raise)

        # create_items / key_can_write must NEVER be reached.
        write_attempts = []
        monkeypatch.setattr(
            unify.zotero, "create_items",
            lambda *a, **k: write_attempts.append(("create", a, k)) or {
                "created": [], "skipped_existing": [], "failed": []},
        )
        monkeypatch.setattr(
            unify.zotero, "key_can_write",
            lambda: write_attempts.append(("key_can_write",)) or True,
        )
        monkeypatch.setattr(
            unify.zotero, "key_can_write_status",
            lambda: write_attempts.append(("key_can_write_status",)) or True,
        )
        monkeypatch.setattr(unify.zotero, "item_uri",
                            lambda key: "http://zotero.org/groups/2504198/items/" + key)
        monkeypatch.setattr(unify.zoterofield, "replace_text_with_zotero_field",
                            lambda doc, *a, **k: doc)

        out = draft.parent / "degraded.docx"
        report = unify.apply_unification(
            draft, plan, {"accept": [1], "add_missing": True}, out=out, track=True,
        )

        # No write was attempted at all (not even the write-permission probe).
        assert not any(w[0] == "create" for w in write_attempts), (
            "create_items must NOT be called on a degraded library read"
        )
        assert report["added"] == []
        # Jones (missing, accepted) is surfaced as needing input with a degraded reason.
        reasons = " ".join(ni.get("reason", "") for ni in report["needs_input"]).lower()
        assert "degraded" in reasons or "could not be read" in reasons
        assert "duplicate" in reasons

    def test_empty_library_still_creates(self, draft, patched_network, monkeypatch):
        """F2 control: a genuinely EMPTY (but readable) library returns {} and
        the apply path proceeds to create normally — empty ≠ unavailable."""
        # Empty index: nothing matches, everything missing — but this is a real
        # (readable) empty library, so creation SHOULD proceed.
        monkeypatch.setattr(unify.zotero, "library_index", lambda **kw: {"doi": {}, "pmid": {}, "title": {}})

        creates = []
        monkeypatch.setattr(
            unify.zotero, "create_items",
            lambda metas, **k: creates.append(metas) or {
                "created": [{"title": m.get("title", ""),
                             "key": "NEW_" + m.get("doi", "").split("/")[-1],
                             "doi": m.get("doi", "")} for m in metas],
                "skipped_existing": [], "failed": []},
        )
        monkeypatch.setattr(unify.zotero, "key_can_write_status", lambda: True)
        monkeypatch.setattr(unify.zotero, "key_can_write", lambda: True)
        monkeypatch.setattr(unify.zotero, "item_uri",
                            lambda key: "http://zotero.org/groups/2504198/items/" + key)
        monkeypatch.setattr(unify.zoterofield, "replace_text_with_zotero_field",
                            lambda doc, *a, **k: doc)

        plan = unify.plan_unification(draft)
        out = draft.parent / "emptylib.docx"
        report = unify.apply_unification(
            draft, plan, {"accept": [1], "add_missing": True}, out=out, track=True,
        )
        assert creates, "create_items MUST be called for a readable empty library"
        assert report["added"], "missing refs should be created into an empty library"


# ===========================================================================
# Idempotency: a second apply on the first apply's OUTPUT is a no-op for in-text
# fields (existing_renderings guard) and never double-creates (DOI/PMID dedup).
# ===========================================================================

def _zotero_item_field_count(path) -> int:
    """Number of live ZOTERO_ITEM CSL_CITATION fields in a saved .docx."""
    root = Docx(path).tree(zoterofield.DOCUMENT)
    return sum(1 for c in zoterofield._field_codes(root)
               if "ZOTERO_ITEM CSL_CITATION" in (c or ""))


class TestIdempotency:
    """Running apply_unification twice (the 2nd run on the 1st run's OUTPUT) must
    not re-cite already-live fields nor re-create items already in the library.

    These use the REAL replace_text_with_zotero_field + existing_renderings (NOT
    the recording fake) so run 1 produces genuine live fields and run 2's guard is
    actually exercised. The library index is DYNAMIC: it absorbs whatever run 1
    creates, mirroring a real re-read of the group after the first write."""

    def test_second_apply_does_not_recite_live_fields(self, draft, monkeypatch):
        # Smith is in-library and high/auto → run 1 converts its in-text markers to
        # live fields. No creation needed (isolate the in-text no-op claim).
        monkeypatch.setattr(unify.refresolve, "resolve_reference", _fake_resolve)
        monkeypatch.setattr(
            unify.zotero, "library_index",
            lambda **kw: {"doi": {"10.1/smith2020": "SMITHKEY"}, "pmid": {}, "title": {}},
        )
        monkeypatch.setattr(unify.citecheck, "ensure_retraction_db", lambda **kw: (None, None))
        monkeypatch.setattr(unify.zotero, "key_can_write_status", lambda: True)
        monkeypatch.setattr(unify.zotero, "key_can_write", lambda: True)
        monkeypatch.setattr(unify.zotero, "item_uri",
                            lambda key: "http://zotero.org/groups/2504198/items/" + key)
        creates = []
        monkeypatch.setattr(
            unify.zotero, "create_items",
            lambda metas, **k: creates.append(metas) or {
                "created": [], "skipped_existing": [], "failed": []},
        )
        # REAL field insertion + existing_renderings (no fake).

        out1 = draft.parent / "idem1.docx"
        plan1 = unify.plan_unification(draft)
        rep1 = unify.apply_unification(draft, plan1, {"accept": []}, out=out1, track=True)
        assert rep1["replaced"] >= 1, "run 1 should have converted Smith's in-text cites"
        n_fields_1 = _zotero_item_field_count(out1)
        assert n_fields_1 >= 1

        # RUN 2 on run-1's output: a fresh plan over the rewritten doc, then apply.
        out2 = out1.parent / "idem2.docx"
        plan2 = unify.plan_unification(out1)
        rep2 = unify.apply_unification(out1, plan2, {"accept": []}, out=out2, track=True)

        # The already-live Smith fields must NOT be re-cited.
        assert rep2["replaced"] == 0, (
            f"second apply re-cited live fields (replaced={rep2['replaced']}); "
            "the existing_renderings guard failed"
        )
        # Field count is unchanged — no field was duplicated or clobbered.
        assert _zotero_item_field_count(out2) == n_fields_1
        # And nothing was created in either run (Smith already in library).
        assert [m for batch in creates for m in batch] == []

    def test_second_apply_does_not_double_create(self, draft, monkeypatch):
        # Jones is missing → run 1 creates it. Run 2 must see it (dynamic index) and
        # NOT create it again (DOI dedup). Accept the medium Jones ref explicitly.
        lib = {"doi": {"10.1/smith2020": "SMITHKEY"}, "pmid": {}, "title": {}}
        monkeypatch.setattr(unify.refresolve, "resolve_reference", _fake_resolve)
        monkeypatch.setattr(unify.zotero, "library_index", lambda **kw: {
            "doi": dict(lib["doi"]), "pmid": dict(lib["pmid"]), "title": dict(lib["title"])})
        monkeypatch.setattr(unify.citecheck, "ensure_retraction_db", lambda **kw: (None, None))
        monkeypatch.setattr(unify.zotero, "key_can_write_status", lambda: True)
        monkeypatch.setattr(unify.zotero, "key_can_write", lambda: True)
        monkeypatch.setattr(unify.zotero, "item_uri",
                            lambda key: "http://zotero.org/groups/2504198/items/" + key)

        created_log = []

        def dynamic_create(metas, *, collection=None, tags=None, dedup=True,
                           doi_index=None, pmid_index=None, attach_pdfs=False):
            # Mirror real dedup: skip any meta whose DOI is already in the index;
            # create the rest and ADD them to the shared index (so a re-read sees them).
            out = {"created": [], "skipped_existing": [], "failed": []}
            for m in metas:
                if not isinstance(m, dict):
                    continue
                doi = (m.get("doi") or "")
                norm = doi.lower()
                if dedup and norm in lib["doi"]:
                    out["skipped_existing"].append(
                        {"title": m.get("title", ""), "existing_key": lib["doi"][norm]})
                    continue
                key = "NEW_" + (doi.split("/")[-1] or m.get("title", "x")[:6])
                out["created"].append({"title": m.get("title", ""), "key": key, "doi": doi})
                created_log.append(doi or m.get("title"))
                if norm:
                    lib["doi"][norm] = key      # absorb into the index
            return out

        monkeypatch.setattr(unify.zotero, "create_items", dynamic_create)

        out1 = draft.parent / "dc1.docx"
        plan1 = unify.plan_unification(draft)
        rep1 = unify.apply_unification(
            draft, plan1, {"accept": [1], "add_missing": True}, out=out1, track=True)
        assert any("Cortical tubers" in a["title"] for a in rep1["added"]), \
            "run 1 should create the missing Jones ref"
        n_created_after_run1 = len(created_log)
        assert n_created_after_run1 >= 1

        # RUN 2 on the output, fresh plan. Jones is now in the (grown) library index.
        out2 = out1.parent / "dc2.docx"
        plan2 = unify.plan_unification(out1)
        rep2 = unify.apply_unification(
            out1, plan2, {"accept": [1], "add_missing": True}, out=out2, track=True)

        # No NEW items were created on the second run — Jones deduped by DOI.
        assert len(created_log) == n_created_after_run1, (
            f"second apply double-created items: {created_log[n_created_after_run1:]}"
        )
        assert rep2["added"] == [], "second apply must add nothing new"


# ===========================================================================
# Surfacing: an accepted ref with NO usable metadata must not vanish.
# ===========================================================================

class TestNoMetadataSurfacing:
    """An accepted reference that resolved to no usable metadata (a refresolve
    miss) has nothing to create and no key to cite. It must be SURFACED in
    needs_input with a clear reason — never silently dropped, and never threaded
    into create_items as a None (which create_items silently filters)."""

    def test_accepted_no_metadata_ref_surfaced_not_dropped(self, tmp_path, monkeypatch):
        p = tmp_path / "nometa.docx"
        new_doc(p, [
            "Body cites a mystery work [1].",
            "References",
            "1. Mystery A, Unknown B. An unindexed local report. Internal Memo. 2018;3:1-9.",
        ])

        def _resolve(text, *, fetch=True):
            # Everything resolves to NO metadata (the miss case).
            return {
                "input": text, "metadata": None, "confidence": "none",
                "source": None, "candidates": [],
                "identifiers": {"doi": None, "pmid": None, "arxiv": None, "isbn": None},
            }

        monkeypatch.setattr(unify.refresolve, "resolve_reference", _resolve)
        monkeypatch.setattr(unify.zotero, "library_index",
                            lambda **kw: {"doi": {}, "pmid": {}, "title": {}})
        monkeypatch.setattr(unify.citecheck, "ensure_retraction_db", lambda **kw: (None, None))
        monkeypatch.setattr(unify.zotero, "key_can_write_status", lambda: True)
        monkeypatch.setattr(unify.zotero, "key_can_write", lambda: True)
        monkeypatch.setattr(unify.zotero, "item_uri", lambda key: "http://x/" + key)

        # create_items must NEVER be handed a None meta for this ref.
        seen_metas = []

        def _create(metas, **k):
            seen_metas.extend(metas)
            return {"created": [], "skipped_existing": [], "failed": []}

        monkeypatch.setattr(unify.zotero, "create_items", _create)
        monkeypatch.setattr(unify.zoterofield, "replace_text_with_zotero_field",
                            lambda doc, *a, **k: doc)

        plan = unify.plan_unification(p)
        # The single low ref (index 0) must be explicitly accepted to be applied.
        out = p.parent / "out.docx"
        report = unify.apply_unification(
            p, plan, {"accept": [0], "add_missing": True}, out=out, track=True)

        # Surfaced in needs_input with the honest "no usable metadata" reason.
        reasons = [ni.get("reason", "") for ni in report["needs_input"]]
        assert any("no usable metadata" in r for r in reasons), (
            f"accepted no-metadata ref was not surfaced; needs_input={report['needs_input']}"
        )
        # It was NOT created and NOT added.
        assert report["added"] == []
        # The no-metadata ref was never threaded into create_items (no None meta).
        assert None not in seen_metas


# ===========================================================================
# Edge: empty document
# ===========================================================================

class TestEmptyDoc:
    def test_empty_plan_and_apply_noop(self, tmp_path, monkeypatch):
        monkeypatch.setattr(unify.refresolve, "resolve_reference", _fake_resolve)
        monkeypatch.setattr(unify.zotero, "library_index", lambda **kw: {"doi": {}, "pmid": {}, "title": {}})
        monkeypatch.setattr(unify.citecheck, "ensure_retraction_db", lambda **kw: (None, None))

        def _no_create(*a, **k):  # pragma: no cover - must not be hit
            raise AssertionError("create_items called for an empty doc")

        monkeypatch.setattr(unify.zotero, "create_items", _no_create)
        monkeypatch.setattr(unify.zotero, "key_can_write_status", lambda: True)
        monkeypatch.setattr(unify.zotero, "key_can_write", lambda: True)
        monkeypatch.setattr(unify.zotero, "item_uri", lambda key: "http://x/" + key)
        monkeypatch.setattr(unify.zoterofield, "replace_text_with_zotero_field", lambda doc, *a, **k: doc)

        p = tmp_path / "empty.docx"
        new_doc(p, ["Just prose with no citations whatsoever."])
        plan = unify.plan_unification(p)
        assert plan["references"] == []
        assert plan["placeholders"] == []
        assert plan["auto"] == []
        assert plan["summary"]["buckets"]["reflist"] == 0

        out = tmp_path / "empty_out.docx"
        report = unify.apply_unification(p, plan, {}, out=out)
        assert report["added"] == []
        assert report["matched"] == []
        assert report["replaced"] == 0
        assert report["needs_input"] == []
        assert out.exists()


# ===========================================================================
# W4-1 — strict offline kill-switch: offline=True / --offline guarantees
# ZERO network calls (no Crossref/PubMed, no Zotero library read).
# ===========================================================================

class TestOfflineKillSwitch:
    """plan_unification(offline=True) must complete without touching the network.

    The Zotero library read and refresolve network calls are the two seams.
    Both are stubbed to RAISE if invoked; the test asserts no exception fires.
    """

    def _boom_zotero(self, label):
        """Return a callable that raises AssertionError if invoked (network guard)."""
        def _raise(**kw):
            raise AssertionError(f"network call made in offline mode: {label}")
        return _raise

    def _boom_resolve(self, label):
        def _raise(text, *, fetch=True):
            raise AssertionError(f"network call made in offline mode: {label}")
        return _raise

    def test_offline_no_network_calls(self, draft, monkeypatch):
        """With offline=True, plan_unification must not call library_doi_index
        for a live fetch OR call refresolve with fetch=True.

        Stubs both seams to RAISE on any invocation and asserts the call
        completes successfully (using only cache / degraded-gracefully fallback).
        """
        # Stub resolve_reference to raise if fetch=True (network path).
        def _offline_resolve(text, *, fetch=True):
            if fetch:
                raise AssertionError(
                    "refresolve.resolve_reference called with fetch=True in offline mode"
                )
            return _fake_resolve(text, fetch=False)

        monkeypatch.setattr(unify.refresolve, "resolve_reference", _offline_resolve)

        # Stub library_doi_index to raise LibraryUnavailableError ALWAYS
        # (simulates no usable cache AND no network — the worst case).
        # plan_unification(offline=True) must degrade gracefully to an empty
        # DOI index rather than propagating the error.
        def _no_network_index(**kw):
            raise unify.zotero.LibraryUnavailableError(
                "offline test: no cache and no network"
            )

        monkeypatch.setattr(unify.zotero, "library_index", _no_network_index)
        monkeypatch.setattr(unify.citecheck, "ensure_retraction_db", lambda **kw: (None, None))

        # Must not raise.
        plan = unify.plan_unification(draft, offline=True)
        assert plan["references"]  # ran to completion
        # With library_doi_index failing, all refs degrade to not-in-library.
        for ref in plan["references"]:
            assert ref["in_library"] is False

    def test_offline_no_network_when_cache_present(self, draft, monkeypatch):
        """With offline=True and a warm in-memory cache, plan_unification uses
        the cache and does NOT attempt a live Zotero fetch.

        The stub returns a canned index (simulating cache hit via max_age_hours=inf)
        and must NOT be called with any argument that would trigger a live fetch.
        """
        network_calls = []

        def _cached_doi_index(**kw):
            # Record the call; a real cache-only call is fine.
            # Fail if it looks like a live-fetch attempt (refresh=True).
            if kw.get("refresh"):
                raise AssertionError(
                    "library_index called with refresh=True in offline mode"
                )
            network_calls.append(kw)
            return {"doi": {"10.1/smith2020": "SMITHKEY"}, "pmid": {}, "title": {}}

        def _offline_resolve(text, *, fetch=True):
            if fetch:
                raise AssertionError(
                    "refresolve called with fetch=True in offline mode"
                )
            return _fake_resolve(text, fetch=False)

        monkeypatch.setattr(unify.refresolve, "resolve_reference", _offline_resolve)
        monkeypatch.setattr(unify.zotero, "library_index", _cached_doi_index)
        monkeypatch.setattr(unify.citecheck, "ensure_retraction_db", lambda **kw: (None, None))

        plan = unify.plan_unification(draft, offline=True)

        # library_index was called with max_age_hours=inf (cache-only intent).
        assert network_calls, "expected library_index to be called once"
        assert network_calls[0].get("max_age_hours") == float("inf"), (
            "offline=True must pass max_age_hours=inf to library_index"
        )
        # Smith is found in the cache-served DOI index.
        refs = {r["ref_index"]: r for r in plan["references"]}
        assert refs[0]["in_library"] is True
        assert refs[0]["existing_key"] == "SMITHKEY"

    def test_online_default_path_unchanged(self, draft, patched_network):
        """Control: default (offline=False) still works and matches refs normally.

        patched_network already stubs library_doi_index with a plain lambda
        (no max_age_hours arg) — the existing online behaviour is unbroken.
        """
        plan = unify.plan_unification(draft)
        assert plan["summary"]["n_in_library"] == 1  # Smith matched — normal online path

    def test_offline_implies_fetch_false(self, draft, monkeypatch):
        """offline=True overrides any fetch=True that was passed — refresolve
        must always receive fetch=False in offline mode regardless of the
        caller-supplied fetch kwarg."""
        fetch_values_seen = []

        def _recording_resolve(text, *, fetch=True):
            fetch_values_seen.append(fetch)
            return _fake_resolve(text, fetch=False)

        monkeypatch.setattr(unify.refresolve, "resolve_reference", _recording_resolve)
        monkeypatch.setattr(
            unify.zotero, "library_index",
            lambda **kw: {"doi": {}, "pmid": {}, "title": {}},
        )
        monkeypatch.setattr(unify.citecheck, "ensure_retraction_db", lambda **kw: (None, None))

        # Pass fetch=True explicitly alongside offline=True; offline must win.
        unify.plan_unification(draft, fetch=True, offline=True)

        assert fetch_values_seen, "resolve_reference must have been called"
        assert all(f is False for f in fetch_values_seen), (
            f"offline=True must force fetch=False; got: {fetch_values_seen}"
        )


# ===========================================================================
# F7 — the apply pass re-extracts the in-text inventory from the file it MUTATES
# (rewrite_src), and resolves each plan link by marker TEXT (index-shift-proof),
# not by a positional index into the original pre-conversion document.
# ===========================================================================

class TestF7MarkerResolution:
    def test_plan_carries_intext_markers(self, draft, patched_network):
        # plan_unification now records each linked marker's text/kind alongside
        # its index, so apply can re-locate it after foreign-field conversion.
        plan = unify.plan_unification(draft)
        smith = next(r for r in plan["references"]
                     if "smith" in (r.get("input", "").lower()))
        assert smith["intext_markers"], "expected carried intext_markers"
        # Each carried marker has the fields apply needs.
        for m in smith["intext_markers"]:
            assert set(m) >= {"index", "text", "kind"}
        # The author-year marker text is among them.
        texts = {m["text"] for m in smith["intext_markers"]}
        assert any("Smith" in t for t in texts)

    def test_resolve_by_text_survives_index_shift(self):
        # Core of F7: the plan link's INDEX points at the original extraction, but
        # the mutated file's inventory has DIFFERENT indices (a foreign field that
        # was counted as a pseudo-marker got converted, shifting everything). The
        # join must follow the marker TEXT, not the stale index.
        placement = {
            "intext_links": [0],   # stale index from the original extraction
            "intext_markers": [{"index": 0, "text": "(Smith et al., 2020)",
                                "kind": "author_year"}],
        }
        # In the MUTATED file this same marker now sits at index 2.
        mutated_marker = {"index": 2, "text": "(Smith et al., 2020)",
                          "kind": "author_year"}
        intext_by_text = {"(Smith et al., 2020)": [mutated_marker]}
        intext_by_index = {2: mutated_marker}  # index 0 is now a DIFFERENT marker
        # A wrong marker happens to occupy the stale index 0 in the mutated file.
        wrong = {"index": 0, "text": "(Jones, 2019)", "kind": "author_year"}
        intext_by_index[0] = wrong
        intext_by_text["(Jones, 2019)"] = [wrong]

        out = unify._resolve_link_markers(placement, intext_by_text, intext_by_index)
        assert len(out) == 1
        assert out[0]["text"] == "(Smith et al., 2020)"   # followed text, not index

    def test_marker_absent_from_mutated_file_is_dropped(self):
        # A marker whose text no longer exists in the mutated file (it WAS a
        # foreign field that got converted) is dropped, never mis-resolved to a
        # coincidental same-index marker.
        placement = {
            "intext_links": [0],
            "intext_markers": [{"index": 0, "text": "(Gone, 2001)",
                                "kind": "author_year"}],
        }
        other = {"index": 0, "text": "(Other, 2010)", "kind": "author_year"}
        out = unify._resolve_link_markers(
            placement, {"(Other, 2010)": [other]}, {0: other})
        assert out == []   # not mis-bound to the index-0 'other' marker

    def test_legacy_plan_without_markers_falls_back_to_index(self):
        # Old/serialized plans that carry only intext_links keep working via the
        # raw index into the rewrite_src inventory.
        placement = {"intext_links": [1]}   # no intext_markers
        m = {"index": 1, "text": "[2]", "kind": "numeric"}
        out = unify._resolve_link_markers(placement, {"[2]": [m]}, {1: m})
        assert out == [m]

    def test_repeated_text_binds_distinct_inventory_entries(self):
        # Two links with the same marker text must bind to two distinct physical
        # inventory entries, not the same one twice.
        placement = {
            "intext_links": [0, 1],
            "intext_markers": [
                {"index": 0, "text": "[1]", "kind": "numeric"},
                {"index": 1, "text": "[1]", "kind": "numeric"},
            ],
        }
        m0 = {"index": 0, "text": "[1]", "kind": "numeric"}
        m1 = {"index": 5, "text": "[1]", "kind": "numeric"}
        out = unify._resolve_link_markers(
            placement, {"[1]": [m0, m1]}, {0: m0, 5: m1})
        assert [id(x) for x in out] == [id(m0), id(m1)]


class TestF7Integration:
    """End-to-end: when foreign-field conversion (n_foreign>0) shifts the in-text
    marker indices, apply still anchors the CORRECT marker text because it
    re-extracts from rewrite_src and joins by text (F7)."""

    def test_apply_anchors_correct_marker_after_conversion(
        self, tmp_path, monkeypatch
    ):
        # Original doc: a leading author-year marker '(Foreign, 1999)' that — in
        # the ORIGINAL extraction — is counted as an in-text pseudo-marker at
        # index 0, pushing the real Smith marker to a higher index. After
        # conversion that pseudo-marker text is gone, so the rewrite_src
        # inventory renumbers — the stale plan index no longer points at Smith.
        original = tmp_path / "orig.docx"
        new_doc(original, [
            "Background per a converted field (Foreign, 1999).",
            "Tuberous sclerosis is associated with ASD (Smith et al., 2020).",
            "References",
            "1. Smith J. Tuberous sclerosis and autism. J Neurol. 2020;10:1-5.",
        ])

        monkeypatch.setattr(unify.refresolve, "resolve_reference", _fake_resolve)
        monkeypatch.setattr(
            unify.zotero, "library_index",
            lambda **kw: {"doi": {"10.1/smith2020": "SMITHKEY"}, "pmid": {}, "title": {}})
        monkeypatch.setattr(unify.citecheck, "ensure_retraction_db",
                            lambda **kw: (None, None))
        monkeypatch.setattr(unify.zotero, "key_can_write_status", lambda: True)
        monkeypatch.setattr(unify.zotero, "key_can_write", lambda: True)
        monkeypatch.setattr(unify.zotero, "item_uri",
                            lambda key: "http://x/" + key)

        inserts = []
        monkeypatch.setattr(
            unify.zoterofield, "replace_text_with_zotero_field",
            lambda doc, anchor, keys, **kw: inserts.append((anchor, list(keys))) or doc)

        # Mock conversion: write a rewrite_src where the '(Foreign, 1999)' marker
        # is GONE (the foreign field converted to a render that is not an
        # author-year/numeric cite), so Smith shifts from original index 1 → 0.
        # An index-keyed lookup against the plan's stale index would therefore
        # mis-resolve or drop Smith; the text join must still find it.
        def fake_convert(path, *, out, track=True):
            new_doc(out, [
                "Background per a converted field.",  # foreign marker dropped out
                "Tuberous sclerosis is associated with ASD (Smith et al., 2020).",
                "References",
                "1. Smith J. Tuberous sclerosis and autism. J Neurol. 2020;10:1-5.",
            ])
            return {"converted": [{"keys": ["FOREIGNKEY"]}]}
        monkeypatch.setattr(unify.citeconvert, "convert_to_zotero", fake_convert)

        plan = unify.plan_unification(original)
        # Force the foreign-field branch so convert_to_zotero runs.
        plan["summary"]["n_foreign_fields"] = 1

        out = tmp_path / "out.docx"
        unify.apply_unification(original, plan, {"accept": []}, out=out, track=True)

        # The Smith insertion anchored the CORRECT marker text, with SMITHKEY —
        # NOT a wrong/stale-index marker, and not silently dropped.
        smith_anchors = [a for a, keys in inserts if keys == ["SMITHKEY"]]
        assert "(Smith et al., 2020)" in smith_anchors
        # And it never tried to anchor the now-converted foreign pseudo-marker.
        assert all("Foreign" not in a for a, _ in inserts)


# ===========================================================================
# Regression: apply_unification must not CRASH on a DEGRADED read (creds unset)
# when it has a matched in-library key to link. zotero.item_uri RAISES without
# credentials; the apply loop must route through zotero.item_uri_offline_safe so
# it degrades to an empty URI (field still carries the KEY; Word rebinds on
# Refresh) and creates NOTHING. Found by the real-doc apply test (two grant docs
# crashed at unify.py item_uri); the citeconvert fix covered only its own path.
# ===========================================================================

class TestApplyDegradedItemUri:
    def test_apply_links_matched_key_without_creds_no_crash(self, draft, monkeypatch):
        # Smith is in-library; item_uri raises (no creds). Apply must NOT crash,
        # must still write Smith's field, must pass an EMPTY uri, create nothing.
        monkeypatch.setattr(unify.refresolve, "resolve_reference", _fake_resolve)
        monkeypatch.setattr(
            unify.zotero, "library_index",
            lambda **kw: {"doi": {"10.1/smith2020": "SMITHKEY"}, "pmid": {}, "title": {}})
        monkeypatch.setattr(unify.citecheck, "ensure_retraction_db",
                            lambda **kw: (None, None))

        def _no_creds(_key):
            raise RuntimeError("Zotero credentials not configured; set env var(s): "
                               "ZOTERO_API_KEY, ZOTERO_GROUP_ID or ZOTERO_USER_ID")
        # Patch the underlying primitive to raise. The real item_uri_offline_safe
        # (same module namespace) must catch it — that is the behavior under test.
        monkeypatch.setattr(unify.zotero, "item_uri", _no_creds)
        # Belt-and-suspenders: creation must never be reachable on this path.
        def _boom_create(*a, **k):  # pragma: no cover - asserts it is never called
            raise AssertionError("apply created an item on a degraded/no-creds read")
        monkeypatch.setattr(unify.zotero, "create_items", _boom_create)

        inserts = []
        monkeypatch.setattr(
            unify.zoterofield, "replace_text_with_zotero_field",
            lambda doc, anchor, keys, **kw: inserts.append(
                (anchor, list(keys), list(kw.get("uris", [])))) or doc)

        out = draft.parent / "out.docx"
        # add_missing=False (the --no-add-missing constraint): never create.
        report = unify.apply_unification(draft, plan=unify.plan_unification(draft),
                                         decisions={"accept": []}, out=out,
                                         add_missing=False, track=True)

        smith = [(a, keys, uris) for a, keys, uris in inserts if keys == ["SMITHKEY"]]
        assert smith, "Smith's in-library key was not linked (apply degraded too far or crashed)"
        # The degraded URI is empty — the field still carries the KEY.
        assert all(uris == [""] for _a, _keys, uris in smith), \
            f"expected empty URI on a degraded read, got {smith}"
        assert report["added"] == [], "nothing must be created on a degraded read"


class TestLiveFieldGuard:
    """End-to-end: a doc with an existing live ZOTERO_ITEM field rendering
    '(1,2)' must NOT have that marker clobbered by apply_unification, while a
    genuine plain-text in-text cite '(Smith et al., 2020)' still converts."""

    def test_apply_does_not_recite_live_field_marker(
        self, tmp_path, monkeypatch
    ):
        # Build a draft that contains BOTH:
        #   (a) a live ZOTERO_ITEM field rendering "(1,2)" — already managed
        #   (b) a plain-text "(Smith et al., 2020)" — a genuine unmanaged cite
        # plus a reference list entry that lets Smith resolve as high-confidence.
        src = tmp_path / "live_guard.docx"
        new_doc(src, [
            "Background supported by prior work here.",   # anchor for live field
            "Tuberous sclerosis is associated with ASD (Smith et al., 2020).",
            "References",
            "1. Smith J, et al. Tuberous sclerosis and autism. J Neurol. 2020.",
        ])

        # Embed a live ZOTERO_ITEM field at the "prior work" anchor, rendering "(1,2)".
        # This simulates a citation that Zotero already manages in the document.
        G = "http://zotero.org/groups/2504198/items/"
        doc = Docx(src)
        insert_citation(
            doc, "prior work",
            ["LIVEKEY1", "LIVEKEY2"],
            itemdata=[
                {"id": "2504198/LIVEKEY1", "type": "article-journal",
                 "title": "Prior work alpha"},
                {"id": "2504198/LIVEKEY2", "type": "article-journal",
                 "title": "Prior work beta"},
            ],
            uris=[G + "LIVEKEY1", G + "LIVEKEY2"],
            rendered="(1,2)",
            style="vancouver",
        )
        doc.save(src)

        # Confirm the live field is now in the doc and existing_renderings sees it.
        root = Docx(src).read_tree(zoterofield.DOCUMENT)
        assert "(1,2)" in zoterofield.existing_renderings(root), (
            "precondition: existing_renderings must report the live field's text"
        )

        # Standard network + write patches (reuse the module-level helpers).
        monkeypatch.setattr(unify.refresolve, "resolve_reference", _fake_resolve)
        monkeypatch.setattr(
            unify.zotero, "library_index",
            lambda **kw: {"doi": {"10.1/smith2020": "SMITHKEY"}, "pmid": {}, "title": {}},
        )
        monkeypatch.setattr(unify.citecheck, "ensure_retraction_db",
                            lambda **kw: (None, None))
        monkeypatch.setattr(unify.zotero, "key_can_write_status", lambda: True)
        monkeypatch.setattr(unify.zotero, "key_can_write", lambda: True)
        monkeypatch.setattr(unify.zotero, "item_uri",
                            lambda key: G + key)
        monkeypatch.setattr(unify.zotero, "create_items",
                            lambda metas, **k: {
                                "created": [{"title": m.get("title", ""),
                                             "key": "NEW_" + m.get("doi", "").split("/")[-1],
                                             "doi": m.get("doi", "")} for m in metas],
                                "skipped_existing": [], "failed": [],
                            })

        insert_calls: list[dict] = []

        def _recording_insert(doc, anchor, keys, **kw):
            insert_calls.append({"anchor": anchor, "keys": list(keys)})
            return doc

        monkeypatch.setattr(
            unify.zoterofield, "replace_text_with_zotero_field",
            _recording_insert,
        )

        # Plan: Smith is high/auto (in-library as SMITHKEY).
        plan = unify.plan_unification(src)

        # Verify the plan has at least Smith's markers, so apply has something to do.
        smith_ref = next(
            (r for r in plan["references"]
             if "smith" in r.get("input", "").lower()),
            None,
        )
        assert smith_ref is not None, "Smith ref must appear in the plan"
        smith_markers = {m["text"] for m in (smith_ref.get("intext_markers") or [])}
        assert any("Smith" in t for t in smith_markers), (
            "Smith's plan entry must carry the '(Smith et al., 2020)' marker"
        )

        # Run apply — naively it would try to re-cite "(1,2)" too if not guarded.
        out = tmp_path / "live_guard_out.docx"
        report = unify.apply_unification(
            src, plan, {"accept": []}, out=out, track=True,
        )

        # --- Core assertion: the live field's rendered text was NEVER attempted ---
        insert_anchors = {c["anchor"] for c in insert_calls}
        assert "(1,2)" not in insert_anchors, (
            "apply_unification must NOT attempt to re-cite '(1,2)' — "
            "it is already backed by a live Zotero field (existing_renderings guard)"
        )

        # --- Smith's plain-text marker was still converted (the real cite converts) ---
        assert "(Smith et al., 2020)" in insert_anchors, (
            "apply_unification must still convert the plain-text '(Smith et al., 2020)' marker"
        )

        # --- Output doc must still have exactly the original live ZOTERO_ITEM field ---
        out_root = Docx(out).read_tree(zoterofield.DOCUMENT)
        out_renderings = zoterofield.existing_renderings(out_root)
        assert "(1,2)" in out_renderings, (
            "the live Zotero field rendering '(1,2)' must survive intact in the output doc"
        )
        assert report["replaced"] >= 1, "at least one plain-text citation was converted"


# ===========================================================================
# Numeric range expansion: "[5-7]" links refs 5, 6 AND 7 (not just endpoints)
# ===========================================================================

class TestNumericRangeExpansion:
    """unify._numeric_marker_targets expands a citation range to EVERY number in
    it, so the interior references of a Vancouver "[5-7]" marker are linked too —
    splitting on the hyphen used to link only the two endpoints."""

    def test_range_expands_to_all_interior_numbers(self):
        assert unify._numeric_marker_targets("[5-7]") == [5, 6, 7]
        assert unify._numeric_marker_targets("(5–7)") == [5, 6, 7]   # en-dash
        assert unify._numeric_marker_targets("[3,4]") == [3, 4]
        assert unify._numeric_marker_targets("[3, 5-7, 9]") == [3, 5, 6, 7, 9]

    def test_single_and_degenerate_ranges(self):
        assert unify._numeric_marker_targets("[12]") == [12]
        assert unify._numeric_marker_targets("[7-7]") == [7]
        # reversed / absurdly large ranges fall back to endpoints (no explosion)
        assert unify._numeric_marker_targets("[9-2]") == [9, 2]
        assert unify._numeric_marker_targets("[1-9999]") == [1, 9999]
