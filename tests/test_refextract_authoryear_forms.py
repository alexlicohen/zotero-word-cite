"""Author-year in-text forms (refextract) and grouped-marker linking (unify).

refextract detects author-year groups joined by 'and' as well as ';', names led
by a non-ASCII capital, and the narrative forms 'X et al., in 2019' and
'X and Y (2019)', without counting one citation twice and without new false
positives on ordinary parenthetical prose. unify._author_year_items splits a
grouped marker into its items, and every item links to its own reference.

Synthetic names, titles and DOIs only (10.5555/ test prefix); no network.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from zoterocite import new_doc
from zoterocite import refextract
from zoterocite import unify


def _author_year_texts(tmp_path: Path, paragraphs) -> list:
    p = tmp_path / "d.docx"
    new_doc(p, list(paragraphs))
    inv = refextract.extract_references(p)
    return [e["text"] for e in inv["intext"] if e["kind"] == "author_year"]


# ===========================================================================
# refextract — positive forms
# ===========================================================================

@pytest.mark.parametrize("para, marker", [
    # group joined by 'and' (#21)
    ("Cohorts were small (Alpha et al., 2022 and Beta et al., 2012).",
     "(Alpha et al., 2022 and Beta et al., 2012)"),
    ("Cohorts were small (Alpha et al., 2022, and Beta et al., 2012).",
     "(Alpha et al., 2022, and Beta et al., 2012)"),
    # names led by a non-ASCII capital / carrying non-ASCII letters
    ("Replicated later (Pagán et al., 2021).", "(Pagán et al., 2021)"),
    ("Replicated later (Ørsted, 2019).", "(Ørsted, 2019)"),
    ("Replicated later (Çelik and Quorn, 2020).", "(Çelik and Quorn, 2020)"),
    ("Replicated later (Ó Briain et al., 2018).", "(Ó Briain et al., 2018)"),
    ("Replicated later (Łabędzki, 2017; Šimek, 2016).", "(Łabędzki, 2017; Šimek, 2016)"),
    # narrative forms
    ("This was shown by Gamma et al., in 2019 using imaging.", "Gamma et al., in 2019"),
    ("This was shown by Gamma et al. in 2019 using imaging.", "Gamma et al. in 2019"),
    ("Delta and Epsilon (2019) used imaging.", "Delta and Epsilon (2019)"),
    ("Pagán et al. (2021) used imaging.", "Pagán et al. (2021)"),
    # a month-named surname is still a citation outside the solo date shape
    ("Seen before (May et al., 2019).", "(May et al., 2019)"),
    # back-compat: the forms detected before this change are unchanged
    ("Linked to outcome (Quorn et al., 2020).", "(Quorn et al., 2020)"),
    ("Quorn (2020) reported it.", "Quorn (2020)"),
    ("Reported (Quorn & Vey, 2020a; Brask, 2019).", "(Quorn & Vey, 2020a; Brask, 2019)"),
])
def test_author_year_form_detected(tmp_path, para, marker):
    assert _author_year_texts(tmp_path, [para]) == [marker]


# ===========================================================================
# refextract — negatives (ordinary parenthetical prose with a year)
# ===========================================================================

@pytest.mark.parametrize("para", [
    "Enrolment closed (and 2019 was the last year).",
    "Values are listed (see Table 2, 2019).",
    "Sample size was large (N = 2019).",
    "The clinic opened (In 2019) downtown.",
    "The clinic opened (Since 2019, rates rose).",
    "The clinic opened (March 2019) downtown.",
    "The clinic opened (May, 2019) downtown.",
    "The clinic opened (founded in 2019) downtown.",
    "The clinic opened in March (2019) downtown.",
    "Results held (Table 2019) overall.",
    "Results held (Quorn, 2020 and references therein).",
    "Results held (Alpha et al., 2022 and 2023).",
])
def test_ordinary_parenthetical_not_detected(tmp_path, para):
    assert _author_year_texts(tmp_path, [para]) == []


def test_bare_form_inside_parenthetical_counted_once(tmp_path):
    """'Quorn (2020)' sits inside the parenthetical group: one citation, one
    entry (the bare scan used to add it a second time)."""
    texts = _author_year_texts(tmp_path, ["Shown before (Brask, 2019; Quorn (2020)) here."])
    assert len(texts) == 1, texts
    assert texts[0].startswith("(Brask, 2019;")


def test_separate_citations_in_one_paragraph_all_counted(tmp_path):
    texts = _author_year_texts(tmp_path, [
        "Quorn (2020) agreed with prior work (Brask et al., 2019); Gamma et al., in 2018 did not."
    ])
    assert texts == ["(Brask et al., 2019)", "Quorn (2020)", "Gamma et al., in 2018"]


def test_suffixed_apa_reference_entries_are_detected(tmp_path):
    """An APA entry whose only year is "2020a" used to drop out of the list."""
    p = tmp_path / "apa.docx"
    entries = ["Quorn, A. (2020a). Sixth synthetic title. J Synth, 6, 1-2.",
               "Quorn, A. (2020b). Seventh synthetic title. J Synth, 7, 3-4.",
               "Brask, C. (2019). Eighth synthetic title. J Synth, 8, 5-6."]
    new_doc(p, ["Shown (Quorn, 2020b).", "References", *entries])
    assert [r["text"] for r in refextract.extract_references(p)["reflist"]] == entries


# ===========================================================================
# unify._author_year_items — the grouped-marker splitting owner
# ===========================================================================

@pytest.mark.parametrize("marker, items", [
    ("(Quorn et al., 2020)", [("Quorn", "2020")]),
    # ONE two-author item: the 'and' has no year on its left
    ("(Alpha and Beta, 2020)", [("Alpha", "2020")]),
    ("(Alpha & Beta, 2020)", [("Alpha", "2020")]),
    # TWO items: a year on both sides of the 'and'
    ("(Alpha et al., 2022 and Beta et al., 2012)", [("Alpha", "2022"), ("Beta", "2012")]),
    ("(Alpha, 2020 and Beta and Gamma, 2019)", [("Alpha", "2020"), ("Beta", "2019")]),
    ("(Alpha et al., 2022; Beta, 2012)", [("Alpha", "2022"), ("Beta", "2012")]),
    # the letter suffix is KEPT: it decides which same-author/same-year work
    ("(Quorn & Vey, 2020a; Brask, 2019)", [("Quorn", "2020a"), ("Brask", "2019")]),
    # every year is its own work; a year-only chunk inherits the previous author
    ("(Quorn, 2019; 2020)", [("Quorn", "2019"), ("Quorn", "2020")]),
    ("(Quorn, 2019 and 2020)", [("Quorn", "2019"), ("Quorn", "2020")]),
    ("(Brask, 2018; Quorn, 2019, 2020)",
     [("Brask", "2018"), ("Quorn", "2019"), ("Quorn", "2020")]),
    ("(Brask, 2018; Quorn, 2020a, b)",
     [("Brask", "2018"), ("Quorn", "2020a"), ("Quorn", "2020b")]),
    # a year with no author anywhere before it: unlinkable "" item, still counted
    ("(Quorn, 2019; 2020; Brask, 2018)",
     [("Quorn", "2019"), ("Quorn", "2020"), ("Brask", "2018")]),
    ("(2020; Quorn, 2019)", [("", "2020"), ("Quorn", "2019")]),
    # "2020abc" carries no suffix; "e.g." after a year is not a continuation
    ("(Quorn, 2020abc)", [("Quorn", "2020")]),
    ("(Brask, 2018; Quorn, 2020a, e.g.)", [("Brask", "2018"), ("Quorn", "2020a")]),
    ("Delta and Epsilon (2019)", [("Delta", "2019")]),
    ("Gamma et al., in 2019", [("Gamma", "2019")]),
    ("(Ó Briain et al., 2018)", [("Ó Briain", "2018")]),
    ("(Pagán et al., 2021)", [("Pagán", "2021")]),
    # trailing prose without a year never forms an item
    ("(Quorn, 2020 and references therein)", [("Quorn", "2020")]),
    # an undated item is still a cited work (year ""), so it is counted
    ("(Alpha et al., 2022; Beta et al., n.d.)", [("Alpha", "2022"), ("Beta", "")]),
    ("(Quorn et al.)", [("Quorn", "")]),
    # ... but a capitalised non-surname in a year-less tail is prose
    ("(Quorn, 2020; Table 2)", [("Quorn", "2020")]),
    # a named leftover after a dated item in the same chunk is trailing prose
    ("(Quorn, 2020 and Brask)", [("Quorn", "2020")]),
])
def test_author_year_items(marker, items):
    assert unify._author_year_items(marker) == items


def test_marker_item_count():
    assert unify._marker_item_count({"kind": "numeric", "text": "[5-7]"}) == 3
    assert unify._marker_item_count(
        {"kind": "author_year", "text": "(Alpha et al., 2022 and Beta et al., 2012)"}) == 2
    assert unify._marker_item_count({"kind": "author_year", "text": "(Alpha and Beta, 2020)"}) == 1
    assert unify._marker_item_count({"kind": "author_year", "text": "(Quorn et al.)"}) == 1


# ===========================================================================
# Grouped markers link EVERY item to its own reference
# ===========================================================================

REFLIST = [
    {"index": 0, "text": "Alpha A, Zed B. First synthetic title. J Synth. 2022;1:1-2.", "numbering": None},
    {"index": 1, "text": "Beta C. Second synthetic title. J Synth. 2012;2:3-4.", "numbering": None},
    {"index": 2, "text": "Pagan R, Yost T. Third synthetic title. J Synth. 2021;3:5-6.", "numbering": None},
    {"index": 3, "text": "Beta Q, Alpha A. Fourth synthetic title. J Synth. 2020;4:7-8.", "numbering": None},
    {"index": 4, "text": "Quorn A. Fifth synthetic title. J Synth. 2020a;5:9-10.", "numbering": None},
]


def _links(marker_text, reflist=REFLIST):
    intext = [{"index": 0, "text": marker_text, "kind": "author_year"}]
    return unify._link_intext_to_reflist(intext, reflist)


def test_grouped_and_marker_links_both_items():
    assert _links("(Alpha et al., 2022 and Beta et al., 2012)") == {0: [0], 1: [0]}


def test_grouped_semicolon_marker_links_both_items():
    assert _links("(Beta, 2012; Alpha et al., 2022)") == {1: [0], 0: [0]}


def test_two_author_item_links_only_its_first_author():
    """'(Beta and Alpha, 2020)' is ONE item whose first author is Beta: it links
    the Beta 2020 reference and nothing else."""
    assert _links("(Beta and Alpha, 2020)") == {3: [0]}


def test_accent_insensitive_surname_link():
    """The in-text marker keeps the accent; the reference list dropped it."""
    assert _links("(Pagán et al., 2021)") == {2: [0]}


def test_suffixed_year_links_both_ways():
    """'2020a' on either side still links (a word-boundary year test refused it)."""
    assert _links("(Quorn, 2020a)") == {4: [0]}


SUFFIXED = [
    {"index": 0, "text": "Quorn A. Sixth synthetic title. J Synth. 2020a;6:1-2.", "numbering": None},
    {"index": 1, "text": "Quorn A. Seventh synthetic title. J Synth. 2020b;7:3-4.", "numbering": None},
]
UNSUFFIXED_TWIN = [
    {"index": 0, "text": "Quorn A. Sixth synthetic title. J Synth. 2020;6:1-2.", "numbering": None},
    {"index": 1, "text": "Quorn B. Seventh synthetic title. J Synth. 2020;7:3-4.", "numbering": None},
]


def test_suffix_picks_its_own_entry():
    """'(Quorn, 2020b)' links the 2020b entry, never the first Quorn-2020 one."""
    assert unify._author_year_item_links("(Quorn, 2020b)", SUFFIXED) == [1]
    assert unify._author_year_item_links("(Quorn, 2020a; Quorn, 2020b)", SUFFIXED) == [0, 1]


def test_suffixed_entry_needs_the_same_suffix():
    assert unify._author_year_item_links("(Quorn, 2020c)", SUFFIXED) == [None]
    assert unify._author_year_item_links("(Quorn, 2020)", SUFFIXED[:1]) == [None]


def test_ambiguous_same_author_year_stays_unlinked():
    """Two unsuffixed Quorn-2020 entries: the marker cannot say which, so it is
    left unlinked instead of taking the first."""
    assert unify._author_year_item_links("(Quorn, 2020)", UNSUFFIXED_TWIN) == [None]
    assert unify._author_year_item_links("(Quorn, 2020b)", UNSUFFIXED_TWIN) == [None]
    assert unify._author_year_item_links("(Quorn, 2020)", UNSUFFIXED_TWIN[:1]) == [0]
    # a lone unsuffixed entry still takes a suffixed item (Vancouver drops it)
    assert unify._author_year_item_links("(Quorn, 2020b)", UNSUFFIXED_TWIN[:1]) == [0]


def test_exact_suffix_breaks_a_tie_with_an_unsuffixed_entry():
    refs = [UNSUFFIXED_TWIN[0], SUFFIXED[1]]
    assert unify._author_year_item_links("(Quorn, 2020b)", refs) == [1]


def test_year_only_chunk_links_with_the_inherited_author():
    refs = [
        {"index": 0, "text": "Quorn A. Eighth synthetic title. J Synth. 2019;8:1-2.", "numbering": None},
        {"index": 1, "text": "Quorn A. Ninth synthetic title. J Synth. 2020;9:3-4.", "numbering": None},
    ]
    assert unify._author_year_item_links("(Quorn, 2019; 2020)", refs) == [0, 1]
    assert unify._author_year_item_links("(2020; Quorn, 2019)", refs) == [None, 0]


def test_undated_item_never_links():
    assert unify._author_year_item_links(
        "(Alpha et al., 2022; Beta et al., n.d.)", REFLIST) == [0, None]


def test_unlinked_item_reported_per_item():
    assert unify._author_year_item_links(
        "(Alpha et al., 2022; Nobody, 1999)", REFLIST) == [0, None]


# ===========================================================================
# Apply: a grouped author-year marker becomes ONE field citing every item
# ===========================================================================

def _meta(doi, title, family, year):
    return {"doi": doi, "title": title, "authors": [{"family": family, "given": "A"}],
            "year": year, "journal": "J Synth", "type": "article-journal"}


ALPHA = _meta("10.5555/alpha.2022", "First synthetic title", "Alpha", "2022")
BETA = _meta("10.5555/beta.2012", "Second synthetic title", "Beta", "2012")


def _fake_resolve(text, *, fetch=True):
    meta = ALPHA if text.startswith("Alpha") else BETA if text.startswith("Beta") else None
    return {"input": text, "metadata": dict(meta) if meta else None,
            "confidence": "high" if meta else "none", "source": "crossref",
            "candidates": [], "identifiers": {}}


@pytest.fixture()
def grouped_doc(tmp_path):
    p = tmp_path / "grouped.docx"
    new_doc(p, [
        "Cohorts were small (Alpha et al., 2022 and Beta et al., 2012).",
        "References",
        "Alpha A, Zed B. First synthetic title. J Synth. 2022;1:1-2.",
        "Beta C. Second synthetic title. J Synth. 2012;2:3-4.",
    ])
    return p


@pytest.fixture()
def patched(monkeypatch):
    monkeypatch.setattr(unify.refresolve, "resolve_reference", _fake_resolve)
    monkeypatch.setattr(unify.citecheck, "ensure_retraction_db", lambda **kw: (None, None))
    monkeypatch.setattr(unify.zotero, "library_index", lambda **kw: {
        "doi": {"10.5555/alpha.2022": "ALPHAKEY", "10.5555/beta.2012": "BETAKEY1"},
        "pmid": {}, "title": {}})
    monkeypatch.setattr(unify.entrez, "esearch_pmids_status",
                        lambda *a, **k: pytest.fail("no PubMed call expected"))
    calls = []
    monkeypatch.setattr(unify.zoterofield, "replace_text_with_zotero_field",
                        lambda doc, anchor, keys, **k: calls.append((anchor, list(keys))) or doc)
    monkeypatch.setattr(unify.zotero, "item_uri", lambda k: "http://zotero.org/groups/1/items/" + k)
    return calls


def test_grouped_marker_converted_with_every_item_in_order(grouped_doc, patched, tmp_path):
    plan = unify.plan_unification(grouped_doc)
    rep = unify.apply_unification(grouped_doc, plan, {}, out=tmp_path / "o.docx")
    assert patched == [("(Alpha et al., 2022 and Beta et al., 2012)", ["ALPHAKEY", "BETAKEY1"])]
    assert rep["replaced"] == 1 and rep["unresolved_in_doc"] == []


def test_group_with_undated_item_left_plain(tmp_path, patched):
    """'(Alpha et al., 2022; Beta et al., n.d.)' cites two works; only one can
    link, so the marker stays plain rather than citing Alpha alone."""
    p = tmp_path / "undated.docx"
    new_doc(p, [
        "Cohorts were small (Alpha et al., 2022; Beta et al., n.d.).",
        "References",
        "Alpha A, Zed B. First synthetic title. J Synth. 2022;1:1-2.",
    ])
    plan = unify.plan_unification(p, fetch=False)
    rep = unify.apply_unification(p, plan, {}, out=tmp_path / "o.docx")
    assert patched == []
    assert [u["marker"] for u in rep["unresolved_in_doc"]] == [
        "(Alpha et al., 2022; Beta et al., n.d.)"]


def test_grouped_marker_with_unaccepted_item_left_plain(grouped_doc, patched, tmp_path, monkeypatch):
    """All-or-nothing: when one item's reference is not accepted, the marker is
    left as plain text and reported (it used to be converted to a field citing
    only the first author)."""
    def _medium_beta(text, *, fetch=True):
        r = _fake_resolve(text, fetch=fetch)
        if text.startswith("Beta"):
            r["confidence"] = "medium"
        return r
    monkeypatch.setattr(unify.refresolve, "resolve_reference", _medium_beta)
    plan = unify.plan_unification(grouped_doc)
    rep = unify.apply_unification(grouped_doc, plan, {}, out=tmp_path / "o.docx")
    assert patched == []
    assert [u["marker"] for u in rep["unresolved_in_doc"]] == [
        "(Alpha et al., 2022 and Beta et al., 2012)"]


def test_year_suffix_excludes_decades():
    """A decade ("the 1990s") is not a suffixed year: the reference-list
    detector must not count a paragraph mentioning one as dated."""
    from zoterocite.refextract import _YEAR_RE
    assert _YEAR_RE.search("Smith, A. (2020a).")
    assert _YEAR_RE.search("2020b;")
    assert not _YEAR_RE.search("in the 1990s")
    assert not _YEAR_RE.search("the 2010s saw")
