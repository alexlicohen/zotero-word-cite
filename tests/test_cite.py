"""Tests for zoterocite.cite (deterministic formatting) and zoterocite.zotero
request construction. No network is performed; no real credentials are used.
"""
from __future__ import annotations

import urllib.request

import pytest

from zoterocite import cite
from zoterocite.cite import (
    Reference,
    format_bibliography,
    format_reference,
    from_pubmed_record,
    in_text,
)
from zoterocite import zotero


# --------------------------------------------------------------------------- #
# cite.py
# --------------------------------------------------------------------------- #
def _sample() -> Reference:
    return Reference(
        authors=["Cohen AL", "Fox MD"],
        title="A study.",
        journal="Nat Hum Behav",
        year=2021,
        volume="5",
        issue="12",
        pages="1707-1716",
        pmid="34239076",
    )


def test_format_reference_vancouver_has_all_parts():
    out = format_reference(_sample(), "vancouver")
    assert "Cohen AL" in out
    assert "Fox MD" in out
    assert "A study." in out
    assert "Nat Hum Behav" in out
    assert "2021;5(12):1707-1716" in out
    assert "PMID: 34239076" in out


def test_vancouver_et_al_truncation_after_six():
    authors = [f"Auth{i} AB" for i in range(1, 9)]  # 8 authors
    ref = Reference(authors=authors, title="T.", journal="J", year=2020)
    out = format_reference(ref, "vancouver")
    assert "et al." in out
    assert "Auth6 AB" in out          # 6th author kept
    assert "Auth7 AB" not in out      # 7th+ dropped


def test_vancouver_et_al_truncation_respects_csl_id_nature():
    """Nature uses et_al_after=5; 6 authors should be truncated at 5."""
    authors = [f"Auth{i} AB" for i in range(1, 7)]  # 6 authors
    ref = Reference(authors=authors, title="T.", journal="Nature", year=2023)
    # Without csl_id: 6 authors fit exactly (no et al.)
    out_default = format_reference(ref, "vancouver")
    assert "et al." not in out_default
    assert "Auth6 AB" in out_default
    # With csl_id="nature": threshold is 5, so 6 authors triggers truncation
    out_nature = format_reference(ref, "vancouver", csl_id="nature")
    assert "et al." in out_nature
    assert "Auth5 AB" in out_nature   # 5th author kept
    assert "Auth6 AB" not in out_nature


def test_vancouver_et_al_default_fallback_for_unknown_csl_id():
    """An unrecognized csl_id falls back to ET_AL_AFTER=6."""
    authors = [f"Auth{i} AB" for i in range(1, 9)]  # 8 authors
    ref = Reference(authors=authors, title="T.", journal="J", year=2020)
    out = format_reference(ref, "vancouver", csl_id="some-unknown-journal-xyz")
    assert "et al." in out
    assert "Auth6 AB" in out   # threshold is still 6
    assert "Auth7 AB" not in out


def test_format_bibliography_respects_csl_id():
    """format_bibliography forwards csl_id so all entries use the right threshold."""
    authors = [f"Auth{i} AB" for i in range(1, 7)]  # 6 authors
    refs = [Reference(authors=authors, title="T.", year=2023)]
    # nature: et_al_after=5 → 6 authors triggers "et al."
    out = format_bibliography(refs, "vancouver", csl_id="nature")
    assert "et al." in out
    assert "Auth5 AB" in out
    assert "Auth6 AB" not in out


def test_author_year_in_text_and_reference():
    ref = _sample()  # two authors: Cohen AL, Fox MD
    marker = in_text(ref, "author_year")
    # E11: a two-author paper joins BOTH surnames; "et al." is for >=3 authors.
    assert marker == "(Cohen & Fox, 2021)"

    out = format_reference(ref, "author_year")
    assert "Cohen AL" in out
    assert "(2021)" in out            # parenthetical year
    assert "A study." in out
    assert "Nat Hum Behav" in out


def test_author_year_in_text_author_count_branches():
    """E11: one author -> bare surname; two -> 'A & B'; three+ -> 'A et al.'."""
    one = Reference(authors=["Smith JA"], title="t.", year=2020)
    two = Reference(authors=["Smith JA", "Jones RB"], title="t.", year=2020)
    three = Reference(
        authors=["Smith JA", "Jones RB", "Lee CD"], title="t.", year=2020
    )
    assert in_text(one, "author_year") == "(Smith, 2020)"
    assert in_text(two, "author_year") == "(Smith & Jones, 2020)"
    assert in_text(three, "author_year") == "(Smith et al., 2020)"


def test_author_year_in_text_no_authors_and_no_year():
    """E11: degrade gracefully — no authors -> 'Anon'; no year -> no comma."""
    no_year = Reference(authors=["Smith JA", "Jones RB"], title="t.", year="")
    assert in_text(no_year, "author_year") == "(Smith & Jones)"
    no_authors = Reference(authors=[], title="t.", year=2020)
    assert in_text(no_authors, "author_year") == "(Anon, 2020)"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2021 Dec", "2021"),
        ("2021-12-01", "2021"),
        ("2021", "2021"),
        ("1998 Jan-Feb", "1998"),
        ("in press", None),     # E11: must NOT leak 'in press' as a year
        ("21", None),           # E11: a stray 2-digit must NOT pass through
        ("forthcoming", None),
        ("", None),
    ],
)
def test_year_from_only_returns_real_years(raw, expected):
    """E11: _year_from returns a 19xx/20xx run or None — never raw garbage."""
    assert cite._year_from({"pubdate": raw}) == expected


def test_from_pubmed_record_in_press_year_is_none():
    """E11 end-to-end: an 'in press' pubdate yields year=None on the Reference,
    so it never leaks into the rendered citation or the author-year sort key."""
    ref = from_pubmed_record({"authors": ["Smith JA"], "pubdate": "in press"})
    assert ref.year is None
    assert in_text(ref, "author_year") == "(Smith)"


def test_vancouver_in_text_is_bracketed_index():
    assert in_text(1, "vancouver") == "[1]"
    assert in_text(7, "vancouver") == "[7]"


def test_missing_fields_degrade_gracefully():
    ref = Reference(authors=["Solo X"], title="Lonely title.")
    out = format_reference(ref, "vancouver")
    assert out.startswith("Solo X.")
    assert "Lonely title." in out
    # No stray separators from absent journal/year/pages.
    assert ";" not in out
    assert "PMID" not in out
    assert "doi" not in out


def test_formatters_dict_removed():
    """_FORMATTERS dead-code dict must not exist on the cite module."""
    assert not hasattr(cite, "_FORMATTERS"), (
        "_FORMATTERS was dead code and should have been deleted"
    )


def test_from_pubmed_record_maps_fields():
    record = {
        "pmid": "34239076",
        "title": "A study.",
        "source": "Nat Hum Behav",
        "pubdate": "2021 Dec",
        "volume": "5",
        "issue": "12",
        "pages": "1707-1716",
        "authors": ["Cohen AL", "Fox MD"],
        "elocationid": "10.1038/s41562-021-01138-0",
    }
    ref = from_pubmed_record(record)
    assert ref.pmid == "34239076"
    assert ref.title == "A study."
    assert ref.journal == "Nat Hum Behav"
    assert ref.year == "2021"
    assert ref.authors == ["Cohen AL", "Fox MD"]
    assert ref.doi == "10.1038/s41562-021-01138-0"


def test_doi_from_normalises_prefix_case_and_trailing_junk():
    # Regression (E7): _doi_from (via from_pubmed_record) must canonicalise the
    # DOI — strip a doi:/URL prefix, lowercase, and drop trailing prose
    # punctuation — rather than the old case-sensitive .replace("doi:","").
    prefixed = from_pubmed_record(
        {"title": "T.", "authors": ["X Y"],
         "doi": "https://doi.org/10.1038/S41586-020-2649-2"}
    )
    assert prefixed.doi == "10.1038/s41586-020-2649-2"

    upper_label = from_pubmed_record(
        {"title": "T.", "authors": ["X Y"], "doi": "DOI: 10.1000/abc)"}
    )
    assert upper_label.doi == "10.1000/abc"


def test_vancouver_renders_clean_doi_no_double_prefix():
    # The rendered tail must be exactly "doi:<clean-doi>." — one prefix,
    # lowercased, no trailing junk, regardless of the source surface form.
    ref_prefixed = from_pubmed_record(
        {"title": "Array programming.", "authors": ["Harris CR"],
         "journal": "Nature", "year": "2020",
         "doi": "https://doi.org/10.1038/S41586-020-2649-2"}
    )
    out = format_reference(ref_prefixed, "vancouver")
    assert "doi:10.1038/s41586-020-2649-2." in out
    assert "doi:https://" not in out          # no double prefix
    assert "S41586" not in out                 # lowercased

    ref_label = from_pubmed_record(
        {"title": "Another.", "authors": ["Smith J"],
         "journal": "Brain", "year": "2021", "doi": "DOI: 10.1000/abc"}
    )
    out2 = format_reference(ref_label, "vancouver")
    assert "doi:10.1000/abc." in out2
    assert "DOI:" not in out2


def test_from_pubmed_record_handles_dict_authors_and_missing_keys():
    record = {
        "title": "Partial.",
        "authors": [{"lastname": "Cohen", "initials": "AL"}],
    }
    ref = from_pubmed_record(record)
    assert ref.authors == ["Cohen AL"]
    assert ref.title == "Partial."
    assert ref.year is None
    assert ref.journal == ""
    # Should still format without raising.
    assert "Cohen AL." in format_reference(ref, "vancouver")


def test_format_bibliography_vancouver_numbers_in_order():
    refs = [
        Reference(authors=["Zed AA"], title="Last.", year=2019),
        Reference(authors=["Ant BB"], title="First.", year=2022),
    ]
    out = format_bibliography(refs, "vancouver")
    lines = out.splitlines()
    assert lines[0].startswith("1. ")
    assert lines[1].startswith("2. ")
    # Input order preserved (not alphabetised).
    assert "Last." in lines[0]
    assert "First." in lines[1]


def test_format_bibliography_author_year_sorts_by_surname_then_year():
    refs = [
        Reference(authors=["Zed AA"], title="Z.", year=2019),
        Reference(authors=["Ant BB"], title="A2.", year=2022),
        Reference(authors=["Ant BB"], title="A1.", year=2020),
    ]
    out = format_bibliography(refs, "author_year")
    lines = out.splitlines()
    # Ant (2020) < Ant (2022) < Zed (2019)
    assert "A1." in lines[0]
    assert "A2." in lines[1]
    assert "Z." in lines[2]


# --------------------------------------------------------------------------- #
# zotero.py — request construction only, NO network
# --------------------------------------------------------------------------- #
def test_build_request_group_library(monkeypatch):
    monkeypatch.setenv("ZOTERO_API_KEY", "fake-key-123")
    monkeypatch.setenv("ZOTERO_GROUP_ID", "987654")
    monkeypatch.delenv("ZOTERO_USER_ID", raising=False)

    req = zotero.build_request("items", {"q": "tsc"})
    assert isinstance(req, urllib.request.Request)
    assert "groups/987654/items" in req.full_url
    assert "q=tsc" in req.full_url
    # Headers are normalised to capitalised keys by Request.
    assert req.get_header("Zotero-api-key") == "fake-key-123"
    assert req.get_header("Zotero-api-version") == "3"


def test_build_request_prefers_group_over_user(monkeypatch):
    monkeypatch.setenv("ZOTERO_API_KEY", "k")
    monkeypatch.setenv("ZOTERO_GROUP_ID", "111")
    monkeypatch.setenv("ZOTERO_USER_ID", "222")
    req = zotero.build_request("items")
    assert "groups/111/items" in req.full_url
    assert "users/222" not in req.full_url


def test_build_request_user_library(monkeypatch):
    monkeypatch.setenv("ZOTERO_API_KEY", "k")
    monkeypatch.delenv("ZOTERO_GROUP_ID", raising=False)
    monkeypatch.setenv("ZOTERO_USER_ID", "555")
    req = zotero.build_request("items", {"q": "x"})
    assert "users/555/items" in req.full_url


def test_build_request_raises_when_creds_missing(monkeypatch):
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    monkeypatch.delenv("ZOTERO_GROUP_ID", raising=False)
    monkeypatch.delenv("ZOTERO_USER_ID", raising=False)
    with pytest.raises(RuntimeError) as exc:
        zotero.build_request("items", {"q": "x"})
    msg = str(exc.value)
    assert "ZOTERO_API_KEY" in msg
    assert "ZOTERO_GROUP_ID" in msg or "ZOTERO_USER_ID" in msg


def test_formatted_citations_builds_csl_request(monkeypatch):
    """formatted_citations should build a request with include=citation & style,
    without performing any network call (we stub _get_json)."""
    monkeypatch.setenv("ZOTERO_API_KEY", "k")
    monkeypatch.setenv("ZOTERO_GROUP_ID", "42")
    monkeypatch.delenv("ZOTERO_USER_ID", raising=False)

    captured: dict = {}

    def fake_get_json(req):
        captured["url"] = req.full_url
        return [{"bib": '<div class="csl-entry">A. Author. Title. 2021.</div>'},
                {"bib": "B. Author. Title2. 2022."}]

    monkeypatch.setattr(zotero, "_get_json", fake_get_json)
    out = zotero.formatted_citations(["ABC", "DEF"], style="ieee")   # default kind="bib"

    assert out == ["A. Author. Title. 2021.", "B. Author. Title2. 2022."]  # HTML stripped
    assert "include=bib" in captured["url"]
    assert "style=ieee" in captured["url"]
    assert "itemKey=ABC%2CDEF" in captured["url"]  # comma url-encoded

    def fake_citation(req):
        captured["url"] = req.full_url
        return [{"citation": "<span>(1)</span>"}]

    monkeypatch.setattr(zotero, "_get_json", fake_citation)
    assert zotero.formatted_citations(["ABC"], kind="citation") == ["(1)"]
    assert "include=citation" in captured["url"]


def test_fetch_all_paginates(monkeypatch):
    monkeypatch.setenv("ZOTERO_API_KEY", "k")
    monkeypatch.setenv("ZOTERO_GROUP_ID", "42")
    monkeypatch.delenv("ZOTERO_USER_ID", raising=False)
    pages = [
        ([{"key": f"K{i}"} for i in range(100)], {"Total-Results": "150"}),
        ([{"key": f"K{i}"} for i in range(100, 150)], {"Total-Results": "150"}),
    ]
    calls = {"n": 0}

    def fake(req, timeout=30.0):
        i = calls["n"]; calls["n"] += 1
        return pages[i]

    monkeypatch.setattr(zotero, "_get_json_headers", fake)
    out = zotero.fetch_all()
    assert len(out) == 150 and calls["n"] == 2


def test_count_items_reads_total_header(monkeypatch):
    monkeypatch.setenv("ZOTERO_API_KEY", "k")
    monkeypatch.setenv("ZOTERO_GROUP_ID", "42")
    monkeypatch.delenv("ZOTERO_USER_ID", raising=False)
    monkeypatch.setattr(zotero, "_get_json_headers",
                        lambda req, timeout=30.0: ([], {"Total-Results": "137"}))
    assert zotero.count_items() == 137


def test_zotero_config_empty_without_key(monkeypatch):
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    monkeypatch.setenv("ZOTERO_GROUP_ID", "1")
    assert zotero.zotero_config() == {}


def test_to_reference_maps_zotero_item():
    item = {
        "data": {
            "title": "A study.",
            "publicationTitle": "Nature Human Behaviour",
            "date": "2021-12-01",
            "volume": "5",
            "issue": "12",
            "pages": "1707-1716",
            "DOI": "10.1038/s41562-021-01138-0",
            "creators": [
                {"creatorType": "author", "firstName": "Alexander L", "lastName": "Cohen"},
                {"creatorType": "author", "firstName": "Michael D", "lastName": "Fox"},
                {"creatorType": "editor", "firstName": "Ed", "lastName": "Itor"},
            ],
        }
    }
    ref = zotero.to_reference(item)
    assert isinstance(ref, cite.Reference)
    assert ref.title == "A study."
    assert ref.journal == "Nature Human Behaviour"
    assert ref.year == "2021"
    assert ref.volume == "5"
    assert ref.doi == "10.1038/s41562-021-01138-0"
    # Editors excluded; initials derived from first names.
    assert ref.authors == ["Cohen AL", "Fox MD"]


# --------------------------------------------------------------------------- #
# CRIT-3 / F1: multi-key citation must never embed the WRONG reference.
# Zotero serves a multi-itemKey response in the library's natural sort order,
# NOT the requested order — csljson / formatted_citations must rebind by key.
# --------------------------------------------------------------------------- #
def _set_group_creds(monkeypatch, gid="2504198"):
    monkeypatch.setenv("ZOTERO_API_KEY", "k")
    monkeypatch.setenv("ZOTERO_GROUP_ID", gid)
    monkeypatch.delenv("ZOTERO_USER_ID", raising=False)


def test_csljson_reorders_to_request_order_when_zotero_reverses(monkeypatch):
    """Request [AAA, BBB]; Zotero returns them REVERSED (library order). Each
    requested key must come back bound to ITS OWN CSL-JSON, in request order."""
    _set_group_creds(monkeypatch)
    # Zotero csljson id is "<libraryID>/<itemKey>". Returned in reversed order.
    reversed_resp = {"items": [
        {"id": "2504198/BBB", "type": "article-journal", "title": "Beta paper"},
        {"id": "2504198/AAA", "type": "article-journal", "title": "Alpha paper"},
    ]}
    monkeypatch.setattr(zotero, "_get_json", lambda req: reversed_resp)

    out = zotero.csljson(["AAA", "BBB"])
    # request order preserved AND each slot carries its own metadata
    assert [it["id"] for it in out] == ["2504198/AAA", "2504198/BBB"]
    assert out[0]["title"] == "Alpha paper"   # AAA -> Alpha, not Beta
    assert out[1]["title"] == "Beta paper"


def test_csljson_bare_id_form_also_rebinds(monkeypatch):
    """Some exports use a bare itemKey as the CSL id; rebinding still works."""
    _set_group_creds(monkeypatch)
    resp = {"items": [
        {"id": "BBB", "title": "Beta paper"},
        {"id": "AAA", "title": "Alpha paper"},
    ]}
    monkeypatch.setattr(zotero, "_get_json", lambda req: resp)
    out = zotero.csljson(["AAA", "BBB"])
    assert [it["title"] for it in out] == ["Alpha paper", "Beta paper"]


def test_csljson_omitted_key_placeholders_without_shifting(monkeypatch):
    """Zotero OMITS one requested key (deleted/inaccessible). The remaining key
    must STILL bind correctly — the omission must not shift the others into the
    wrong slot — and the omission must be surfaced (warning), not silent."""
    _set_group_creds(monkeypatch)
    # Request [AAA, BBB, CCC]; Zotero returns only CCC and AAA (BBB omitted).
    resp = {"items": [
        {"id": "2504198/CCC", "title": "Gamma paper"},
        {"id": "2504198/AAA", "title": "Alpha paper"},
    ]}
    monkeypatch.setattr(zotero, "_get_json", lambda req: resp)

    with pytest.warns(UserWarning, match="BBB"):
        out = zotero.csljson(["AAA", "BBB", "CCC"])

    assert len(out) == 3                       # aligned 1:1 with request, no shift
    assert out[0]["title"] == "Alpha paper"    # AAA still bound to Alpha
    assert out[2]["title"] == "Gamma paper"    # CCC still bound to Gamma
    # BBB slot is a placeholder carrying ITS OWN id, not Gamma's/Alpha's metadata
    assert out[1] == {"id": "2504198/BBB"}
    assert "title" not in out[1]               # no other work's metadata leaked in


def test_csljson_single_key_unchanged(monkeypatch):
    """Single-key citations are trivially ordered and must pass through intact."""
    _set_group_creds(monkeypatch)
    resp = {"items": [{"id": "2504198/X8ISWWQ2", "title": "Solo paper"}]}
    monkeypatch.setattr(zotero, "_get_json", lambda req: resp)
    out = zotero.csljson(["X8ISWWQ2"])
    assert len(out) == 1 and out[0]["title"] == "Solo paper"


def test_csljson_list_response_form_reorders(monkeypatch):
    """csljson tolerates a bare-list response (no {"items": ...} wrapper)."""
    _set_group_creds(monkeypatch)
    resp = [
        {"id": "2504198/BBB", "title": "Beta paper"},
        {"id": "2504198/AAA", "title": "Alpha paper"},
    ]
    monkeypatch.setattr(zotero, "_get_json", lambda req: resp)
    out = zotero.csljson(["AAA", "BBB"])
    assert [it["title"] for it in out] == ["Alpha paper", "Beta paper"]


def test_formatted_citations_reorders_by_key(monkeypatch):
    """format=json carries a top-level "key"; rendered entries must be rebound to
    request order so a grouped marker doesn't render the wrong work first."""
    _set_group_creds(monkeypatch)
    # Zotero returns BBB before AAA (library order); each carries its own bib.
    resp = [
        {"key": "BBB", "bib": "B. Beta. 2022."},
        {"key": "AAA", "bib": "A. Alpha. 2021."},
    ]
    monkeypatch.setattr(zotero, "_get_json", lambda req: resp)
    out = zotero.formatted_citations(["AAA", "BBB"])
    assert out == ["A. Alpha. 2021.", "B. Beta. 2022."]   # request order


def test_formatted_citations_keyless_response_preserves_order(monkeypatch):
    """Legacy/stub responses carry no key; fall back to response order so the
    existing render path is unchanged (no regression)."""
    _set_group_creds(monkeypatch)
    resp = [{"bib": "First."}, {"bib": "Second."}]
    monkeypatch.setattr(zotero, "_get_json", lambda req: resp)
    out = zotero.formatted_citations(["ABC", "DEF"])
    assert out == ["First.", "Second."]


def test_formatted_citations_omitted_key_dropped_and_warned(monkeypatch):
    """A key Zotero omits yields no rendered entry (can't render what we lack) and
    is surfaced; the remaining entries stay correctly bound."""
    _set_group_creds(monkeypatch)
    resp = [{"key": "AAA", "bib": "A. Alpha. 2021."}]   # BBB omitted
    monkeypatch.setattr(zotero, "_get_json", lambda req: resp)
    with pytest.warns(UserWarning, match="BBB"):
        out = zotero.formatted_citations(["AAA", "BBB"])
    assert out == ["A. Alpha. 2021."]
