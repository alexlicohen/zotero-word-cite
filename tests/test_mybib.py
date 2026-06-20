"""Tests for zoterocite.mybib — all network calls monkeypatched (offline)."""
from __future__ import annotations

import pytest

from zoterocite import mybib as mybib_module
from zoterocite.mybib import (
    _is_mybib_url,
    _normalize_mybib_url,
    fetch_mybib_works,
)


# ---------------------------------------------------------------------------
# Canned HTML fixtures
# ---------------------------------------------------------------------------

def _page1_html() -> bytes:
    """Simulate page 1: two PMIDs + one DOI."""
    return (
        b'<html><body>'
        b'<div pmid="12345678">Article A</div>'
        b'<div pmid="23456789">Article B</div>'
        b'<a href="https://doi.org/10.1093/brain/awaa001">doi</a>'
        b'</body></html>'
    )


def _page2_html() -> bytes:
    """Simulate page 2: one new PMID."""
    return (
        b'<html><body>'
        b'<div pmid="34567890">Article C</div>'
        b'</body></html>'
    )


def _page3_html() -> bytes:
    """Simulate page 3: empty (no PMIDs, no DOIs) → pagination must stop."""
    return b'<html><body><p>No more results.</p></body></html>'


_VALID_URL = "https://www.ncbi.nlm.nih.gov/myncbi/examplelab/bibliography/public/"
_VALID_URL_WITH_QUERY = _VALID_URL + "?page=3&sort=date"


# ---------------------------------------------------------------------------
# _is_mybib_url
# ---------------------------------------------------------------------------

class TestIsMybibUrl:
    def test_recognizes_valid_url(self):
        assert _is_mybib_url(_VALID_URL) is True

    def test_recognizes_url_with_query(self):
        assert _is_mybib_url(_VALID_URL_WITH_QUERY) is True

    def test_rejects_orcid_url(self):
        assert _is_mybib_url("https://orcid.org/0000-0002-1825-0097") is False

    def test_rejects_empty(self):
        assert _is_mybib_url("") is False

    def test_rejects_none(self):
        assert _is_mybib_url(None) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _normalize_mybib_url
# ---------------------------------------------------------------------------

class TestNormalizeMybibUrl:
    def test_strips_query_string(self):
        assert _normalize_mybib_url(_VALID_URL_WITH_QUERY) == _VALID_URL

    def test_ensures_trailing_slash(self):
        url = _VALID_URL.rstrip("/")
        assert _normalize_mybib_url(url) == _VALID_URL

    def test_idempotent(self):
        assert _normalize_mybib_url(_VALID_URL) == _VALID_URL

    def test_non_mybib_url_returns_none(self):
        assert _normalize_mybib_url("https://example.com/foo") is None


# ---------------------------------------------------------------------------
# fetch_mybib_works — paginated happy path
# ---------------------------------------------------------------------------

class TestFetchMybibWorks:
    def _mock_pages(self, monkeypatch):
        """Mock _http_get to return canned pages 1→2→3(empty)."""
        calls = []

        def fake_http_get(url: str, timeout: float = 10.0):
            calls.append(url)
            if "page=1" in url:
                return _page1_html()
            if "page=2" in url:
                return _page2_html()
            # page=3 and beyond: empty (stops pagination)
            return _page3_html()

        monkeypatch.setattr(mybib_module, "_http_get", fake_http_get)
        return calls

    def _mock_entrez(self, monkeypatch):
        """Mock entrez.efetch_pubmed to return metadata for canned PMIDs."""
        def fake_efetch(pmids):
            data = {
                "12345678": {
                    "title": "Lesion network mapping of stroke outcomes",
                    "year": "2020",
                    "abstract": "We mapped stroke lesion networks.",
                    "journal": "Brain",
                    "doi": "10.1093/brain/awaa001",
                    "authors": ["Cohen A", "Fox MD"],
                },
                "23456789": {
                    "title": "Network localization of cognitive deficits after stroke",
                    "year": "2021",
                    "abstract": "Cognitive deficits map to specific networks.",
                    "journal": "Neuron",
                    "doi": "",
                    "authors": ["Cohen A"],
                },
                "34567890": {
                    "title": "Causal brain network mapping",
                    "year": "2022",
                    "abstract": "Causal inference in lesion networks.",
                    "journal": "Nat Neurosci",
                    "doi": "",
                    "authors": ["Cohen A"],
                },
            }
            return {p: data[p] for p in pmids if p in data}

        monkeypatch.setattr(mybib_module.entrez, "efetch_pubmed", fake_efetch)

    def test_pagination_stops_on_empty_page(self, monkeypatch):
        calls = self._mock_pages(monkeypatch)
        self._mock_entrez(monkeypatch)
        fetch_mybib_works(_VALID_URL)
        # Pages 1 and 2 fetched; page 3 fetched (to detect empty); page 4 NOT fetched.
        page_nums = [int(u.split("page=")[1]) for u in calls if "page=" in u]
        assert 3 in page_nums, "should fetch page 3 to detect empty"
        assert 4 not in page_nums, "should stop after empty page 3"

    def test_extracts_pmids_from_two_pages(self, monkeypatch):
        self._mock_pages(monkeypatch)
        self._mock_entrez(monkeypatch)
        works = fetch_mybib_works(_VALID_URL)
        pmids = {w["pmid"] for w in works if w.get("pmid")}
        assert pmids == {"12345678", "23456789", "34567890"}

    def test_enriches_title_and_year(self, monkeypatch):
        self._mock_pages(monkeypatch)
        self._mock_entrez(monkeypatch)
        works = fetch_mybib_works(_VALID_URL)
        by_pmid = {w["pmid"]: w for w in works if w.get("pmid")}
        w1 = by_pmid["12345678"]
        assert w1["title"] == "Lesion network mapping of stroke outcomes"
        assert w1["year"] == "2020"
        assert w1["citation"] == "Lesion network mapping of stroke outcomes (2020)"

    def test_source_tag_is_mybib(self, monkeypatch):
        self._mock_pages(monkeypatch)
        self._mock_entrez(monkeypatch)
        works = fetch_mybib_works(_VALID_URL)
        assert all(w["source"] == "mybib" for w in works)

    def test_doi_only_entry_emitted_when_no_pmid(self, monkeypatch):
        """Page has a DOI (from page 1) without a matching PMID-based doi → doi-only entry."""
        # Override page1 to have a DOI not returned by entrez for any PMID.
        def fake_http_get(url: str, timeout: float = 10.0):
            if "page=1" in url:
                return (
                    b'<html><body>'
                    b'<a href="https://doi.org/10.1234/dataset.001">link</a>'
                    b'</body></html>'
                )
            return _page3_html()

        monkeypatch.setattr(mybib_module, "_http_get", fake_http_get)
        monkeypatch.setattr(mybib_module.entrez, "efetch_pubmed", lambda ids: {})

        works = fetch_mybib_works(_VALID_URL)
        dois = [w["doi"] for w in works]
        assert "10.1234/dataset.001" in dois

        doi_entry = next(w for w in works if w["doi"] == "10.1234/dataset.001")
        assert doi_entry["pmid"] == ""
        assert doi_entry["source"] == "mybib"

    def test_no_phantom_doi_only_when_efetch_omits_pmid_doi(self, monkeypatch):
        """A paper whose PMID and DOI sit in the same citation block must NOT be
        emitted twice when PubMed efetch fails to echo the DOI back for that PMID
        — the page-block pairing suppresses the standalone doi-only duplicate."""
        def fake_http_get(url: str, timeout: float = 10.0):
            if "page=1" in url:
                return (
                    b'<html><body>'
                    b'<div pmid="11111111">Paper with a DOI</div>'
                    b'<a href="https://doi.org/10.1234/cool">doi</a>'
                    b'</body></html>'
                )
            return _page3_html()

        monkeypatch.setattr(mybib_module, "_http_get", fake_http_get)
        # efetch returns the record but WITHOUT the DOI (a common real case).
        monkeypatch.setattr(mybib_module.entrez, "efetch_pubmed",
                            lambda ids: {"11111111": {"title": "Paper with a DOI",
                                                      "year": "2021", "doi": ""}})

        works = fetch_mybib_works(_VALID_URL)
        # exactly one work — the PMID entry — and NO phantom titleless doi-only entry.
        assert len(works) == 1
        assert works[0]["pmid"] == "11111111"
        assert not any(w["pmid"] == "" and w["doi"] == "10.1234/cool" for w in works)

    def test_doi_in_pmid_entry_not_duplicated_as_doi_only(self, monkeypatch):
        """A DOI that also appears in a PMID's Entrez record must not emit a second doi-only entry."""
        def fake_http_get(url: str, timeout: float = 10.0):
            if "page=1" in url:
                return (
                    b'<html><body>'
                    b'<div pmid="12345678">Article A</div>'
                    b'<a href="https://doi.org/10.1093/brain/awaa001">doi</a>'
                    b'</body></html>'
                )
            return _page3_html()

        def fake_efetch(pmids):
            return {
                "12345678": {
                    "title": "Lesion network mapping of stroke outcomes",
                    "year": "2020",
                    "doi": "10.1093/brain/awaa001",
                    "abstract": "", "journal": "Brain", "authors": [],
                }
            }

        monkeypatch.setattr(mybib_module, "_http_get", fake_http_get)
        monkeypatch.setattr(mybib_module.entrez, "efetch_pubmed", fake_efetch)

        works = fetch_mybib_works(_VALID_URL)
        doi_entries = [w for w in works if w.get("doi") == "10.1093/brain/awaa001"]
        assert len(doi_entries) == 1, "DOI from Entrez meta must not create a second doi-only entry"

    def test_deduplication_across_pages(self, monkeypatch):
        """Same PMID appearing on two pages must only produce one entry."""
        def fake_http_get(url: str, timeout: float = 10.0):
            if "page=1" in url:
                return b'<html><body><div pmid="12345678">A</div></body></html>'
            if "page=2" in url:
                return b'<html><body><div pmid="12345678">A again</div></body></html>'
            return _page3_html()

        monkeypatch.setattr(mybib_module, "_http_get", fake_http_get)
        monkeypatch.setattr(mybib_module.entrez, "efetch_pubmed", lambda ids: {})

        works = fetch_mybib_works(_VALID_URL)
        # Page 2 has the same PMID as page 1 → only one entry; page 2 looks like a
        # "new content" page (technically same pmid but seen_pmids prevents re-adding).
        # The pagination should stop after page 2 because no NEW pmids on page 2.
        pmids = [w["pmid"] for w in works if w.get("pmid")]
        assert pmids.count("12345678") == 1

    def test_bad_url_returns_empty(self, monkeypatch):
        """A non-My-Bibliography URL returns [] without calling the network."""
        called = []
        monkeypatch.setattr(mybib_module, "_http_get", lambda url, **kw: called.append(url) or b"")
        result = fetch_mybib_works("https://example.com/not/mybib/")
        assert result == []
        assert not called, "network must not be called for a bad URL"

    def test_network_error_returns_empty(self, monkeypatch):
        """Any network failure returns [] without raising."""
        monkeypatch.setattr(mybib_module, "_http_get", lambda url, **kw: None)
        result = fetch_mybib_works(_VALID_URL)
        assert result == []

    def test_entrez_failure_returns_unenriched(self, monkeypatch):
        """If entrez raises, PMIDs still produce entries (unenriched)."""
        def fake_http_get(url: str, timeout: float = 10.0):
            if "page=1" in url:
                return b'<html><body><div pmid="12345678">A</div></body></html>'
            return _page3_html()

        def boom(pmids):
            raise RuntimeError("entrez exploded")

        monkeypatch.setattr(mybib_module, "_http_get", fake_http_get)
        monkeypatch.setattr(mybib_module.entrez, "efetch_pubmed", boom)

        works = fetch_mybib_works(_VALID_URL)
        # Should not raise; returns entry with pmid but empty title/year.
        assert len(works) == 1
        assert works[0]["pmid"] == "12345678"
        assert works[0]["source"] == "mybib"

    def test_max_pages_respected(self, monkeypatch):
        """max_pages cap prevents infinite pagination."""
        def infinite_page(url: str, timeout: float = 10.0):
            # Returns a new PMID on every page (8-digit, starts at 10000001).
            page_num = int(url.split("page=")[1]) if "page=" in url else 1
            pmid = str(10000000 + page_num)
            return f'<html><body><div pmid="{pmid}">X</div></body></html>'.encode()

        monkeypatch.setattr(mybib_module, "_http_get", infinite_page)
        monkeypatch.setattr(mybib_module.entrez, "efetch_pubmed", lambda ids: {})

        works = fetch_mybib_works(_VALID_URL, max_pages=3)
        assert len(works) == 3

    def test_query_string_in_url_stripped(self, monkeypatch):
        """A URL with an existing ?page= query is normalized before use."""
        seen_urls = []

        def fake_http_get(url: str, timeout: float = 10.0):
            seen_urls.append(url)
            if "page=1" in url:
                return b'<html><body><div pmid="12345678">A</div></body></html>'
            return _page3_html()

        monkeypatch.setattr(mybib_module, "_http_get", fake_http_get)
        monkeypatch.setattr(mybib_module.entrez, "efetch_pubmed", lambda ids: {})

        fetch_mybib_works(_VALID_URL_WITH_QUERY)
        # The URLs used must be built from the stripped base, not the original.
        assert all("page=3&sort=date" not in u for u in seen_urls)
        assert any("page=1" in u for u in seen_urls)


# ---------------------------------------------------------------------------
# FIX 2 regression: doi=None in meta must not raise AttributeError
# ---------------------------------------------------------------------------

class TestFix2DoiNone:
    def test_doi_none_in_pmid_meta_does_not_raise(self, monkeypatch):
        """A meta record with doi=None must not raise; returns the entry safely."""
        def fake_http_get(url: str, timeout: float = 10.0):
            if "page=1" in url:
                return b'<html><body><div pmid="12345678">A</div></body></html>'
            return b'<html><body></body></html>'

        def fake_efetch(pmids):
            return {
                "12345678": {
                    "title": "Some title",
                    "year": "2020",
                    "abstract": "",
                    "journal": "Brain",
                    "doi": None,   # explicitly None — the problematic case
                    "authors": [],
                }
            }

        monkeypatch.setattr(mybib_module, "_http_get", fake_http_get)
        monkeypatch.setattr(mybib_module.entrez, "efetch_pubmed", fake_efetch)

        works = fetch_mybib_works(_VALID_URL)
        assert len(works) >= 1
        pmid_entry = next(w for w in works if w.get("pmid") == "12345678")
        assert pmid_entry["doi"] == ""  # None coerced to empty string

    def test_doi_none_in_doi_only_set_does_not_raise(self, monkeypatch):
        """None doi in meta used for pmid_doi_set must not raise; a genuinely
        standalone DOI (not in any PMID's citation block — here it precedes the
        only pmid) is still emitted as a doi-only entry."""
        def fake_http_get(url: str, timeout: float = 10.0):
            if "page=1" in url:
                return (
                    b'<html><body>'
                    b'<a href="https://doi.org/10.9999/dataset">link</a>'
                    b'<div pmid="12345678">A</div>'
                    b'</body></html>'
                )
            return b'<html><body></body></html>'

        def fake_efetch(pmids):
            return {
                "12345678": {
                    "title": "T",
                    "year": "2021",
                    "abstract": "",
                    "journal": "J",
                    "doi": None,   # None here triggers pmid_doi_set AttributeError
                    "authors": [],
                }
            }

        monkeypatch.setattr(mybib_module, "_http_get", fake_http_get)
        monkeypatch.setattr(mybib_module.entrez, "efetch_pubmed", fake_efetch)

        works = fetch_mybib_works(_VALID_URL)
        # Should not raise; the DOI-only entry should appear
        dois = [w["doi"] for w in works]
        assert "10.9999/dataset" in dois


# ---------------------------------------------------------------------------
# FIX 3a regression: DOI trimming at & and ?
# ---------------------------------------------------------------------------

class TestFix3aDOITrimming:
    def test_doi_trimmed_at_ampersand(self, monkeypatch):
        """DOIs with &amp; junk appended are trimmed at the first &."""
        def fake_http_get(url: str, timeout: float = 10.0):
            if "page=1" in url:
                # Simulate a bare DOI followed by &-separated query junk.
                return (
                    b'<html><body>'
                    b'10.1234/foo&amp;session=xyz'
                    b'</body></html>'
                )
            return b'<html><body></body></html>'

        monkeypatch.setattr(mybib_module, "_http_get", fake_http_get)
        monkeypatch.setattr(mybib_module.entrez, "efetch_pubmed", lambda ids: {})

        works = fetch_mybib_works(_VALID_URL)
        dois = [w["doi"] for w in works]
        # The junk after & must be stripped; &amp; decodes to & in html so trim fires.
        assert all("&" not in d for d in dois), f"Untrimmed DOIs: {dois}"
        assert all("session=xyz" not in d for d in dois)


# ---------------------------------------------------------------------------
# FIX 3b regression: SSRF — netloc check rejects path-embedded host
# ---------------------------------------------------------------------------

class TestFix3bSSRF:
    def test_crafted_ssrf_url_rejected(self):
        """evil.com with ncbi path in URL must be rejected."""
        evil = "https://evil.com/www.ncbi.nlm.nih.gov/myncbi/X/bibliography/public/"
        assert _is_mybib_url(evil) is False
        assert _normalize_mybib_url(evil) is None

    def test_real_mybib_url_still_accepted(self):
        """Legitimate My Bib URL must still pass."""
        assert _is_mybib_url(_VALID_URL) is True
        assert _normalize_mybib_url(_VALID_URL) == _VALID_URL

    def test_crafted_ssrf_returns_empty_from_fetch(self, monkeypatch):
        """fetch_mybib_works with an SSRF URL returns [] without hitting the network."""
        called = []
        monkeypatch.setattr(mybib_module, "_http_get", lambda url, **kw: called.append(url) or b"")
        evil = "https://evil.com/www.ncbi.nlm.nih.gov/myncbi/X/bibliography/public/"
        result = fetch_mybib_works(evil)
        assert result == []
        assert not called, "network must not be called for a crafted SSRF URL"


# ---------------------------------------------------------------------------
# F5 — fetch_mybib_works_status: a mid-pagination fetch failure must mark the
# bibliography INCOMPLETE rather than silently truncating it. The bare
# fetch_mybib_works stays back-compat (returns just the list).
# ---------------------------------------------------------------------------

from zoterocite.mybib import fetch_mybib_works_status  # noqa: E402


class TestMybibDegradedSignal:
    def _mock_entrez_full(self, monkeypatch):
        def fake_efetch(pmids):
            base = {
                "12345678": {"title": "A", "year": "2020", "doi": "", "authors": []},
                "23456789": {"title": "B", "year": "2021", "doi": "", "authors": []},
                "34567890": {"title": "C", "year": "2022", "doi": "", "authors": []},
            }
            return {p: base[p] for p in pmids if p in base}
        monkeypatch.setattr(mybib_module.entrez, "efetch_pubmed", fake_efetch)

    def test_midpagination_failure_marks_incomplete(self, monkeypatch):
        # Page 1 OK, page 2 fetch FAILS (None) — the works list is truncated to
        # page 1, and the status must say so (not pass it off as the whole bib).
        def fake_http_get(url, timeout=10.0):
            if "page=1" in url:
                return _page1_html()
            return None  # page 2 fetch failed
        monkeypatch.setattr(mybib_module, "_http_get", fake_http_get)
        self._mock_entrez_full(monkeypatch)

        works, status = fetch_mybib_works_status(_VALID_URL)
        # page-1 PMIDs are present (partial), but the result is flagged incomplete.
        assert {w["pmid"] for w in works if w.get("pmid")} == {"12345678", "23456789"}
        assert status["incomplete"] is True
        assert "page 2" in status["reason"]
        # back-compat: the bare function still returns just the (truncated) list.
        works_only = fetch_mybib_works(_VALID_URL)
        assert isinstance(works_only, list)

    def test_clean_end_is_not_incomplete(self, monkeypatch):
        # A page that returns 0 NEW items (true end of results) is NOT incomplete.
        def fake_http_get(url, timeout=10.0):
            if "page=1" in url:
                return _page1_html()
            if "page=2" in url:
                return _page2_html()
            return _page3_html()  # empty page → clean stop
        monkeypatch.setattr(mybib_module, "_http_get", fake_http_get)
        self._mock_entrez_full(monkeypatch)

        works, status = fetch_mybib_works_status(_VALID_URL)
        assert {w["pmid"] for w in works if w.get("pmid")} == {
            "12345678", "23456789", "34567890"}
        assert status["incomplete"] is False
        assert status["reason"] is None

    def test_partial_enrichment_marks_incomplete(self, monkeypatch):
        # All pages fetch fine, but PMID enrichment returns a PARTIAL map
        # (efetch degraded). The work SET is complete but metadata is partial →
        # incomplete, so the caller knows titles/years may be missing.
        def fake_http_get(url, timeout=10.0):
            if "page=1" in url:
                return _page1_html()
            if "page=2" in url:
                return _page2_html()
            return _page3_html()
        monkeypatch.setattr(mybib_module, "_http_get", fake_http_get)

        def fake_efetch(pmids):
            return {"12345678": {"title": "A", "year": "2020", "doi": "", "authors": []}}
        monkeypatch.setattr(mybib_module.entrez, "efetch_pubmed", fake_efetch)

        works, status = fetch_mybib_works_status(_VALID_URL)
        assert status["incomplete"] is True
        assert "enrichment" in status["reason"].lower()
