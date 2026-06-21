"""Tests for zoterocite.refresolve — reference resolver.

All network access is monkeypatched; NO real HTTP requests are made.
"""
from __future__ import annotations

import json
import urllib.error

import pytest

import zoterocite._http as _http_mod
import zoterocite.refresolve as rr


# ---------------------------------------------------------------------------
# Fake http_get plumbing — patch the shared GET primitive, not raw urlopen.
# refresolve now routes all Crossref fetches through _http.http_get, so
# tests patch that seam instead of urllib.request.urlopen.
# ---------------------------------------------------------------------------

def _patch_urlopen(monkeypatch, body: bytes):
    """Patch _http.http_get to return *body* (simulates a successful response)."""
    monkeypatch.setattr(_http_mod, "http_get", lambda url, **kw: body)


def _patch_urlopen_error(monkeypatch, exc=None):
    """Patch _http.http_get to return None (simulates network failure).

    _http.http_get never raises — callers treat None as 'no response'.
    Previously this patched urlopen to raise URLError; now the contract is
    the same (caller sees a failure), but expressed via None return.
    """
    monkeypatch.setattr(_http_mod, "http_get", lambda url, **kw: None)


# ---------------------------------------------------------------------------
# Canned Crossref payloads
# ---------------------------------------------------------------------------

def _crossref_works_response(items: list[dict]) -> bytes:
    """Wrap items in the Crossref works envelope."""
    return json.dumps({
        "status": "ok",
        "message-type": "work-list",
        "message": {"items": items},
    }).encode()


def _crossref_single_response(item: dict) -> bytes:
    """Wrap a single item in the Crossref works/{doi} envelope."""
    return json.dumps({
        "status": "ok",
        "message-type": "work",
        "message": item,
    }).encode()


_CANNED_ITEM = {
    "DOI": "10.1016/j.neuron.2019.01.001",
    "title": ["Lesion network mapping reveals the neuroanatomical basis of behavior"],
    "author": [
        {"family": "Fox", "given": "Michael D"},
        {"family": "Cohen", "given": "Alexander L"},
    ],
    "published-print": {"date-parts": [[2019, 3, 6]]},
    "container-title": ["Neuron"],
    "type": "journal-article",
    "score": 42.5,
}

_CANNED_ITEM_2 = {
    "DOI": "10.1093/brain/awz123",
    "title": ["An unrelated paper about something else entirely"],
    "author": [{"family": "Smith", "given": "John"}],
    "published-print": {"date-parts": [[2020]]},
    "container-title": ["Brain"],
    "type": "journal-article",
    "score": 10.1,
}


# ---------------------------------------------------------------------------
# 1. extract_identifier tests
# ---------------------------------------------------------------------------

class TestExtractIdentifier:
    def test_doi_extracted_without_trailing_period(self):
        text = "Fox MD et al. 10.1016/j.neuron.2019.01.001. Neuron 2019."
        result = rr.extract_identifier(text)
        assert result["doi"] == "10.1016/j.neuron.2019.01.001"
        assert not result["doi"].endswith(".")

    def test_doi_trailing_comma_stripped(self):
        text = "See doi:10.1038/nature12345, for details."
        result = rr.extract_identifier(text)
        assert result["doi"] == "10.1038/nature12345"

    def test_doi_trailing_paren_stripped(self):
        text = "(10.1007/s00415-020-09876-5)"
        result = rr.extract_identifier(text)
        assert result["doi"] == "10.1007/s00415-020-09876-5"

    def test_doi_trailing_bracket_stripped(self):
        # Regression (E2): a bracketed DOI must NOT keep its trailing ']' — the
        # old local _TRAILING_PUNCT did not strip ']', producing a DOI that
        # 404'd at the Crossref /works/{doi} endpoint.
        text = "Smith J. [10.1038/s41586-020-2649-2] Nature 2020."
        result = rr.extract_identifier(text)
        assert result["doi"] == "10.1038/s41586-020-2649-2"
        assert not result["doi"].endswith("]")

    def test_doi_prefix_and_case_normalised(self):
        # The DOI is canonicalised (prefix-stripped + lowercased) via the owner.
        text = "See https://doi.org/10.1038/S41586-020-2649-2 for the data."
        result = rr.extract_identifier(text)
        assert result["doi"] == "10.1038/s41586-020-2649-2"

    def test_pmid_extracted(self):
        text = "Cohen AL et al. PMID: 26980150. Nat Neurosci 2016."
        result = rr.extract_identifier(text)
        assert result["pmid"] == "26980150"
        assert result["doi"] is None

    def test_pmid_no_space(self):
        text = "PMID:12345678"
        result = rr.extract_identifier(text)
        assert result["pmid"] == "12345678"

    def test_arxiv_extracted(self):
        text = "arXiv: 2301.01234 — a preprint on deep learning."
        result = rr.extract_identifier(text)
        assert result["arxiv"] == "2301.01234"

    def test_arxiv_id_surfaced_from_doi(self):
        # citation-resolve-lookup-8: an arXiv id carried only as a 10.48550/arXiv.
        # DOI (no explicit "arXiv:" label) must still populate arxiv= so the
        # zero-round-trip arXiv shortcut is not lost.
        text = "Vaswani A et al. Attention is all you need. doi:10.48550/arXiv.1706.03762"
        result = rr.extract_identifier(text)
        assert result["doi"] == "10.48550/arxiv.1706.03762"
        assert result["arxiv"] == "1706.03762"

    def test_non_arxiv_doi_leaves_arxiv_none(self):
        # A normal journal DOI must NOT populate arxiv.
        text = "Fox MD. 10.1038/nature12345. Nature 2013."
        result = rr.extract_identifier(text)
        assert result["arxiv"] is None

    def test_plain_author_year_all_none(self):
        text = "Fox MD, Buckner RL. Mapping symptoms to brain networks. Brain 2019;142:3138-3150."
        result = rr.extract_identifier(text)
        assert result["doi"] is None
        assert result["pmid"] is None
        assert result["arxiv"] is None

    def test_isbn_extracted(self):
        text = "Kandel ER. Principles of Neural Science. ISBN: 978-1-25-918217-8."
        result = rr.extract_identifier(text)
        assert result["isbn"] is not None
        # Digits only, no hyphens
        assert "-" not in result["isbn"]

    def test_empty_string(self):
        result = rr.extract_identifier("")
        assert result == {"doi": None, "pmid": None, "arxiv": None, "isbn": None}


# ---------------------------------------------------------------------------
# 2. crossref_bibliographic tests
# ---------------------------------------------------------------------------

class TestCrossrefBibliographic:
    def test_parses_items(self, monkeypatch):
        body = _crossref_works_response([_CANNED_ITEM, _CANNED_ITEM_2])
        _patch_urlopen(monkeypatch, body)
        results = rr.crossref_bibliographic("lesion network mapping Fox 2019")
        assert len(results) == 2

    def test_first_candidate_fields(self, monkeypatch):
        body = _crossref_works_response([_CANNED_ITEM])
        _patch_urlopen(monkeypatch, body)
        results = rr.crossref_bibliographic("lesion network mapping")
        assert len(results) == 1
        c = results[0]
        assert c["doi"] == "10.1016/j.neuron.2019.01.001"
        assert "Lesion network mapping" in c["title"]
        assert c["year"] == "2019"
        assert c["journal"] == "Neuron"
        assert c["type"] == "journal-article"
        assert c["score"] == 42.5
        assert c["authors"][0] == {"family": "Fox", "given": "Michael D"}

    def test_network_failure_returns_empty(self, monkeypatch):
        _patch_urlopen_error(monkeypatch)
        results = rr.crossref_bibliographic("anything")
        assert results == []

    def test_empty_query_returns_empty(self, monkeypatch):
        # Should short-circuit without hitting network
        results = rr.crossref_bibliographic("")
        assert results == []

    def test_malformed_json_returns_empty(self, monkeypatch):
        _patch_urlopen(monkeypatch, b"not json {{")
        results = rr.crossref_bibliographic("something")
        assert results == []


# ---------------------------------------------------------------------------
# 3. resolve_reference tests
# ---------------------------------------------------------------------------

class TestResolveReference:
    # (a) DOI in text → high/"doi"
    def test_doi_in_text_gives_high_confidence(self, monkeypatch):
        single = _crossref_single_response(_CANNED_ITEM)
        _patch_urlopen(monkeypatch, single)
        result = rr.resolve_reference(
            "Fox MD et al. doi:10.1016/j.neuron.2019.01.001 Neuron 2019."
        )
        assert result["confidence"] == "high"
        assert result["source"] == "doi"
        assert result["metadata"] is not None
        assert result["metadata"]["doi"] == "10.1016/j.neuron.2019.01.001"
        assert result["identifiers"]["doi"] == "10.1016/j.neuron.2019.01.001"

    # (a2) Bracketed DOI resolves by its EXACT clean DOI (regression E2).
    def test_bracketed_doi_resolves_by_exact_doi(self, monkeypatch):
        # A reference with a bracketed DOI [10.1038/s41586-020-2649-2] must hit
        # the Crossref /works/{doi} endpoint with the CLEAN DOI — not a URL
        # containing %5D (an escaped ']'), which 404s and silently degrades to
        # fuzzy bibliographic search.
        captured: dict = {}

        canned = {
            "DOI": "10.1038/s41586-020-2649-2",
            "title": ["Array programming with NumPy"],
            "author": [{"family": "Harris", "given": "Charles R"}],
            "published-print": {"date-parts": [[2020]]},
            "container-title": ["Nature"],
            "type": "journal-article",
            "score": 99.0,
        }

        def fake(url, **kw):
            captured["url"] = url
            return _crossref_single_response(canned)

        monkeypatch.setattr(_http_mod, "http_get", fake)

        result = rr.resolve_reference(
            "Harris CR et al. [10.1038/s41586-020-2649-2] Nature 2020."
        )

        # The fetch URL carries the clean DOI, not an escaped bracket.
        assert "url" in captured, "no Crossref request was made"
        assert "10.1038%2Fs41586-020-2649-2" in captured["url"] or \
               "10.1038/s41586-020-2649-2" in captured["url"]
        assert "%5D" not in captured["url"] and "%5d" not in captured["url"]
        assert "]" not in captured["url"]
        # Resolved by DOI — did NOT fall through to fuzzy bibliographic search.
        assert result["source"] == "doi"
        assert result["confidence"] == "high"
        assert result["identifiers"]["doi"] == "10.1038/s41586-020-2649-2"

    # (b) Full author-title-year reference matching Crossref top hit → high/"crossref"
    def test_full_ref_matching_crossref_high(self, monkeypatch):
        # Two candidates; first has large score lead + strong overlap
        body = _crossref_works_response([_CANNED_ITEM, _CANNED_ITEM_2])
        _patch_urlopen(monkeypatch, body)
        # Craft a reference that shares many tokens with _CANNED_ITEM's title,
        # mentions "Fox" and year 2019
        ref = (
            "Fox MD, Cohen AL. Lesion network mapping reveals neuroanatomical basis "
            "behavior. Neuron. 2019;103(2):214-225."
        )
        result = rr.resolve_reference(ref)
        assert result["source"] == "crossref"
        assert result["confidence"] in ("high", "medium")  # must be at least medium
        assert result["metadata"] is not None
        assert result["candidates"][0]["doi"] == "10.1016/j.neuron.2019.01.001"

    # (b2) Strict high: all three criteria met
    def test_full_ref_strict_high_crossref(self, monkeypatch):
        body = _crossref_works_response([_CANNED_ITEM, _CANNED_ITEM_2])
        _patch_urlopen(monkeypatch, body)
        ref = (
            "Fox MD. Lesion network mapping reveals the neuroanatomical basis of "
            "behavior. Neuron. 2019."
        )
        result = rr.resolve_reference(ref)
        assert result["source"] == "crossref"
        assert result["confidence"] == "high"

    # (c) Vague placeholder → returns candidates, lower confidence, no crash
    def test_placeholder_returns_candidates_no_crash(self, monkeypatch):
        body = _crossref_works_response([_CANNED_ITEM, _CANNED_ITEM_2])
        _patch_urlopen(monkeypatch, body)
        result = rr.resolve_reference("[find a ref on tubers and ASD]")
        assert result["confidence"] in ("low", "medium", "none")
        assert isinstance(result["candidates"], list)
        # Should not raise, input preserved
        assert "[find a ref on tubers and ASD]" in result["input"]

    # (d) Network failure → confidence "none"/"low", no raise
    def test_network_failure_no_raise(self, monkeypatch):
        _patch_urlopen_error(monkeypatch)
        result = rr.resolve_reference(
            "Smith J. Some important paper. Brain 2020;142:100-110."
        )
        assert result["confidence"] in ("none", "low")
        assert result["metadata"] is None
        assert result["candidates"] == []
        # Never raises

    def test_network_failure_with_doi_no_raise(self, monkeypatch):
        _patch_urlopen_error(monkeypatch)
        # DOI present but network is down — should fall through to "none"/"low"
        result = rr.resolve_reference(
            "Smith J. 10.1093/brain/awz999. Brain 2020."
        )
        # No raise; confidence degraded
        assert result["confidence"] in ("none", "low", "medium")

    # fetch=False tests
    def test_fetch_false_doi_present(self):
        result = rr.resolve_reference(
            "Fox MD. 10.1016/j.neuron.2019.01.001",
            fetch=False,
        )
        assert result["confidence"] == "high"
        assert result["metadata"] is None  # no network call made
        assert result["source"] is None

    def test_fetch_false_no_identifiers(self):
        result = rr.resolve_reference(
            "Smith J. An interesting paper. Brain 2020.",
            fetch=False,
        )
        assert result["confidence"] == "none"
        assert result["metadata"] is None

    # Empty input
    def test_empty_input_returns_none(self, monkeypatch):
        result = rr.resolve_reference("", fetch=True)
        assert result["confidence"] == "none"
        assert result["metadata"] is None

    # PMID path
    def test_pmid_resolution(self, monkeypatch):
        """PMID triggers entrez.efetch_pubmed — monkeypatch at the entrez layer."""
        fake_results = {
            "26980150": {
                "title": "Lesion network mapping of stroke.",
                "abstract": "...",
                "journal": "Nature Neuroscience",
                "year": "2016",
            }
        }
        monkeypatch.setattr(rr._entrez, "efetch_pubmed", lambda ids: fake_results)
        result = rr.resolve_reference("Cohen AL et al. PMID: 26980150")
        assert result["confidence"] == "high"
        assert result["source"] == "pmid"
        assert result["metadata"]["title"] == "Lesion network mapping of stroke."
        assert result["metadata"]["year"] == "2016"

    # Struct tests
    def test_return_structure_always_present(self, monkeypatch):
        body = _crossref_works_response([_CANNED_ITEM])
        _patch_urlopen(monkeypatch, body)
        result = rr.resolve_reference("anything")
        for key in ("input", "metadata", "confidence", "source", "candidates", "identifiers"):
            assert key in result

    def test_identifiers_always_returned(self, monkeypatch):
        _patch_urlopen_error(monkeypatch)
        result = rr.resolve_reference("Smith 2020 PMID:99999", fetch=True)
        assert "pmid" in result["identifiers"]
        assert result["identifiers"]["pmid"] == "99999"


# ---------------------------------------------------------------------------
# 3b. E3 — 'medium' must require ≥2 corroborating signals, with hardened
#     signal detectors (Jaccard overlap; word-boundary, length-guarded author).
# ---------------------------------------------------------------------------

class TestMediumRequiresTwoSignals:
    def test_wrong_same_year_only_signal_not_returned(self, monkeypatch):
        """A wrong same-year candidate (overlap 0, author absent) carries ONE
        weak signal (year) and must NOT be promoted to 'medium' / returned."""
        wrong = {
            "DOI": "10.9999/wrong",
            "title": ["Completely unrelated genome wide association meta study"],
            "author": [{"family": "Zzyzx", "given": "Q"}],
            "published-print": {"date-parts": [[2019]]},
            "container-title": ["Other Journal"],
            "type": "journal-article",
            "score": 5.0,
        }
        _patch_urlopen(monkeypatch, _crossref_works_response([wrong]))
        result = rr.resolve_reference(
            "Fox MD. Lesion network mapping of behavior. Neuron. 2019."
        )
        assert result["confidence"] not in ("high", "medium")
        assert result["metadata"] is None

    def test_two_signal_match_still_resolves(self, monkeypatch):
        """Author + year (two signals) on the correct paper still yields
        'medium' (or 'high') with metadata."""
        good = {
            "DOI": "10.1/correct",
            "title": ["Lesion network mapping reveals neuroanatomical basis of behavior"],
            "author": [{"family": "Fox", "given": "Michael"}],
            "published-print": {"date-parts": [[2019]]},
            "container-title": ["Neuron"],
            "type": "journal-article",
            "score": 40.0,
        }
        other = {
            "DOI": "10.2/other",
            "title": ["zzz unrelated"],
            "author": [{"family": "Q", "given": "R"}],
            "published-print": {"date-parts": [[2001]]},
            "type": "journal-article",
            "score": 2.0,
        }
        _patch_urlopen(monkeypatch, _crossref_works_response([good, other]))
        result = rr.resolve_reference(
            "Fox MD. Lesion network mapping behavior. Neuron. 2019;103:214."
        )
        assert result["confidence"] in ("high", "medium")
        assert result["metadata"] is not None
        assert result["candidates"][0]["doi"] == "10.1/correct"

    def test_short_surname_does_not_match_inside_word(self, monkeypatch):
        """Surname 'An' must not match inside 'analysis'; with only a
        coincidental substring (no real signal) the candidate is not returned."""
        an = {
            "DOI": "10.3/an",
            "title": ["Unrelated topic about something entirely different"],
            "author": [{"family": "An", "given": "H"}],
            "published-print": {"date-parts": [[1999]]},
            "type": "journal-article",
            "score": 3.0,
        }
        _patch_urlopen(monkeypatch, _crossref_works_response([an]))
        result = rr.resolve_reference(
            "a meta analysis of cortical development 2019"
        )
        assert result["confidence"] not in ("high", "medium")
        assert result["metadata"] is None

    def test_first_author_surname_an_not_in_analysis(self):
        cand = {"authors": [{"family": "An", "given": "H"}]}
        assert rr._first_author_surname_in_text(cand, "a meta analysis 2021") is False
        # but a real token "An H" is credited
        assert rr._first_author_surname_in_text(cand, "An H, Smith J. 2021") is True

    def test_short_generic_candidate_title_does_not_auto_clear(self):
        """A 2-3 token candidate title whose tokens all appear in a long input
        scored 1.0 under |A∩B|/|A|; Jaccard keeps it well below MEDIUM."""
        ov = rr._title_overlap(
            "brain study",
            "brain study of cortical tubers in tuberous sclerosis complex "
            "children with autism spectrum disorder presenting in 2019",
        )
        assert ov < rr._TITLE_OVERLAP_MEDIUM

    def test_title_overlap_is_symmetric_jaccard(self):
        # identical token sets -> 1.0; disjoint -> 0.0
        assert rr._title_overlap("alpha beta gamma", "alpha beta gamma") == 1.0
        assert rr._title_overlap("alpha beta", "delta epsilon") == 0.0

    # -- R4-3 (regression of the E3 2-of-3 gate) -----------------------------
    # A single STRONG title signal (overlap >= HIGH) must suffice for 'medium'
    # so a correct title-only / year-less match is not silently dropped to 'low'
    # (metadata=None) where consumers lose a correct resolution.

    def test_strong_title_only_match_resolves_medium(self, monkeypatch):
        """A correct title-only match (perfect title overlap, no parseable
        author token, no year in the input) yields 'medium' with metadata —
        a single STRONG title signal is sufficient."""
        # Input is essentially the title verbatim: no author surname, no year.
        title = "Lesion network mapping localizes neuropsychiatric symptoms"
        good = {
            "DOI": "10.1/titleonly",
            "title": [title],
            # First-author surname intentionally NOT present in the input text
            "author": [{"family": "Zylberberg", "given": "Q"}],
            "published-print": {"date-parts": [[2018]]},
            "container-title": ["Neuron"],
            "type": "journal-article",
            "score": 30.0,
        }
        _patch_urlopen(monkeypatch, _crossref_works_response([good]))
        result = rr.resolve_reference(title)  # no author token, no year
        # Sanity: only the single title signal fires
        assert rr._title_overlap(title, title) >= rr._TITLE_OVERLAP_HIGH
        assert rr._first_author_surname_in_text(good, title) is False
        assert rr._year_matches({"year": "2018"}, title) is False
        assert result["confidence"] == "medium", (
            f"Strong title-only match should be medium: {result['confidence']}"
        )
        assert result["metadata"] is not None
        assert result["metadata"]["title"] == title

    def test_wrong_year_only_still_low_under_strong_title_rule(self, monkeypatch):
        """E3 must still hold: a WRONG same-year-only match (overlap 0, author
        absent, only the year matches) stays 'low' / metadata=None even with the
        single-strong-title relaxation, because its lone signal is weak (year),
        not the strong title signal."""
        wrong = {
            "DOI": "10.9999/wrong",
            "title": ["Completely unrelated genome wide association meta study here"],
            "author": [{"family": "Zzyzx", "given": "Q"}],
            "published-print": {"date-parts": [[2019]]},
            "container-title": ["Other Journal"],
            "type": "journal-article",
            "score": 5.0,
        }
        _patch_urlopen(monkeypatch, _crossref_works_response([wrong]))
        result = rr.resolve_reference(
            "Fox MD. Lesion network mapping of behavior. Neuron. 2019."
        )
        assert result["confidence"] == "low"
        assert result["metadata"] is None

    def test_short_generic_title_does_not_clear_strong_rule(self, monkeypatch):
        """A short generic candidate title whose few tokens appear in a long
        input must NOT reach overlap >= HIGH (Jaccard guard), so it stays 'low'
        with no author/year corroboration."""
        generic = {
            "DOI": "10.5/generic",
            "title": ["brain study"],  # 2 tokens; Jaccard stays well below HIGH
            "author": [{"family": "Nomatch", "given": "Q"}],
            "published-print": {"date-parts": [[1990]]},
            "type": "journal-article",
            "score": 4.0,
        }
        _patch_urlopen(monkeypatch, _crossref_works_response([generic]))
        long_input = (
            "brain study of cortical tubers in tuberous sclerosis complex "
            "children with autism spectrum disorder presenting to clinic"
        )
        assert rr._title_overlap("brain study", long_input) < rr._TITLE_OVERLAP_HIGH
        result = rr.resolve_reference(long_input)
        assert result["confidence"] == "low"
        assert result["metadata"] is None


# ---------------------------------------------------------------------------
# 4. is_preprint tests
# ---------------------------------------------------------------------------

class TestIsPreprint:
    def test_biorxiv_doi_is_preprint(self):
        assert rr.is_preprint("10.1101/2020.01.01.123456") is True

    def test_medrxiv_doi_is_preprint(self):
        # medRxiv shares the 10.1101/ prefix with bioRxiv
        assert rr.is_preprint("10.1101/2021.05.15.21257155") is True

    def test_research_square_doi_is_preprint(self):
        assert rr.is_preprint("10.21203/rs.3.rs-12345/v1") is True

    def test_ssrn_doi_is_preprint(self):
        assert rr.is_preprint("10.2139/ssrn.3456789") is True

    def test_arxiv_formal_doi_is_preprint(self):
        assert rr.is_preprint("10.48550/arXiv.2301.01234") is True

    def test_bare_arxiv_id_is_preprint(self):
        # arXiv id without a formal DOI
        assert rr.is_preprint("arXiv: 2301.01234") is True

    def test_journal_doi_is_not_preprint(self):
        assert rr.is_preprint("10.1212/WNL.0000000000012345") is False

    def test_neuron_doi_is_not_preprint(self):
        assert rr.is_preprint("10.1016/j.neuron.2019.01.001") is False

    def test_empty_string_is_not_preprint(self):
        assert rr.is_preprint("") is False

    def test_doi_inside_reference_string(self):
        # Full reference string with embedded bioRxiv DOI
        ref = "Smith J et al. A preprint study. doi:10.1101/2022.03.14.484321"
        assert rr.is_preprint(ref) is True

    def test_posted_content_type_not_via_is_preprint_directly(self):
        # is_preprint works on identifier strings, not item dicts;
        # posted-content type is handled by _parse_crossref_item's preprint flag
        assert rr.is_preprint("10.1016/j.neuron.2019.01.001") is False

    def test_journal_ref_mentioning_biorxiv_doi_is_not_preprint(self):
        """A published-paper reference that cites a bioRxiv DOI in passing → False."""
        ref = (
            "Smith J. A study expanding on bioRxiv work (doi:10.1101/2019.01.01.123). "
            "Brain. 2022. doi:10.1093/brain/awz999."
        )
        assert rr.is_preprint(ref) is False

    def test_bare_arxiv_number_without_prefix_is_not_preprint(self):
        """Bare NNNN.NNNNN without arXiv: prefix must not match (could be a page range)."""
        assert rr.is_preprint("2021.10234") is False
        assert rr.is_preprint("Fox MD. Brain 2021;10:1-12.") is False

    def test_arxiv_doi_prefix_is_preprint(self):
        """10.48550/ DOI → arXiv preprint."""
        assert rr.is_preprint("10.48550/arXiv.2301.01234") is True

    def test_arxiv_prefix_with_colon_is_preprint(self):
        """Explicit arXiv: label → preprint."""
        assert rr.is_preprint("arXiv:2301.01234") is True


# ---------------------------------------------------------------------------
# 5. check_preprint_status tests
# ---------------------------------------------------------------------------

def _crossref_work_with_relation(doi: str, published_doi: str,
                                  journal: str = None,
                                  id_type: str = "doi") -> bytes:
    """Build a Crossref works/{doi} response with relation.is-preprint-of."""
    item = {
        "DOI": doi,
        "title": ["A preprint title"],
        "type": "posted-content",
        "relation": {
            "is-preprint-of": [
                {"id": published_doi, "id-type": id_type, "asserted-by": "publisher"}
            ]
        },
    }
    if journal:
        item["container-title"] = [journal]
    return json.dumps({"status": "ok", "message-type": "work", "message": item}).encode()


def _crossref_work_no_relation(doi: str) -> bytes:
    """Build a Crossref works/{doi} response with no relation field."""
    item = {
        "DOI": doi,
        "title": ["A preprint title"],
        "type": "posted-content",
        "relation": {},
    }
    return json.dumps({"status": "ok", "message-type": "work", "message": item}).encode()


class TestCheckPreprintStatus:
    def test_biorxiv_with_published_doi(self, monkeypatch):
        preprint_doi = "10.1101/2020.01.01.123456"
        published = "10.1212/WNL.0000000000014567"
        body = _crossref_work_with_relation(preprint_doi, published, journal="Neurology")
        _patch_urlopen(monkeypatch, body)
        result = rr.check_preprint_status(preprint_doi)
        assert result["is_preprint"] is True
        assert result["published_doi"] == published
        # published_in is no longer returned — venue requires a follow-up resolve
        assert "published_in" not in result

    def test_published_doi_requires_id_type_doi(self, monkeypatch):
        """published_doi must be None when id-type is not 'doi'."""
        preprint_doi = "10.1101/2020.01.01.123456"
        published = "10.1212/WNL.0000000000014567"
        # id-type is "uri" instead of "doi" — should NOT trust the id
        body = _crossref_work_with_relation(preprint_doi, published, id_type="uri")
        _patch_urlopen(monkeypatch, body)
        result = rr.check_preprint_status(preprint_doi)
        assert result["is_preprint"] is True
        assert result["published_doi"] is None

    def test_preprint_no_relation_returns_none(self, monkeypatch):
        preprint_doi = "10.1101/2020.01.01.123456"
        body = _crossref_work_no_relation(preprint_doi)
        _patch_urlopen(monkeypatch, body)
        result = rr.check_preprint_status(preprint_doi)
        assert result["is_preprint"] is True
        assert result["published_doi"] is None

    def test_journal_doi_is_not_preprint(self, monkeypatch):
        # Even if Crossref returns a record, the DOI prefix tells us it's not a preprint.
        journal_doi = "10.1212/WNL.0000000000012345"
        # We won't call the network for a non-preprint in this test — just check flag.
        _patch_urlopen_error(monkeypatch)
        result = rr.check_preprint_status(journal_doi, fetch=False)
        assert result["is_preprint"] is False
        assert result["published_doi"] is None

    def test_network_failure_degrades_gracefully(self, monkeypatch):
        preprint_doi = "10.1101/2020.01.01.123456"
        _patch_urlopen_error(monkeypatch)
        result = rr.check_preprint_status(preprint_doi)
        assert result["is_preprint"] is True   # flag from DOI prefix, no network needed
        assert result["published_doi"] is None  # network failed

    def test_fetch_false_skips_network(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("must not hit network when fetch=False")
        monkeypatch.setattr(_http_mod, "http_get", boom)
        result = rr.check_preprint_status("10.1101/2020.01.01.123456", fetch=False)
        assert result["is_preprint"] is True
        assert result["published_doi"] is None

    def test_doi_extracted_from_reference_string(self, monkeypatch):
        """check_preprint_status accepts full reference strings, not just bare DOIs."""
        preprint_doi = "10.1101/2020.01.01.123456"
        published = "10.1093/brain/awz200"
        body = _crossref_work_with_relation(preprint_doi, published)
        _patch_urlopen(monkeypatch, body)
        ref_string = f"Smith J et al. A study. bioRxiv doi:{preprint_doi}"
        result = rr.check_preprint_status(ref_string)
        assert result["is_preprint"] is True
        assert result["published_doi"] == published

    # -- citation-resolve-lookup-7 ------------------------------------------
    # The published DOI is not guaranteed to be is-preprint-of[0], nor always
    # under the is-preprint-of key. Scan all entries across is-preprint-of /
    # is-version-of / has-version and take the first DOI-typed id.

    def test_published_doi_found_past_index_zero(self, monkeypatch):
        """A non-DOI entry at [0] must not hide the DOI entry at [1]."""
        preprint_doi = "10.1101/2020.01.01.123456"
        published = "10.1212/WNL.0000000000014567"
        item = {
            "DOI": preprint_doi,
            "title": ["A preprint title"],
            "type": "posted-content",
            "relation": {
                "is-preprint-of": [
                    # [0] carries a non-DOI id (the old code stopped here -> None)
                    {"id": "32000000", "id-type": "pmid", "asserted-by": "publisher"},
                    {"id": published, "id-type": "doi", "asserted-by": "publisher"},
                ]
            },
        }
        _patch_urlopen(monkeypatch, _crossref_single_response(item))
        result = rr.check_preprint_status(preprint_doi)
        assert result["is_preprint"] is True
        assert result["published_doi"] == published

    def test_published_doi_found_under_is_version_of(self, monkeypatch):
        """The link may live under is-version-of, not is-preprint-of."""
        preprint_doi = "10.1101/2020.01.01.123456"
        published = "10.1093/brain/awz200"
        item = {
            "DOI": preprint_doi,
            "title": ["A preprint title"],
            "type": "posted-content",
            "relation": {
                "is-version-of": [
                    {"id": published, "id-type": "doi", "asserted-by": "publisher"},
                ]
            },
        }
        _patch_urlopen(monkeypatch, _crossref_single_response(item))
        result = rr.check_preprint_status(preprint_doi)
        assert result["is_preprint"] is True
        assert result["published_doi"] == published


# ---------------------------------------------------------------------------
# 6. preprint flag on parsed Crossref items
# ---------------------------------------------------------------------------

class TestPreprintFlagOnParsedItems:
    def test_posted_content_item_has_preprint_true(self, monkeypatch):
        preprint_item = {
            "DOI": "10.1101/2020.01.01.123456",
            "title": ["A bioRxiv preprint"],
            "author": [{"family": "Smith", "given": "Jane"}],
            "type": "posted-content",
            "score": 9.0,
        }
        body = _crossref_works_response([preprint_item])
        _patch_urlopen(monkeypatch, body)
        results = rr.crossref_bibliographic("Smith bioRxiv preprint")
        assert results[0]["preprint"] is True

    def test_journal_article_item_has_preprint_false(self, monkeypatch):
        body = _crossref_works_response([_CANNED_ITEM])
        _patch_urlopen(monkeypatch, body)
        results = rr.crossref_bibliographic("lesion network mapping Fox 2019")
        assert results[0]["preprint"] is False


# ---------------------------------------------------------------------------
# 5. _pubmed_fetch author population
# ---------------------------------------------------------------------------

class TestPubmedFetchAuthors:
    """_pubmed_fetch must propagate authors from the entrez record."""

    def _fake_efetch(self, record: dict):
        """Return a monkeypatch-compatible efetch_pubmed that yields *record* for any PMID."""
        def _efetch(ids):
            return {pmid: record for pmid in ids}
        return _efetch

    def test_authors_populated_from_entrez_record(self, monkeypatch):
        record = {
            "title": "Lesion network mapping of stroke.",
            "abstract": "...",
            "journal": "Nature Neuroscience",
            "year": "2016",
            "authors": ["Cohen AL", "Fox MD"],
        }
        monkeypatch.setattr(rr._entrez, "efetch_pubmed", self._fake_efetch(record))
        result = rr._pubmed_fetch("26980150")
        assert result is not None
        assert result["authors"] == ["Cohen AL", "Fox MD"]

    def test_authors_empty_when_entrez_returns_none(self, monkeypatch):
        """If efetch_pubmed returns no record for the PMID, _pubmed_fetch returns None."""
        monkeypatch.setattr(rr._entrez, "efetch_pubmed", lambda ids: {})
        result = rr._pubmed_fetch("26980150")
        assert result is None

    def test_authors_empty_list_when_record_has_no_authors(self, monkeypatch):
        """Record present but no authors key → authors defaults to []."""
        record = {
            "title": "Anonymous paper.",
            "abstract": "",
            "journal": "Anon Journal",
            "year": "2022",
        }
        monkeypatch.setattr(rr._entrez, "efetch_pubmed", self._fake_efetch(record))
        result = rr._pubmed_fetch("11111111")
        assert result is not None
        assert result["authors"] == []

    def test_collective_name_author_passes_through(self, monkeypatch):
        """CollectiveName authors (plain strings) pass through unchanged."""
        record = {
            "title": "Consortium paper.",
            "abstract": "",
            "journal": "NEJM",
            "year": "2020",
            "authors": ["Jones BK", "Brain Imaging Consortium"],
        }
        monkeypatch.setattr(rr._entrez, "efetch_pubmed", self._fake_efetch(record))
        result = rr._pubmed_fetch("99999999")
        assert result is not None
        assert result["authors"] == ["Jones BK", "Brain Imaging Consortium"]
