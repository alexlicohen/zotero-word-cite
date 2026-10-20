"""Crossref group/consortium authors survive item creation, in author order.

Crossref carries a group author as ``{"name": ..., "sequence": ...}`` with no
family/given. It used to become ``{"family": "", "given": ""}``, which
``zotero.csljson_to_zotero_item`` skipped, so the created item lost it.

* ``refresolve._parse_crossref_item``: a name-only author -> CSL ``{"literal"}``.
* ``zotero.csljson_to_zotero_item``: ``{"literal"}`` -> Zotero single-field
  ``{"creatorType": "author", "name"}``; two-field creators unchanged.
* ``refresolve.normalize_create_meta``: a literal is never recased or split.
* ``refresolve.reconcile_create_authors``: a group author is not a person; a
  PubMed ``<CollectiveName>`` of the same normalised text is the same author;
  when PubMed's list wins, a group PubMed lacks is kept in place
  (``groups_kept``), never dropped.
* ``unify._itemdata_from_meta`` / ``endnote._meta_to_zotero_meta`` carry it.

Synthetic names, titles and identifiers only; every network seam is faked.
"""
from __future__ import annotations

import json

import pytest

from zoterocite import endnote, refresolve, unify, zotero

from test_create_meta_normalize import FakePubMed, _create, pubmed, zot  # noqa: F401

GROUP = "The Synthetic Example Consortium"

CROSSREF_WORK = {
    "DOI": "10.5555/group.2022",
    "title": ["A synthetic consortium paper"],
    "author": [
        {"given": "Rosa", "family": "Pagán", "sequence": "first"},
        {"name": GROUP, "sequence": "additional"},
        {"given": "Chidi", "family": "Okonkwo", "sequence": "additional"},
    ],
    "issued": {"date-parts": [[2022]]},
    "container-title": ["Journal of Synthetic Results"],
    "type": "journal-article",
}


def _parsed():
    return refresolve._parse_crossref_item(json.loads(json.dumps(CROSSREF_WORK)))


def _creators(item):
    return [c.get("name") or (c["lastName"], c["firstName"]) for c in item["creators"]]


# ---------------------------------------------------------------------------
# 1. Crossref parse -> meta
# ---------------------------------------------------------------------------

def test_crossref_group_author_becomes_literal_in_order():
    assert _parsed()["authors"] == [
        {"family": "Pagán", "given": "Rosa"},
        {"literal": GROUP},
        {"family": "Okonkwo", "given": "Chidi"},
    ]


def test_crossref_doi_fetch_keeps_group(monkeypatch):
    body = json.dumps({"message": CROSSREF_WORK}).encode()
    monkeypatch.setattr(refresolve._http, "http_get", lambda *a, **k: body)
    meta = refresolve._crossref_doi_fetch("10.5555/group.2022")
    assert [a.get("literal") or a["family"] for a in meta["authors"]] == \
        ["Pagán", GROUP, "Okonkwo"]


# ---------------------------------------------------------------------------
# 2. meta -> Zotero item
# ---------------------------------------------------------------------------

def test_csljson_to_zotero_item_single_field_creator_in_place():
    item = zotero.csljson_to_zotero_item(_parsed())
    assert item["creators"] == [
        {"creatorType": "author", "firstName": "Rosa", "lastName": "Pagán"},
        {"creatorType": "author", "name": GROUP},
        {"creatorType": "author", "firstName": "Chidi", "lastName": "Okonkwo"},
    ]


def test_csljson_to_zotero_item_accepts_name_key_and_skips_empty():
    item = zotero.csljson_to_zotero_item({"title": "T", "authors": [
        {"name": "Synthetic Study Group"}, {"family": "", "given": ""},
        {"literal": "  "}]})
    assert item["creators"] == [{"creatorType": "author", "name": "Synthetic Study Group"}]


def test_create_items_posts_group_creator(zot):
    res = _create([_parsed()])
    (item,) = zot["posted"]
    assert _creators(item) == [("Pagán", "Rosa"), GROUP, ("Okonkwo", "Chidi")]
    assert [c["title"] for c in res["created"]] == ["A synthetic consortium paper"]


# ---------------------------------------------------------------------------
# 3. Create-path hygiene never recases/splits a group name
# ---------------------------------------------------------------------------

def test_normalize_leaves_all_caps_literal_alone():
    meta = {"authors": [{"family": "SMITH", "given": "JOHN"},
                        {"literal": "THE EXAMPLE STUDY GROUP"},
                        {"literal": "Fish &amp; Fowl Consortium"}]}
    got = refresolve.normalize_create_meta(meta)["authors"]
    assert got == [{"family": "Smith", "given": "John"},
                   {"literal": "THE EXAMPLE STUDY GROUP"},
                   {"literal": "Fish & Fowl Consortium"}]
    assert refresolve.normalize_create_meta({"authors": got})["authors"] == got
    # Empty family/given keys alongside the literal: still the literal branch.
    (b,) = refresolve.normalize_create_meta({"authors": [
        {"family": "", "given": "", "literal": "Fish &amp; Fowl Consortium"}]})["authors"]
    assert b["literal"] == "Fish & Fowl Consortium"


def test_bare_collective_string_is_not_split_into_initials():
    # The trailing all-caps token looks like PubMed initials ("ABC").
    (a,) = refresolve.normalize_create_meta(
        {"authors": ["EXAMPLE CONSORTIUM ABC"]})["authors"]
    assert a == {"family": "EXAMPLE CONSORTIUM ABC", "given": ""}


@pytest.mark.parametrize("s, fam, giv", [
    ("Nice AB", "Nice", "AB"),        # surname equal to an org acronym
    ("Group JM", "Group", "JM"),      # surname equal to an org keyword
    ("Who J", "Who", "J"),
    ("Panel S", "Panel", "S"),
    ("Smith JA", "Smith", "JA"),
])
def test_single_surname_plus_initials_is_always_a_person(s, fam, giv):
    assert refresolve._author_from_string(s) == {"family": fam, "given": giv}


def test_create_items_posts_all_caps_group_unchanged(zot):
    _create([{"title": "T", "authors": [{"literal": "THE EXAMPLE STUDY GROUP"},
                                        {"family": "HADDAD", "given": "OMAR"}]}])
    assert _creators(zot["posted"][0]) == ["THE EXAMPLE STUDY GROUP", ("Haddad", "Omar")]


# ---------------------------------------------------------------------------
# 4. verify_authors=True against PubMed
# ---------------------------------------------------------------------------

def test_pubmed_collective_matches_crossref_group_confirmed(zot, pubmed):
    # PubMed spells it in caps without "The"; the keyword-less variant in the
    # next test proves the name-key match (not the corporate detector) works.
    pubmed.records["61616161"] = ("10.5555/group.2022", [
        ("Pagán", "Rosa"), ("SYNTHETIC EXAMPLE CONSORTIUM", ""), ("Okonkwo", "Chidi")])
    res = _create([_parsed()], verify_authors=True)
    (chk,) = res["author_check"]
    assert (chk["author_check"], chk["reason"]) == ("confirmed", "match")
    assert "groups_kept" not in chk
    assert _creators(zot["posted"][0]) == [("Pagán", "Rosa"), GROUP, ("Okonkwo", "Chidi")]


def test_keywordless_collective_is_not_counted_as_a_person(zot, pubmed):
    meta = {"pmid": "62626262", "title": "T", "authors": [
        {"family": "Haddad", "given": "Omar"}, {"literal": "SYNTHAX"},
        {"family": "Lindqvist", "given": "Sara"}]}
    pubmed.records["62626262"] = (None, [("Haddad", "Omar"), ("Synthax", ""),
                                         ("Lindqvist", "Sara")])
    res = _create([meta], verify_authors=True)
    (chk,) = res["author_check"]
    assert (chk["author_check"], chk["reason"]) == ("confirmed", "match")
    assert chk["authors_before"] == 3
    assert _creators(zot["posted"][0]) == [("Haddad", "Omar"), "SYNTHAX", ("Lindqvist", "Sara")]


def test_pubmed_wins_but_group_it_lacks_is_kept_in_place(zot, pubmed):
    # Crossref is shorter (one person missing) -> PubMed's list wins; PubMed has
    # no CollectiveName, so the group must be kept between the same persons.
    pubmed.records["63636363"] = ("10.5555/group.2022", [
        ("Pagán", "Rosa"), ("Okonkwo", "Chidi"), ("Lindqvist", "Sara")])
    res = _create([_parsed()], verify_authors=True)
    (chk,) = res["author_check"]
    assert (chk["author_check"], chk["reason"]) == ("pubmed", "crossref_shorter")
    assert chk["groups_kept"] == [GROUP]
    assert (chk["authors_before"], chk["authors_after"]) == (3, 4)
    assert _creators(zot["posted"][0]) == [
        ("Pagán", "Rosa"), GROUP, ("Okonkwo", "Chidi"), ("Lindqvist", "Sara")]


def test_pubmed_wins_with_matching_collective_uses_meta_group(zot, pubmed):
    pubmed.records["64646464"] = (None, [
        ("Pagán", "Rosa"), ("Synthetic Example Consortium", ""),
        ("Okonkwo", "Chidi"), ("Lindqvist", "Sara")])
    meta = dict(_parsed(), pmid="64646464")
    res = _create([meta], verify_authors=True)
    (chk,) = res["author_check"]
    assert chk["author_check"] == "pubmed" and "groups_kept" not in chk
    assert _creators(zot["posted"][0]) == [
        ("Pagán", "Rosa"), GROUP, ("Okonkwo", "Chidi"), ("Lindqvist", "Sara")]


def test_group_only_crossref_list_keeps_group_and_order(zot, pubmed):
    # Crossref has only groups (no persons) -> crossref_no_authors -> PubMed
    # wins; both groups stay, in their original relative order, before persons.
    pubmed.records["65656565"] = (None, [("Haddad", "Omar")])
    meta = {"pmid": "65656565", "title": "T",
            "authors": [{"literal": "Alpha Synthetic Group"},
                        {"literal": "Beta Synthetic Network"}]}
    res = _create([meta], verify_authors=True)
    (chk,) = res["author_check"]
    assert (chk["author_check"], chk["reason"]) == ("pubmed", "crossref_no_authors")
    assert chk["groups_kept"] == ["Alpha Synthetic Group", "Beta Synthetic Network"]
    assert _creators(zot["posted"][0]) == [
        "Alpha Synthetic Group", "Beta Synthetic Network", ("Haddad", "Omar")]


def test_trailing_group_appended_after_pubmed_persons():
    cr = [{"family": "Haddad", "given": "Omar"}, {"literal": "Gamma Synthetic Group"}]
    pm = [{"family": "Haddad", "given": "Omar"}, {"family": "Lindqvist", "given": "Sara"}]
    merged, kept = refresolve._keep_meta_groups(cr, pm)
    assert merged == [pm[0], {"literal": "Gamma Synthetic Group"}, pm[1]]
    assert kept == ["Gamma Synthetic Group"]
    cr2 = [{"family": "Haddad", "given": "Omar"}, {"family": "Lindqvist", "given": "Sara"},
           {"family": "Okonkwo", "given": "Chidi"}, {"literal": "Delta Synthetic Group"}]
    merged2, _ = refresolve._keep_meta_groups(cr2, pm)
    assert merged2[-1] == {"literal": "Delta Synthetic Group"} and len(merged2) == 3


def test_n_named_counts_group_authors():
    assert refresolve._n_named(_parsed()["authors"]) == 3


# ---------------------------------------------------------------------------
# 5. itemData / EndNote meta carry the literal
# ---------------------------------------------------------------------------

def test_unify_itemdata_carries_literal():
    idata = unify._itemdata_from_meta(_parsed(), "ABCD1234")
    assert idata["author"] == [{"family": "Pagán", "given": "Rosa"},
                               {"literal": GROUP},
                               {"family": "Okonkwo", "given": "Chidi"}]


def test_endnote_meta_carries_literal():
    out = endnote._meta_to_zotero_meta(_parsed(), {"authors": []})
    assert out["authors"][1] == {"literal": GROUP}
    item = zotero.csljson_to_zotero_item(out)
    assert item["creators"][1] == {"creatorType": "author", "name": GROUP}
