"""decisions["intext_keys"] and confirm-only PubMed candidates (unify).

* intext_keys: an in-text marker → DOI / PMID / Zotero key (a list for a
  grouped marker) becomes a live Zotero citation field even with NO reference
  list; missing identifiers go through the existing gated create path.
* Validation runs before any Zotero write or document change and fails closed.
* plan_unification offers bounded, confirm-only PubMed candidates for
  author-year items with no reference-list link; offline makes no calls.

Synthetic names, titles and identifiers only (DOIs under 10.5555/, invented
PMIDs); every network seam is monkeypatched. No Zotero writes.
"""
from __future__ import annotations

import html
import json
import re
import zipfile
from pathlib import Path

import pytest

from zoterocite import new_doc
from zoterocite import unify
from zoterocite import zotero


GROUP = "(Alpha et al., 2022 and Beta et al., 2012)"
NARR = "Gamma et al., in 2019"
PAGAN = "(Pagán et al., 2021)"

LIB = {
    "doi": {"10.5555/alpha.2022": "ALPHAKEY"},
    "pmid": {"31415926": "BETAKEY1"},
    "title": {"a synthetic title held in the library": "GAMMAKY2"},
}

PAGAN_META = {
    "doi": "10.5555/pagan.2021", "title": "Synthetic replication study",
    "authors": [{"family": "Pagán", "given": "R"}], "year": "2021",
    "journal": "J Synth", "type": "journal-article",
}


@pytest.fixture()
def doc(tmp_path: Path) -> Path:
    """Author-year draft with NO reference list."""
    p = tmp_path / "noreflist.docx"
    new_doc(p, [
        f"Prior cohorts were small {GROUP}.",
        f"Imaging was first reported by {NARR} in a pilot.",
        f"A follow-up confirmed the effect {PAGAN}.",
    ])
    return p


@pytest.fixture()
def net(monkeypatch):
    """Library index, metadata fetchers, create path, retraction DB. Records
    every create / fetch / PubMed call."""
    rec = {"create": [], "doi_fetch": [], "pmid_fetch": [], "esearch": [], "efetch": []}
    monkeypatch.setattr(zotero, "library_index", lambda **kw: json.loads(json.dumps(LIB)))
    monkeypatch.setattr(unify.citecheck, "ensure_retraction_db", lambda **kw: (None, None))
    monkeypatch.setattr(zotero, "key_can_write_status", lambda: True)
    monkeypatch.setattr(zotero, "item_uri", lambda k: "http://zotero.org/groups/5555/items/" + k)

    def _doi_fetch(doi):
        rec["doi_fetch"].append(doi)
        return dict(PAGAN_META) if doi == "10.5555/pagan.2021" else None

    def _pmid_fetch(pmid):
        rec["pmid_fetch"].append(pmid)
        return None
    monkeypatch.setattr(unify.refresolve, "_crossref_doi_fetch", _doi_fetch)
    monkeypatch.setattr(unify.refresolve, "_pubmed_fetch", _pmid_fetch)
    monkeypatch.setattr(unify.refresolve, "resolve_reference",
                        lambda *a, **k: pytest.fail("resolve_reference not expected"))

    def _create(metas, **kw):
        rec["create"].append({"metas": [dict(m) for m in metas], **kw})
        return {"created": [{"title": m.get("title") or "", "key": "NEWKEY01",
                             "doi": m.get("doi") or ""} for m in metas],
                "skipped_existing": [], "failed": [], "skipped_degraded_read": []}
    monkeypatch.setattr(zotero, "create_items", _create)

    def _esearch(term, **kw):
        rec["esearch"].append(term)
        return [], {"degraded": False, "reason": None}

    def _efetch(pmids):
        rec["efetch"].append(list(pmids))
        return {}, {"degraded": False}
    monkeypatch.setattr(unify.entrez, "esearch_pmids_status", _esearch)
    monkeypatch.setattr(unify.entrez, "efetch_pubmed_status", _efetch)
    return rec


def _plan(doc):
    return unify.plan_unification(doc, fetch=False)


def _index_of(plan, text):
    return next(m["index"] for m in plan["intext"] if m["text"] == text)


def _fields(path: Path) -> list:
    """CSL_CITATION payloads of every Zotero field in document order."""
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    out = []
    for raw in re.findall(r"<w:instrText[^>]*>(.*?)</w:instrText>", xml, re.S):
        instr = html.unescape(raw).strip()
        if instr.startswith("ADDIN ZOTERO_ITEM CSL_CITATION"):
            out.append(json.loads(instr[len("ADDIN ZOTERO_ITEM CSL_CITATION"):]))
    return out


def _uris(field: dict) -> list:
    return [ci["uris"][0] for ci in field["citationItems"]]


U = "http://zotero.org/groups/5555/items/"


# ===========================================================================
# End to end — no reference list
# ===========================================================================

class TestIntextKeysEndToEnd:
    def test_markers_become_fields_with_the_right_items(self, doc, net, tmp_path):
        plan = _plan(doc)
        assert plan["references"] == []           # no reference list at all
        decisions = {"intext_keys": {
            str(_index_of(plan, GROUP)): ["10.5555/ALPHA.2022", "PMID:31415926"],
            NARR: "GAMMAKY2",                     # Zotero item key in the library
            PAGAN: "https://doi.org/10.5555/pagan.2021",   # missing -> created
        }}
        out = tmp_path / "out.docx"
        rep = unify.apply_unification(doc, plan, decisions, out=out, track=True)

        assert rep["replaced"] == 3, rep
        assert rep["unresolved_in_doc"] == [] and rep["needs_input"] == []
        assert [f for f in map(_uris, _fields(out))] == [
            [U + "ALPHAKEY", U + "BETAKEY1"],      # grouped marker, item order
            [U + "GAMMAKY2"],
            [U + "NEWKEY01"],
        ]
        # The missing DOI went through the existing create path: once, tagged,
        # into the review collection, dedup on, freshness flag passed.
        assert net["doi_fetch"] == ["10.5555/pagan.2021"]
        (call,) = net["create"]
        assert [m["doi"] for m in call["metas"]] == ["10.5555/pagan.2021"]
        assert call["collection"] == unify.IMPORTED_COLLECTION
        assert unify.ADDED_TAG in call["tags"] and "noreflist.docx" in call["tags"]
        assert call["dedup"] is True and call["index_degraded"] is False
        assert call["doi_index"] == LIB["doi"] and call["pmid_index"] == LIB["pmid"]
        assert [a["key"] for a in rep["added"]] == ["NEWKEY01"]
        assert rep["added"][0]["intext_index"] == _index_of(plan, PAGAN)

    def test_tracked_by_default_and_untracked_on_request(self, doc, net, tmp_path):
        plan = _plan(doc)
        dec = {"intext_keys": {NARR: "GAMMAKY2"}}
        out = tmp_path / "t.docx"
        unify.apply_unification(doc, plan, dec, out=out)
        xml = zipfile.ZipFile(out).read("word/document.xml").decode("utf-8")
        assert "<w:ins " in xml and "<w:del " in xml
        assert "Gamma et al., in 2019" in html.unescape(
            "".join(re.findall(r"<w:delText[^>]*>(.*?)</w:delText>", xml)))
        out2 = tmp_path / "u.docx"
        unify.apply_unification(doc, plan, dec, out=out2, track=False)
        xml2 = zipfile.ZipFile(out2).read("word/document.xml").decode("utf-8")
        assert "<w:ins " not in xml2 and "<w:del " not in xml2
        assert len(_fields(out2)) == 1

    def test_shared_identifier_created_once(self, doc, net, tmp_path):
        plan = _plan(doc)
        dec = {"intext_keys": {NARR: "10.5555/pagan.2021", PAGAN: "10.5555/pagan.2021"}}
        out = tmp_path / "o.docx"
        rep = unify.apply_unification(doc, plan, dec, out=out)
        assert len(net["create"]) == 1 and len(net["create"][0]["metas"]) == 1
        assert net["doi_fetch"] == ["10.5555/pagan.2021"]
        assert [_uris(f) for f in _fields(out)] == [[U + "NEWKEY01"], [U + "NEWKEY01"]]
        assert rep["replaced"] == 2

    def test_add_missing_off_skips_and_reports(self, doc, net, tmp_path):
        plan = _plan(doc)
        dec = {"intext_keys": {PAGAN: "10.5555/pagan.2021", NARR: "GAMMAKY2"},
               "add_missing": False}
        out = tmp_path / "o.docx"
        rep = unify.apply_unification(doc, plan, dec, out=out)
        assert net["create"] == [] and net["doi_fetch"] == []
        (ni,) = rep["needs_input"]
        assert "add_missing=False" in ni["reason"]
        assert ni["intext_index"] == _index_of(plan, PAGAN)
        assert [u["marker"] for u in rep["unresolved_in_doc"]] == [PAGAN]
        assert [_uris(f) for f in _fields(out)] == [[U + "GAMMAKY2"]]

    def test_unresolvable_identifier_reported_not_created(self, doc, net, tmp_path):
        plan = _plan(doc)
        dec = {"intext_keys": {PAGAN: "PMID:27182818"}}
        rep = unify.apply_unification(doc, plan, dec, out=tmp_path / "o.docx")
        assert net["pmid_fetch"] == ["27182818"] and net["create"] == []
        assert "could not resolve" in rep["needs_input"][0]["reason"]
        assert rep["replaced"] == 0

    def test_partial_group_left_plain(self, doc, net, tmp_path):
        """All-or-nothing for a grouped marker: one unkeyable item leaves it plain."""
        plan = _plan(doc)
        dec = {"intext_keys": {GROUP: ["10.5555/alpha.2022", "PMID:27182818"]}}
        out = tmp_path / "o.docx"
        rep = unify.apply_unification(doc, plan, dec, out=out)
        assert _fields(out) == []
        (u,) = rep["unresolved_in_doc"]
        assert u["marker"] == GROUP and "[1]" in u["reason"]

    def test_index_key_wins_over_text_key(self, doc, net, tmp_path):
        plan = _plan(doc)
        idx = _index_of(plan, NARR)
        dec = {"intext_keys": {NARR: "10.5555/alpha.2022", str(idx): "GAMMAKY2"}}
        out = tmp_path / "o.docx"
        unify.apply_unification(doc, plan, dec, out=out)
        assert [_uris(f) for f in _fields(out)] == [[U + "GAMMAKY2"]]

    def test_intext_keys_override_an_auto_link(self, tmp_path, net, monkeypatch):
        """A marker auto-linked to a HIGH (auto) reference can still be pointed
        elsewhere by intext_keys; the marker is converted once, as asked."""
        p = tmp_path / "withref.docx"
        new_doc(p, ["Shown before (Alpha et al., 2022).", "References",
                    "Alpha A, Zed B. First synthetic title. J Synth. 2022;1:1-2."])
        monkeypatch.setattr(unify.refresolve, "resolve_reference", lambda t, **k: {
            "input": t, "confidence": "high", "source": "crossref", "candidates": [],
            "identifiers": {}, "metadata": {"doi": "10.5555/alpha.2022",
                                            "title": "First synthetic title",
                                            "authors": [{"family": "Alpha"}], "year": "2022"}})
        plan = unify.plan_unification(p, fetch=False)
        assert plan["auto"] == [0]
        out = tmp_path / "o.docx"
        rep = unify.apply_unification(p, plan, {"intext_keys": {"0": "GAMMAKY2"}}, out=out)
        assert [_uris(f) for f in _fields(out)] == [[U + "GAMMAKY2"]]
        assert rep["replaced"] == 1


# ===========================================================================
# Validation fails closed: nothing written, nothing created
# ===========================================================================

class TestIntextKeysValidation:
    @pytest.mark.parametrize("keys, needle", [
        ({"99": "10.5555/alpha.2022"}, "index 99"),
        ({"(Nobody et al., 1999)": "10.5555/alpha.2022"}, "no in-text marker with text"),
        ({NARR: "not-an-identifier"}, "malformed identifier"),
        ({NARR: "abcd1234"}, "malformed identifier"),           # lowercase: not a key
        ({NARR: 31415926}, "malformed identifier"),             # not a string
        ({NARR: "1234567890"}, "malformed identifier"),         # too long for a PMID
        ({GROUP: "10.5555/alpha.2022"}, "cites 2 item(s)"),     # group needs a list
        ({GROUP: ["10.5555/alpha.2022"]}, "cites 2 item(s)"),
        ({NARR: ["GAMMAKY2", "ALPHAKEY"]}, "cites 1 item(s)"),
        ({NARR: "ZZZZZZZ9"}, "not in the group library"),
        # one valid entry does not let an invalid one through
        ({PAGAN: "10.5555/pagan.2021", NARR: "bogus"}, "malformed identifier"),
    ])
    def test_invalid_entry_writes_nothing(self, doc, net, tmp_path, monkeypatch, keys, needle):
        replaced = []
        monkeypatch.setattr(unify.zoterofield, "replace_text_with_zotero_field",
                            lambda *a, **k: replaced.append(a))
        plan = _plan(doc)
        before = doc.read_bytes()
        out = tmp_path / "o.docx"
        with pytest.raises(unify.IntextKeysError) as ei:
            unify.apply_unification(doc, plan, {"intext_keys": keys}, out=out)
        assert needle in str(ei.value)
        assert re.search(r"marker \d+|index \d+|text ", str(ei.value))
        assert not out.exists()
        assert doc.read_bytes() == before
        assert net["create"] == [] and net["doi_fetch"] == [] and net["pmid_fetch"] == []
        assert replaced == []

    def test_not_an_object(self, doc, net, tmp_path):
        with pytest.raises(unify.IntextKeysError):
            unify.apply_unification(doc, _plan(doc), {"intext_keys": [NARR]},
                                    out=tmp_path / "o.docx")
        assert not (tmp_path / "o.docx").exists()

    def test_same_index_twice(self, doc, net, tmp_path):
        plan = _plan(doc)
        idx = _index_of(plan, NARR)
        with pytest.raises(unify.IntextKeysError, match="more than once"):
            unify.apply_unification(doc, plan, {"intext_keys": {idx: "GAMMAKY2",
                                                                 str(idx): "GAMMAKY2"}},
                                    out=tmp_path / "o.docx")

    def test_marker_also_handled_by_accept(self, tmp_path, net, monkeypatch):
        p = tmp_path / "withref.docx"
        new_doc(p, ["Shown before (Alpha et al., 2022).", "References",
                    "Alpha A, Zed B. First synthetic title. J Synth. 2022;1:1-2."])
        monkeypatch.setattr(unify.refresolve, "resolve_reference", lambda t, **k: {
            "input": t, "confidence": "medium", "source": "crossref", "candidates": [],
            "identifiers": {}, "metadata": {"doi": "10.5555/alpha.2022",
                                            "title": "First synthetic title",
                                            "authors": [{"family": "Alpha"}], "year": "2022"}})
        plan = unify.plan_unification(p, fetch=False)
        assert plan["references"][0]["intext_links"] == [0]
        before = p.read_bytes()
        with pytest.raises(unify.IntextKeysError, match=r"marker 0 .*decisions\['accept'\]"):
            unify.apply_unification(p, plan, {"accept": [0], "intext_keys": {"0": "GAMMAKY2"}},
                                    out=tmp_path / "o.docx")
        assert p.read_bytes() == before and net["create"] == []

    def test_works_against_an_old_plan_without_intext(self, doc, net, tmp_path):
        """A plan saved before 'intext' existed: validation falls back to
        unlinked_intext (every marker here is unlinked)."""
        plan = _plan(doc)
        del plan["intext"]
        out = tmp_path / "o.docx"
        rep = unify.apply_unification(doc, plan, {"intext_keys": {NARR: "GAMMAKY2"}}, out=out)
        assert rep["replaced"] == 1


# ===========================================================================
# Degraded library read: no create (fail closed)
# ===========================================================================

class TestIntextKeysDegradedRead:
    def test_unavailable_library_never_creates(self, doc, net, tmp_path, monkeypatch):
        def _down(**kw):
            raise zotero.LibraryUnavailableError("synthetic outage")
        monkeypatch.setattr(zotero, "library_index", _down)
        plan = _plan(doc)
        out = tmp_path / "o.docx"
        rep = unify.apply_unification(
            doc, plan, {"intext_keys": {PAGAN: "10.5555/pagan.2021", NARR: "GAMMAKY2"}},
            out=out)
        assert net["create"] == [] and net["doi_fetch"] == []
        reasons = " | ".join(n["reason"] for n in rep["needs_input"])
        assert "refusing to create" in reasons          # the missing DOI
        assert "cannot confirm this item key" in reasons  # the unverifiable key
        assert _fields(out) == []
        assert sorted(u["marker"] for u in rep["unresolved_in_doc"]) == sorted([NARR, PAGAN])

    def test_degraded_status_threaded_to_create_items(self, doc, net, tmp_path, monkeypatch):
        monkeypatch.setattr(zotero, "library_index_status",
                            lambda **kw: (json.loads(json.dumps(LIB)), {"degraded": True}))
        plan = _plan(doc)
        unify.apply_unification(doc, plan, {"intext_keys": {PAGAN: "10.5555/pagan.2021"}},
                                out=tmp_path / "o.docx")
        (call,) = net["create"]
        assert call["index_degraded"] is True


# ===========================================================================
# Grouped author-year apply: suffixes, collapse, year-only chunks
# ===========================================================================

def _ref_meta(title):
    slug = title.split()[0].lower()
    return {"doi": f"10.5555/{slug}", "title": title,
            "authors": [{"family": "Quorn", "given": "A"}], "year": "2020",
            "journal": "J Synth", "type": "article-journal"}


@pytest.fixture()
def reflist_net(monkeypatch):
    """Resolve every reflist entry HIGH to metadata keyed by its title's first
    word; every DOI is in the library (KEY<WORD>). Records field insertions."""
    def _resolve(text, *, fetch=True):
        m = re.search(r"Quorn,? [AB]\.(?: \(\d{4}[a-z]?\)\.)? (\w+) synthetic title", text)
        meta = _ref_meta(m.group(1) + " synthetic title") if m else None
        return {"input": text, "metadata": meta, "confidence": "high" if meta else "none",
                "source": "crossref", "candidates": [], "identifiers": {}}
    monkeypatch.setattr(unify.refresolve, "resolve_reference", _resolve)
    monkeypatch.setattr(unify.citecheck, "ensure_retraction_db", lambda **kw: (None, None))
    monkeypatch.setattr(zotero, "library_index", lambda **kw: {
        "doi": {f"10.5555/{w}": f"KEY{w.upper()}"[:8]
                for w in ("sixth", "seventh", "eighth", "ninth")},
        "pmid": {}, "title": {}})
    monkeypatch.setattr(zotero, "create_items",
                        lambda *a, **k: pytest.fail("no create expected"))
    calls = []
    monkeypatch.setattr(unify.zoterofield, "replace_text_with_zotero_field",
                        lambda doc, anchor, keys, **k: calls.append((anchor, list(keys))) or doc)
    monkeypatch.setattr(zotero, "item_uri", lambda k: U + k)
    return calls


def _reflist_doc(tmp_path, marker, refs):
    p = tmp_path / "suffix.docx"
    new_doc(p, [f"Effects were replicated {marker}.", "References", *refs])
    return p


# APA-style suffixed entries (the reflist detector accepts "(2020a)").
SIXTH_A = "Quorn, A. (2020a). Sixth synthetic title. J Synth, 6, 1-2."
SEVENTH_B = "Quorn, A. (2020b). Seventh synthetic title. J Synth, 7, 3-4."
SIXTH = "Quorn A. Sixth synthetic title. J Synth. 2020;6:1-2."
SEVENTH = "Quorn B. Seventh synthetic title. J Synth. 2020;7:3-4."
EIGHTH_2019 = "Quorn A. Eighth synthetic title. J Synth. 2019;8:1-2."
NINTH_2020 = "Quorn A. Ninth synthetic title. J Synth. 2020;9:3-4."


class TestGroupedAuthorYearApply:
    def test_suffix_selects_the_right_reference(self, tmp_path, reflist_net):
        p = _reflist_doc(tmp_path, "(Quorn, 2020b)", [SIXTH_A, SEVENTH_B])
        plan = unify.plan_unification(p, fetch=False)
        rep = unify.apply_unification(p, plan, {}, out=tmp_path / "o.docx")
        assert reflist_net == [("(Quorn, 2020b)", ["KEYSEVEN"])]
        assert rep["unresolved_in_doc"] == []

    def test_suffixed_group_cites_both_in_order(self, tmp_path, reflist_net):
        marker = "(Quorn, 2020b; Quorn, 2020a)"
        p = _reflist_doc(tmp_path, marker, [SIXTH_A, SEVENTH_B])
        plan = unify.plan_unification(p, fetch=False)
        unify.apply_unification(p, plan, {}, out=tmp_path / "o.docx")
        assert reflist_net == [(marker, ["KEYSEVEN", "KEYSIXTH"])]

    def test_ambiguous_twin_left_unlinked_never_first_match(self, tmp_path, reflist_net):
        p = _reflist_doc(tmp_path, "(Quorn, 2020)", [SIXTH, SEVENTH])
        plan = unify.plan_unification(p, fetch=False)
        assert [m["text"] for m in plan["unlinked_intext"]] == ["(Quorn, 2020)"]
        unify.apply_unification(p, plan, {}, out=tmp_path / "o.docx")
        assert reflist_net == []

    def test_two_items_on_one_reference_left_plain_and_reported(self, tmp_path, reflist_net):
        """'(Quorn, 2020a; Quorn, 2020b)' against ONE unsuffixed Quorn-2020 entry:
        both items link to it. Citing it once would drop a printed work, so the
        marker stays plain and is reported (it used to collapse silently)."""
        marker = "(Quorn, 2020a; Quorn, 2020b)"
        p = _reflist_doc(tmp_path, marker, [SIXTH])
        plan = unify.plan_unification(p, fetch=False)
        rep = unify.apply_unification(p, plan, {}, out=tmp_path / "o.docx")
        assert reflist_net == []
        [u] = rep["unresolved_in_doc"]
        assert u["marker"] == marker and "same reference" in u["reason"]

    def test_year_only_chunk_cites_both_works(self, tmp_path, reflist_net):
        marker = "(Quorn, 2019; 2020)"
        p = _reflist_doc(tmp_path, marker, [EIGHTH_2019, NINTH_2020])
        plan = unify.plan_unification(p, fetch=False)
        assert plan["intext"][0]["n_items"] == 2
        unify.apply_unification(p, plan, {}, out=tmp_path / "o.docx")
        assert reflist_net == [(marker, ["KEYEIGHT", "KEYNINTH"])]

    def test_year_only_chunk_without_its_reference_left_plain(self, tmp_path, reflist_net):
        """The 2020 work has no entry: the group must not be cited as 2019 alone."""
        marker = "(Quorn, 2019; 2020)"
        p = _reflist_doc(tmp_path, marker, [EIGHTH_2019])
        plan = unify.plan_unification(p, fetch=False)
        rep = unify.apply_unification(p, plan, {}, out=tmp_path / "o.docx")
        assert reflist_net == []
        assert [u["marker"] for u in rep["unresolved_in_doc"]] == [marker]

    def test_year_only_item_needs_two_intext_keys(self, doc, net, tmp_path, monkeypatch):
        p = tmp_path / "yo.docx"
        new_doc(p, ["Shown twice (Alpha, 2022; 2023)."])
        plan = _plan(p)
        with pytest.raises(unify.IntextKeysError, match="cites 2 item"):
            unify.apply_unification(p, plan, {"intext_keys": {"0": "ALPHAKEY"}},
                                    out=tmp_path / "o.docx")


# ===========================================================================
# PubMed candidates — bounded, confirm-only, offline-safe
# ===========================================================================

def _pubmed(monkeypatch, hits):
    """esearch returns ``hits[term]`` (default 2 synthetic PMIDs); efetch
    returns a synthetic record per PMID."""
    calls = {"esearch": [], "efetch": []}

    def _esearch(term, *, retmax=20, **kw):
        calls["esearch"].append((term, retmax))
        if term in hits:
            return hits[term]
        return ["90000001", "90000002"], {"degraded": False, "reason": None}

    def _efetch(pmids):
        calls["efetch"].append(list(pmids))
        return ({p: {"title": f"Synthetic record {p}", "journal": "J Synth",
                     "year": "2022", "doi": f"10.5555/rec.{p}",
                     "authors": ["Alpha A"],
                     "authors_structured": [{"family": "Alpha", "given": "A"}]}
                 for p in pmids}, {"degraded": False})
    monkeypatch.setattr(unify.entrez, "esearch_pmids_status", _esearch)
    monkeypatch.setattr(unify.entrez, "efetch_pubmed_status", _efetch)
    return calls


class TestPubMedCandidates:
    def test_candidates_attached_per_unlinked_item(self, doc, net, monkeypatch):
        calls = _pubmed(monkeypatch, {})
        plan = unify.plan_unification(doc, fetch=True)
        group = plan["intext_candidates"][str(_index_of(plan, GROUP))]
        assert [(e["item"], e["surname"], e["year"]) for e in group] == [
            (0, "Alpha", "2022"), (1, "Beta", "2012")]
        c = group[0]["candidates"][0]
        assert c == {"pmid": "90000001", "doi": "10.5555/rec.90000001",
                     "title": "Synthetic record 90000001", "first_author": "Alpha",
                     "year": "2022", "journal": "J Synth"}
        assert group[0]["status"] == "ok"
        assert ("Alpha[1au] AND 2022[dp]", 5) in calls["esearch"]
        assert plan["summary"]["intext_candidates"]["queries"] == 4

    def test_candidates_are_confirm_only(self, doc, net, monkeypatch, tmp_path):
        _pubmed(monkeypatch, {})
        plan = unify.plan_unification(doc, fetch=True)
        assert plan["intext_candidates"]
        out = tmp_path / "o.docx"
        rep = unify.apply_unification(doc, plan, {}, out=out)
        assert _fields(out) == []
        assert rep["replaced"] == 0 and rep["added"] == [] and rep["matched"] == []
        assert net["create"] == [] and net["doi_fetch"] == [] and net["pmid_fetch"] == []

    def test_copying_a_candidate_into_intext_keys_applies_it(self, doc, net, monkeypatch, tmp_path):
        _pubmed(monkeypatch, {})
        plan = unify.plan_unification(doc, fetch=True)
        cand = plan["intext_candidates"][str(_index_of(plan, NARR))][0]["candidates"][0]
        monkeypatch.setattr(zotero, "library_index", lambda **kw: {
            "doi": {cand["doi"]: "CANDKEY1"}, "pmid": {}, "title": {}})
        out = tmp_path / "o.docx"
        unify.apply_unification(doc, plan, {"intext_keys": {NARR: cand["doi"]}}, out=out)
        assert [_uris(f) for f in _fields(out)] == [[U + "CANDKEY1"]]

    def test_undated_item_not_queried(self, tmp_path, net, monkeypatch):
        calls = _pubmed(monkeypatch, {})
        p = tmp_path / "undated.docx"
        new_doc(p, ["Shown (Alpha et al., 2022; Beta et al., n.d.)."])
        plan = unify.plan_unification(p, fetch=True)
        assert [t for t, _ in calls["esearch"]] == ["Alpha[1au] AND 2022[dp]"]
        assert [e["status"] for e in plan["intext_candidates"]["0"]] == ["ok", "no_year"]
        assert plan["intext"][0]["n_items"] == 2

    def test_suffixed_items_query_by_year_digits_once(self, tmp_path, net, monkeypatch):
        """PubMed's [dp] has no "2020a": both suffixed items share ONE digits query."""
        calls = _pubmed(monkeypatch, {})
        p = tmp_path / "suffixed.docx"
        new_doc(p, ["Shown (Quorn, 2020a; Quorn, 2020b)."])
        plan = unify.plan_unification(p, fetch=True)
        assert [t for t, _ in calls["esearch"]] == ["Quorn[1au] AND 2020[dp]"]
        assert [(e["year"], e["status"]) for e in plan["intext_candidates"]["0"]] == [
            ("2020a", "ok"), ("2020b", "ok")]

    def test_year_only_item_queried_under_inherited_author(self, tmp_path, net, monkeypatch):
        calls = _pubmed(monkeypatch, {})
        p = tmp_path / "noauthor.docx"
        new_doc(p, ["Shown (Quorn, 2019; 2020; Brask, 2018)."])
        plan = unify.plan_unification(p, fetch=True)
        assert [e["surname"] for e in plan["intext_candidates"]["0"]] == ["Quorn", "Quorn", "Brask"]
        assert len(calls["esearch"]) == 3

    def test_two_word_surname_is_quoted(self, net, monkeypatch):
        calls = _pubmed(monkeypatch, {})
        cands, status = unify._pubmed_author_year_query("Ó Briain", "2018")
        assert calls["esearch"] == [('"Ó Briain"[1au] AND 2018[dp]', 5)]
        assert status == "ok" and len(cands) == 2

    def test_query_cap_and_dedupe(self, tmp_path, net, monkeypatch):
        calls = _pubmed(monkeypatch, {})
        p = tmp_path / "many.docx"
        new_doc(p, [
            "One (Alpha et al., 2022).", "Two (Alpha et al., 2022).",     # same item
            "Three (Beta, 2012).", "Four (Gamma, 2019).", "Five (Delta, 2018).",
        ])
        plan = unify.plan_unification(p, fetch=True, max_candidate_queries=2)
        assert len(calls["esearch"]) == 2
        statuses = [e["status"] for lst in plan["intext_candidates"].values() for e in lst]
        assert statuses == ["ok", "ok", "ok", "not_queried_cap", "not_queried_cap"]
        s = plan["summary"]["intext_candidates"]
        assert s == {"items": 5, "queries": 2, "query_cap": 2, "items_capped": 2,
                     "degraded": 0}

    def test_search_failure_recorded_never_raised(self, doc, net, monkeypatch):
        def _boom(term, **kw):
            raise RuntimeError("synthetic outage")
        monkeypatch.setattr(unify.entrez, "esearch_pmids_status", _boom)
        plan = unify.plan_unification(doc, fetch=True)
        statuses = {e["status"] for lst in plan["intext_candidates"].values() for e in lst}
        assert statuses == {"error"}
        assert plan["summary"]["intext_candidates"]["degraded"] == 4

    def test_degraded_search_status(self, doc, net, monkeypatch):
        monkeypatch.setattr(unify.entrez, "esearch_pmids_status",
                            lambda term, **kw: ([], {"degraded": True, "reason": "HTTP 503"}))
        plan = unify.plan_unification(doc, fetch=True)
        statuses = {e["status"] for lst in plan["intext_candidates"].values() for e in lst}
        assert statuses == {"degraded"}

    def test_linked_items_are_not_queried(self, tmp_path, net, monkeypatch):
        calls = _pubmed(monkeypatch, {})
        monkeypatch.setattr(unify.refresolve, "resolve_reference", lambda t, **k: {
            "input": t, "confidence": "none", "source": None, "candidates": [],
            "identifiers": {}, "metadata": None})
        p = tmp_path / "half.docx"
        new_doc(p, ["Shown (Alpha et al., 2022; Beta, 2012).", "References",
                    "Alpha A, Zed B. First synthetic title. J Synth. 2022;1:1-2."])
        plan = unify.plan_unification(p, fetch=True)
        assert [t for t, _ in calls["esearch"]] == ["Beta[1au] AND 2012[dp]"]
        (entry,) = plan["intext_candidates"]["0"]
        assert entry["item"] == 1

    @pytest.mark.parametrize("kw", [{"offline": True}, {"fetch": False}])
    def test_offline_and_no_fetch_make_no_calls(self, doc, monkeypatch, kw):
        monkeypatch.setattr(unify.citecheck, "ensure_retraction_db", lambda **k: (None, None))
        monkeypatch.setattr(zotero, "library_index", lambda **k: {"doi": {}, "pmid": {}, "title": {}})

        def _forbidden(*a, **k):
            raise AssertionError("network call made under offline/no-fetch")
        for name in ("esearch_pmids_status", "esearch_pmids", "efetch_pubmed_status",
                     "efetch_pubmed", "_http_get"):
            monkeypatch.setattr(unify.entrez, name, _forbidden)
        monkeypatch.setattr(unify.refresolve, "_crossref_doi_fetch", _forbidden)
        plan = unify.plan_unification(doc, **kw)
        assert plan["intext_candidates"] == {}
        assert "intext_candidates" not in plan["summary"]


def test_plan_documents_the_decisions_shape(doc, net):
    plan = _plan(doc)
    t = plan["decisions_template"]
    assert set(t) >= {"accept", "placeholder_resolutions", "intext_keys", "add_missing", "_help"}
    assert "Zotero item key" in t["_help"]["intext_keys"]
    by_text = {m["text"]: m for m in plan["intext"]}
    assert by_text[GROUP]["n_items"] == 2
    assert [it["ref_index"] for it in by_text[GROUP]["items"]] == [None, None]


@pytest.mark.parametrize("raw, parsed", [
    ("10.5555/X.1", ("doi", "10.5555/x.1")),
    ("doi:10.5555/x.1", ("doi", "10.5555/x.1")),
    ("https://doi.org/10.5555/x.1.", ("doi", "10.5555/x.1")),
    ("PMID:31415926", ("pmid", "31415926")),
    ("pmid 31415926", ("pmid", "31415926")),
    ("31415926", ("pmid", "31415926")),       # all digits: always a PMID
    ("ABCD2345", ("key", "ABCD2345")),
])
def test_parse_identifier(raw, parsed):
    assert unify._parse_intext_identifier(raw) == parsed


def test_cli_bad_intext_keys_exits_2_nothing_written(doc, net, tmp_path, monkeypatch, capsys):
    """unify-refs --apply with a bad intext_keys entry: clean one-line error,
    exit 2, no output file, no create — and the re-plan asks PubMed nothing."""
    from zoterocite import cli
    out = tmp_path / "o.docx"
    dec = tmp_path / "d.json"
    dec.write_text(json.dumps({"intext_keys": {NARR: "not-an-identifier"}}), encoding="utf-8")
    with pytest.raises(SystemExit) as ei:
        cli.main(["unify-refs", str(doc), "--apply", "--decisions", f"@{dec}",
                  "-o", str(out)])
    assert ei.value.code == 2
    err = capsys.readouterr().err
    assert "malformed identifier" in err and "nothing written" in err
    assert not out.exists() and net["create"] == [] and net["esearch"] == []
