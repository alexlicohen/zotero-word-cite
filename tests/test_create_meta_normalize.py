"""Create-path metadata hygiene + PubMed author reconciliation (feedback #22).

* ``refresolve.normalize_create_meta`` is the single owner: entities and inline
  markup out of title / container title, ALL-CAPS personal names recased
  (initials, particles, hyphens, apostrophes, mixed case and corporate authors
  left alone), bare author strings structured.
* ``zotero.create_items`` applies it BEFORE the write-time dedup lookups, so the
  dedup title and the posted title agree.
* ``create_items(verify_authors=True)`` runs ``refresolve.reconcile_create_authors``
  over ONLY the items about to be posted: one DOI->PMID ESearch + one EFetch
  batch; PubMed's list wins over a shorter / ALL-CAPS / mismatched one; any
  failure creates exactly as before with the item flagged ``authors_unverified``.
* ``refresolve._pubmed_fetch`` returns structured ``{family, given}`` authors
  (the bare strings were dropped by ``csljson_to_zotero_item``: no creators).

Synthetic names, titles and identifiers only (DOIs under 10.5555/, invented
PMIDs). Every network seam is monkeypatched; no Zotero writes.
"""
from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

import pytest

from zoterocite import entrez, new_doc, refresolve, unify, zotero


# ---------------------------------------------------------------------------
# Synthetic PubMed (EFetch XML / ESearch JSON) served at entrez._http_get
# ---------------------------------------------------------------------------

def _article(pmid, doi, authors, title="Synthetic replication study"):
    auth = "".join(
        f"<Author><LastName>{f}</LastName><ForeName>{g}</ForeName>"
        f"<Initials>{g[:1]}</Initials></Author>" if g else
        f"<Author><CollectiveName>{f}</CollectiveName></Author>"
        for f, g in authors)
    ids = f'<ArticleId IdType="doi">{doi}</ArticleId>' if doi else ""
    return (f"<PubmedArticle><MedlineCitation><PMID>{pmid}</PMID><Article>"
            f"<Journal><JournalIssue><PubDate><Year>2021</Year></PubDate></JournalIssue>"
            f"<Title>Journal of Synthetic Results</Title></Journal>"
            f"<ArticleTitle>{title}</ArticleTitle><AuthorList>{auth}</AuthorList>"
            f"</Article></MedlineCitation><PubmedData><ArticleIdList>{ids}"
            f"</ArticleIdList></PubmedData></PubmedArticle>")


class FakePubMed:
    """``entrez._http_get`` stand-in. ``records`` = {pmid: (doi, authors)};
    ``down`` makes every call fail (None, like a network error)."""

    def __init__(self, records=None, down=False):
        self.records = records or {}
        self.down = down
        self.calls = []          # ("esearch", term) / ("efetch", [pmids])

    def __call__(self, url, timeout=None, *, cache_ttl=None):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        if "esearch" in url:
            term = q["term"][0]
            self.calls.append(("esearch", term))
            if self.down:
                return None
            ids = [p for p, (doi, _a) in self.records.items()   # case-insensitive
                   if doi and f'"{doi.lower()}"[doi]' in term.lower()]
            return json.dumps({"esearchresult": {"idlist": ids}}).encode()
        if "efetch" in url:
            ids = q["id"][0].split(",")
            self.calls.append(("efetch", ids))
            if self.down:
                return None
            body = "".join(_article(p, *self.records[p]) for p in ids if p in self.records)
            return f"<PubmedArticleSet>{body}</PubmedArticleSet>".encode()
        raise AssertionError(f"unexpected URL {url}")

    def kinds(self):
        return [k for k, _ in self.calls]


@pytest.fixture()
def pubmed(monkeypatch):
    fake = FakePubMed()
    monkeypatch.setattr(entrez, "_http_get", fake)
    monkeypatch.setattr(entrez, "_sleep_between_batches", lambda: None)
    return fake


@pytest.fixture()
def zot(monkeypatch):
    """Real create_items with the Zotero transport faked: write access, a
    collection, a searchable library (``library`` list) and a recording POST."""
    rec = {"posted": [], "searched": [], "library": []}
    monkeypatch.setattr(zotero, "key_can_write_status", lambda: True)
    monkeypatch.setattr(zotero, "ensure_collection", lambda name: "COLLKEY1")

    def _search(query, qmode=None):
        rec["searched"].append(query)
        return list(rec["library"])

    def _post(path, body, **kw):
        assert path == "items"
        rec["posted"].extend(body)
        return {"successful": {str(i): {"key": f"NEWKEY{i:02d}"} for i in range(len(body))},
                "unchanged": {}, "failed": {}}

    def _no_fetch_all(*a, **k):
        raise AssertionError("fetch_all must not run (doi_index is supplied)")

    monkeypatch.setattr(zotero, "search_items", _search)
    monkeypatch.setattr(zotero, "_post_json", _post)
    monkeypatch.setattr(zotero, "fetch_all", _no_fetch_all)
    return rec


def _creators(item):
    return [(c["lastName"], c["firstName"]) for c in item["creators"]]


# ===========================================================================
# 1. Name casing (normalize_create_meta authors)
# ===========================================================================

@pytest.mark.parametrize("family,given,want", [
    ("SMITH", "JOHN", ("Smith", "John")),
    ("SMITH", "J.A.", ("Smith", "J.A.")),            # dotted initials untouched
    ("SMITH", "JA", ("Smith", "JA")),                # bare initials untouched
    ("SMITH", "JRR", ("Smith", "JRR")),              # vowel-less 3 = initials
    ("SMITH", "JOHN A.", ("Smith", "John A.")),
    ("O'BRIEN", "MARY", ("O'Brien", "Mary")),        # apostrophe
    ("D’ANGELO", "LUCA", ("D’Angelo", "Luca")),
    ("SMITH-JONES", "ANNE-MARIE", ("Smith-Jones", "Anne-Marie")),   # hyphen
    ("VAN DER BERG", "PIETER", ("van der Berg", "Pieter")),         # particles
    ("DE", "PAULA", ("De", "Paula")),                # a lone particle is the name
    ("MCDONALD", "ANN", ("McDonald", "Ann")),
    ("McDonald", "Ann", ("McDonald", "Ann")),        # already mixed: untouched
    ("DeLisi", "Lynn", ("DeLisi", "Lynn")),
    ("MacAvoy", "Kim", ("MacAvoy", "Kim")),
    ("Smith", "JOHN", ("Smith", "John")),            # each field on its own
    ("LI", "WEI", ("Li", "Wei")),
    ("MÜLLER", "JÜRGEN", ("Müller", "Jürgen")),
    ("ENIGMA", "", ("ENIGMA", "")),                  # given-less acronym
    ("ENIGMA CONSORTIUM", "", ("ENIGMA CONSORTIUM", "")),
    ("SYNTHETIC IMAGING STUDY GROUP", "", ("SYNTHETIC IMAGING STUDY GROUP", "")),
])
def test_name_casing_table(family, given, want):
    meta = {"authors": [{"family": family, "given": given}]}
    (a,) = refresolve.normalize_create_meta(meta)["authors"]
    assert (a["family"], a["given"]) == want


def test_bare_author_strings_are_structured():
    meta = {"authors": ["Okonkwo CA", "Pagán, Rosa", "Synthetic Cohort Consortium"]}
    got = refresolve.normalize_create_meta(meta)["authors"]
    assert got == [{"family": "Okonkwo", "given": "CA"},
                   {"family": "Pagán", "given": "Rosa"},
                   {"family": "Synthetic Cohort Consortium", "given": ""}]


# ===========================================================================
# 2. Entities / markup in title + container title
# ===========================================================================

@pytest.mark.parametrize("raw,want", [
    ("Fear &amp; loathing in synthetic cohorts", "Fear & loathing in synthetic cohorts"),
    ("Effects of <i>Mus synthetica</i> on CO<sub>2</sub>", "Effects of Mus synthetica on CO2"),
    ("A <jats:italic>de novo</jats:italic> variant", "A de novo variant"),
    ("&#946;-amyloid and &#x3b1;-synuclein", "β-amyloid and α-synuclein"),
    ("Risk p &lt; 0.05 &gt; chance", "Risk p < 0.05 > chance"),   # real '<' survives
    ("Double &amp;amp; escaped", "Double & escaped"),
    ("&lt;i&gt;Escaped&lt;/i&gt; markup", "Escaped markup"),
    ("SHOUTED TITLE STAYS SHOUTED", "SHOUTED TITLE STAYS SHOUTED"),  # no title-casing
    ("  spaced \n  out  ", "spaced out"),
])
def test_title_cleanup(raw, want):
    assert refresolve.normalize_create_meta({"title": raw})["title"] == want


def test_container_title_cleaned_other_fields_untouched():
    meta = {"title": "T", "journal": "Brain &amp; <i>Synthetic</i> Behavior",
            "doi": "10.5555/A&amp;B", "pmid": "27182818", "year": "2021",
            "volume": "<i>3</i>"}
    out = refresolve.normalize_create_meta(meta)
    assert out["journal"] == "Brain & Synthetic Behavior"
    for k in ("doi", "pmid", "year", "volume"):
        assert out[k] == meta[k]                   # dedup keys never touched
    assert meta["journal"].startswith("Brain &amp;")   # input not mutated
    assert refresolve.normalize_create_meta(out) == out   # idempotent


# ===========================================================================
# 3. _pubmed_fetch -> structured authors -> creators (was: none)
# ===========================================================================

def test_pubmed_fetch_structured_authors_become_creators(pubmed):
    pubmed.records["27182818"] = ("10.5555/pagan.2021",
                                  [("Pagán", "Rosa"), ("Okonkwo", "Chidi")])
    meta = refresolve._pubmed_fetch("27182818")
    assert meta["authors"] == [{"family": "Pagán", "given": "Rosa"},
                               {"family": "Okonkwo", "given": "Chidi"}]
    assert meta["authors_plain"] == ["Pagán R", "Okonkwo C"]   # old shape kept
    item = zotero.csljson_to_zotero_item(meta)
    assert _creators(item) == [("Pagán", "Rosa"), ("Okonkwo", "Chidi")]


# ===========================================================================
# 4. create_items: normalisation before dedup, author reconciliation
# ===========================================================================

def _create(metas, **kw):
    kw.setdefault("doi_index", {})
    kw.setdefault("pmid_index", {})
    return zotero.create_items(metas, collection="Review", tags=["t"], **kw)


class TestNormalizedCreate:
    def test_posted_item_is_normalized_and_result_echoes_caller_title(self, zot):
        raw = {"doi": "10.5555/x.1", "title": "Fear &amp; <i>loathing</i>",
               "journal": "Brain &amp; Behavior",
               "authors": [{"family": "SMITH", "given": "JOHN"}]}
        res = _create([raw])
        (item,) = zot["posted"]
        assert item["title"] == "Fear & loathing"
        assert item["publicationTitle"] == "Brain & Behavior"
        assert _creators(item) == [("Smith", "John")]
        assert res["created"][0]["title"] == raw["title"]   # caller can map back
        assert "author_check" not in res                    # opt-in only

    def test_dedup_searches_the_normalized_title(self, zot):
        zot["library"].append({"key": "EXISTS01", "data": {
            "title": "Fear & loathing in synthetic cohorts"}})
        res = _create([{"title": "Fear &amp; loathing in <i>synthetic</i> cohorts"}])
        assert res["skipped_existing"] == [{
            "title": "Fear &amp; loathing in <i>synthetic</i> cohorts",
            "existing_key": "EXISTS01"}]
        assert res["created"] == [] and zot["posted"] == []
        assert zot["searched"][0] == "Fear & loathing in synthetic cohorts"

    def test_raw_title_of_an_earlier_unnormalized_item_still_dedups(self, zot):
        zot["library"].append({"key": "OLDRAW01", "data": {
            "title": "Fear &amp; loathing in cohorts"}})
        res = _create([{"title": "Fear &amp; loathing in cohorts"}])
        assert [s["existing_key"] for s in res["skipped_existing"]] == ["OLDRAW01"]
        assert zot["posted"] == []

    def test_index_degraded_unchanged(self, zot, pubmed):
        res = _create([{"doi": "10.5555/absent.1", "title": "Absent &amp; unknown"}],
                      index_degraded=True, verify_authors=True)
        assert zot["posted"] == [] and res["created"] == []
        assert [d["title"] for d in res["skipped_degraded_read"]] == ["Absent &amp; unknown"]
        assert res["author_check"] == [] and pubmed.calls == []

    def test_present_items_never_reach_pubmed(self, zot, pubmed):
        res = _create([{"doi": "10.5555/present.1", "title": "P"},
                       {"pmid": "11111111", "title": "Q"}],
                      doi_index={"10.5555/present.1": "HAVEDOI1"},
                      pmid_index={"11111111": "HAVEPMI1"}, verify_authors=True)
        assert [s["existing_key"] for s in res["skipped_existing"]] == ["HAVEDOI1", "HAVEPMI1"]
        assert pubmed.calls == [] and res["author_check"] == []

    def test_only_items_about_to_be_created_are_checked(self, zot, pubmed):
        pubmed.records["22222222"] = (None, [("Haddad", "Omar")])
        res = _create([{"pmid": "11111111", "title": "Present"},
                       {"pmid": "22222222", "title": "Absent"}],
                      pmid_index={"11111111": "HAVEPMI1"}, verify_authors=True)
        assert pubmed.calls == [("efetch", ["22222222"])]
        assert [c["title"] for c in res["author_check"]] == ["Absent"]
        assert [c["title"] for c in res["created"]] == ["Absent"]


class TestPubMedReconciliation:
    def test_pubmed_wins_over_short_crossref_list(self, zot, pubmed):
        pubmed.records["27182818"] = ("10.5555/pagan.2021",
                                      [("Pagán", "Rosa"), ("Okonkwo", "Chidi")])
        meta = {"doi": "10.5555/PAGAN.2021", "title": "Synthetic replication study",
                "authors": [{"family": "Pagán", "given": "Rosa"}]}
        res = _create([meta], verify_authors=True)
        (item,) = zot["posted"]
        assert _creators(item) == [("Pagán", "Rosa"), ("Okonkwo", "Chidi")]
        (chk,) = res["author_check"]
        assert chk["author_check"] == "pubmed" and chk["reason"] == "crossref_shorter"
        assert (chk["authors_before"], chk["authors_after"]) == (1, 2)
        assert chk["pmid"] == "27182818" and "authors" not in chk
        assert pubmed.kinds() == ["esearch", "efetch"]     # DOI -> PMID, then fetch
        assert "pmid" not in meta                          # caller's meta untouched

    def test_all_caps_crossref_replaced_by_pubmed(self, zot, pubmed):
        pubmed.records["22222222"] = (None, [("Okonkwo", "Chidi"), ("Lindqvist", "Sara")])
        res = _create([{"pmid": "22222222", "title": "T",
                        "authors": [{"family": "OKONKWO", "given": "CHIDI"},
                                    {"family": "LINDQVIST", "given": "SARA"}]}],
                      verify_authors=True)
        assert res["author_check"][0]["reason"] == "crossref_all_caps"
        assert _creators(zot["posted"][0]) == [("Okonkwo", "Chidi"), ("Lindqvist", "Sara")]
        assert pubmed.kinds() == ["efetch"]                 # PMID known: no ESearch

    def test_mismatch_pubmed_wins(self, zot, pubmed):
        pubmed.records["33333333"] = (None, [("Haddad", "Omar"), ("Lindqvist", "Sara")])
        res = _create([{"pmid": "33333333", "title": "T",
                        "authors": [{"family": "Haddad", "given": "Omar"},
                                    {"family": "Lundgren", "given": "Sara"}]}],
                      verify_authors=True)
        assert res["author_check"][0]["reason"] == "mismatch"
        assert _creators(zot["posted"][0])[1] == ("Lindqvist", "Sara")

    def test_agreeing_lists_kept_and_confirmed(self, zot, pubmed):
        pubmed.records["44444444"] = (None, [("Haddad", "O")])
        res = _create([{"pmid": "44444444", "title": "T",
                        "authors": [{"family": "Haddad", "given": "Omar"}]}],
                      verify_authors=True)
        assert res["author_check"][0]["author_check"] == "confirmed"
        assert _creators(zot["posted"][0]) == [("Haddad", "Omar")]   # Crossref kept

    def test_shorter_pubmed_list_never_drops_names(self, zot, pubmed):
        pubmed.records["55555555"] = (None, [("Haddad", "Omar")])
        res = _create([{"pmid": "55555555", "title": "T",
                        "authors": [{"family": "Haddad", "given": "Omar"},
                                    {"family": "Lindqvist", "given": "Sara"}]}],
                      verify_authors=True)
        chk = res["author_check"][0]
        assert (chk["author_check"], chk["reason"]) == ("authors_unverified", "pubmed_shorter")
        assert len(zot["posted"][0]["creators"]) == 2

    def test_pubmed_unavailable_creates_as_today_flagged(self, zot, pubmed):
        pubmed.down = True
        meta = {"doi": "10.5555/down.1", "title": "T",
                "authors": [{"family": "Haddad", "given": "Omar"}]}
        res = _create([meta], verify_authors=True)
        assert [c["title"] for c in res["created"]] == ["T"]
        assert _creators(zot["posted"][0]) == [("Haddad", "Omar")]
        chk = res["author_check"][0]
        assert (chk["author_check"], chk["reason"]) == ("authors_unverified", "pubmed_unavailable")

    def test_no_identifier_makes_no_call(self, zot, pubmed):
        res = _create([{"title": "Only a title", "authors": []}], verify_authors=True)
        assert res["author_check"][0]["reason"] == "no_pmid"
        assert pubmed.calls == [] and len(zot["posted"]) == 1

    def test_pmid_doi_disagreement_is_not_trusted(self, zot, pubmed):
        pubmed.records["66666666"] = ("10.5555/other.work", [("Haddad", "Omar"), ("Lee", "Ana")])
        res = _create([{"pmid": "66666666", "doi": "10.5555/this.work", "title": "T",
                        "authors": [{"family": "Haddad", "given": "Omar"}]}],
                      verify_authors=True)
        assert res["author_check"][0]["reason"] == "pmid_doi_disagree"
        assert len(zot["posted"][0]["creators"]) == 1

    def test_reconcile_error_never_blocks(self, zot, monkeypatch):
        def _boom(metas, **kw):
            raise RuntimeError("synthetic")
        monkeypatch.setattr(refresolve, "reconcile_create_authors", _boom)
        res = _create([{"doi": "10.5555/e.1", "title": "T"}], verify_authors=True)
        assert len(res["created"]) == 1
        assert res["author_check"][0]["author_check"] == "authors_unverified"


class TestBounds:
    def test_one_esearch_one_efetch_per_call(self, pubmed):
        for i in range(5):
            pubmed.records[f"7000000{i}"] = (f"10.5555/b.{i}", [("Haddad", "Omar")])
        metas = ([{"doi": f"10.5555/b.{i}", "title": f"T{i}"} for i in range(3)]
                 + [{"pmid": f"7000000{i}", "title": f"T{i}"} for i in (3, 4)])
        out = refresolve.reconcile_create_authors(metas)
        assert pubmed.kinds() == ["esearch", "efetch"]
        assert all(c["author_check"] == "pubmed" for c in out)   # no Crossref authors
        assert [c["pmid"] for c in out] == [f"7000000{i}" for i in range(5)]

    def test_fetch_false_makes_no_call(self, pubmed):
        out = refresolve.reconcile_create_authors([{"doi": "10.5555/b.0"}], fetch=False)
        assert pubmed.calls == [] and out[0]["reason"] == "offline"

    def test_cap(self, pubmed, monkeypatch):
        monkeypatch.setattr(refresolve, "_AUTHOR_CHECK_MAX", 2)
        out = refresolve.reconcile_create_authors(
            [{"pmid": str(80000000 + i)} for i in range(3)])
        assert out[2]["reason"] == "over_cap"
        assert pubmed.calls[-1] == ("efetch", ["80000000", "80000001"])


# ===========================================================================
# 5. End to end: unify intext_keys PMID path -> created WITH creators
# ===========================================================================

def test_unify_pmid_intext_key_creates_item_with_authors(tmp_path, zot, pubmed, monkeypatch):
    marker = "(Pagán et al., 2021)"
    doc = tmp_path / "draft.docx"
    new_doc(doc, [f"A follow-up confirmed the effect {marker}."])
    pubmed.records["27182818"] = ("10.5555/pagan.2021",
                                  [("Pagán", "Rosa"), ("Okonkwo", "Chidi")])
    monkeypatch.setattr(zotero, "library_index",
                        lambda **kw: {"doi": {}, "pmid": {}, "title": {}})
    monkeypatch.setattr(unify.citecheck, "ensure_retraction_db", lambda **kw: (None, None))
    monkeypatch.setattr(zotero, "item_uri", lambda k: "http://zotero.org/groups/5555/items/" + k)
    plan = unify.plan_unification(doc, fetch=False)
    rep = unify.apply_unification(doc, plan, {"intext_keys": {marker: "PMID:27182818"}},
                                  out=tmp_path / "out.docx")
    (item,) = zot["posted"]
    assert _creators(item) == [("Pagán", "Rosa"), ("Okonkwo", "Chidi")]
    assert [a["key"] for a in rep["added"]] == ["NEWKEY00"]
    (chk,) = rep["author_check"]
    assert chk["author_check"] == "confirmed" and chk["pmid"] == "27182818"
