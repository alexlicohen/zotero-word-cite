"""Tests for zoterocite.entrez — NCBI ID-Converter + EFetch client.

All network access is monkeypatched; NO real requests are made.  We assert the
parsers handle the canned ID-Converter JSON and a PubmedArticleSet XML (incl. a
multi-AbstractText article), that a URLError degrades to an empty result, and
that the api_key never leaks into a returned value.
"""
import urllib.error

import pytest

import zoterocite.entrez as entrez


# ---------------------------------------------------------------------------
# Fake urlopen plumbing
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_urlopen(monkeypatch, body: bytes, capture: list = None):
    def fake(req, timeout=None):
        if capture is not None:
            # Record the actual URL urllib would fetch (carries any api_key).
            capture.append(req.full_url)
        return _FakeResp(body)
    monkeypatch.setattr(entrez.urllib.request, "urlopen", fake)


# ---------------------------------------------------------------------------
# Canned payloads
# ---------------------------------------------------------------------------

IDCONV_JSON = b"""{
  "status": "ok",
  "records": [
    {"pmcid": "PMC4734147", "pmid": "26980150", "doi": "10.1/x"},
    {"pmcid": "PMC9999999", "pmid": "30000001"},
    {"pmcid": "PMC0000000", "errmsg": "invalid article id"}
  ]
}"""

# Two articles: the second has multiple labeled AbstractText sections.
# Authors added to match real PubMed efetch output.
PUBMED_XML = b"""<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>26980150</PMID>
      <Article>
        <Journal>
          <Title>Nature Neuroscience</Title>
          <JournalIssue><PubDate><Year>2016</Year></PubDate></JournalIssue>
        </Journal>
        <ArticleTitle>Lesion network mapping of stroke.</ArticleTitle>
        <Abstract>
          <AbstractText>A single unlabeled abstract paragraph about networks.</AbstractText>
        </Abstract>
        <AuthorList>
          <Author>
            <LastName>Cohen</LastName>
            <ForeName>Alexander L</ForeName>
            <Initials>AL</Initials>
          </Author>
          <Author>
            <LastName>Fox</LastName>
            <Initials>MD</Initials>
          </Author>
        </AuthorList>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>30000001</PMID>
      <Article>
        <Journal>
          <Title>Brain</Title>
          <JournalIssue><PubDate><MedlineDate>2018 Spring</MedlineDate></PubDate></JournalIssue>
        </Journal>
        <ArticleTitle>Cognition after stroke.</ArticleTitle>
        <Abstract>
          <AbstractText Label="BACKGROUND">Cognitive deficits are common.</AbstractText>
          <AbstractText Label="METHODS">We mapped lesions.</AbstractText>
          <AbstractText Label="RESULTS">Networks predicted outcome.</AbstractText>
        </Abstract>
        <AuthorList>
          <Author>
            <LastName>Smith</LastName>
            <Initials>JA</Initials>
          </Author>
        </AuthorList>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
</PubmedArticleSet>"""

# Article with a CollectiveName group author and one named author.
PUBMED_XML_COLLECTIVE = b"""<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>99999999</PMID>
      <Article>
        <Journal>
          <Title>NEJM</Title>
          <JournalIssue><PubDate><Year>2020</Year></PubDate></JournalIssue>
        </Journal>
        <ArticleTitle>Consortium guidelines for brain imaging.</ArticleTitle>
        <AuthorList>
          <Author>
            <LastName>Jones</LastName>
            <Initials>BK</Initials>
          </Author>
          <Author>
            <CollectiveName>Brain Imaging Consortium</CollectiveName>
          </Author>
        </AuthorList>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
</PubmedArticleSet>"""

# Article with no AuthorList at all.
PUBMED_XML_NO_AUTHORS = b"""<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>11111111</PMID>
      <Article>
        <Journal>
          <Title>Anon Journal</Title>
          <JournalIssue><PubDate><Year>2022</Year></PubDate></JournalIssue>
        </Journal>
        <ArticleTitle>Anonymous title.</ArticleTitle>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
</PubmedArticleSet>"""


# ---------------------------------------------------------------------------
# available()
# ---------------------------------------------------------------------------

def test_available_true_with_default_email(monkeypatch):
    monkeypatch.delenv("NCBI_EMAIL", raising=False)
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    assert entrez.available() is True


# ---------------------------------------------------------------------------
# pmcids_to_pmids
# ---------------------------------------------------------------------------

def test_pmcids_to_pmids_parses_records(monkeypatch):
    _patch_urlopen(monkeypatch, IDCONV_JSON)
    out = entrez.pmcids_to_pmids(["PMC4734147", "PMC9999999"])
    assert out == {"PMC4734147": "26980150", "PMC9999999": "30000001"}


def test_pmcids_to_pmids_keys_are_original_spellings(monkeypatch):
    """Caller can look results up by exactly what they passed in."""
    _patch_urlopen(monkeypatch, IDCONV_JSON)
    out = entrez.pmcids_to_pmids(["pmc4734147"])  # lowercase input
    assert out == {"pmc4734147": "26980150"}


def test_pmcids_to_pmids_skips_error_records(monkeypatch):
    _patch_urlopen(monkeypatch, IDCONV_JSON)
    out = entrez.pmcids_to_pmids(["PMC0000000"])  # has errmsg, no pmid
    assert out == {}


def test_pmcids_to_pmids_empty_input_no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not hit the network on empty input")
    monkeypatch.setattr(entrez.urllib.request, "urlopen", boom)
    assert entrez.pmcids_to_pmids([]) == {}


def test_pmcids_to_pmids_urlerror_returns_empty(monkeypatch):
    def raise_urlerror(*a, **k):
        raise urllib.error.URLError("no network")
    monkeypatch.setattr(entrez.urllib.request, "urlopen", raise_urlerror)
    assert entrez.pmcids_to_pmids(["PMC4734147"]) == {}


@pytest.mark.parametrize("body", [b"[]", b"null", b'"x"', b"123"])
def test_pmcids_to_pmids_non_dict_json_returns_empty(monkeypatch, body):
    """FIX 1 regression: valid-but-non-dict JSON must return {} without raising."""
    _patch_urlopen(monkeypatch, body)
    result = entrez.pmcids_to_pmids(["PMC4734147"])
    assert result == {}, f"expected {{}} for body={body!r}, got {result!r}"


def test_pmcids_to_pmids_sends_prefixed_id_and_no_key_when_unset(monkeypatch):
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    captured: list = []
    _patch_urlopen(monkeypatch, IDCONV_JSON, capture=captured)
    entrez.pmcids_to_pmids(["123"])  # bare digits -> must be sent as PMC123
    assert captured, "expected one request"
    url = captured[0]
    assert "PMC123" in url
    assert "api_key" not in url


def test_normalize_pmcid_variants():
    assert entrez._normalize_pmcid("PMC4734147") == "PMC4734147"
    assert entrez._normalize_pmcid("pmc4734147") == "PMC4734147"
    assert entrez._normalize_pmcid("4734147") == "PMC4734147"
    assert entrez._normalize_pmcid("PMC4734147.1") == "PMC4734147"
    assert entrez._normalize_pmcid("") is None
    assert entrez._normalize_pmcid("garbage") is None


# ---------------------------------------------------------------------------
# efetch_pubmed
# ---------------------------------------------------------------------------

def test_efetch_pubmed_parses_articles(monkeypatch):
    _patch_urlopen(monkeypatch, PUBMED_XML)
    out = entrez.efetch_pubmed(["26980150", "30000001"])
    assert set(out) == {"26980150", "30000001"}

    a = out["26980150"]
    assert a["title"] == "Lesion network mapping of stroke."
    assert a["journal"] == "Nature Neuroscience"
    assert a["year"] == "2016"
    assert "networks" in a["abstract"]


def test_efetch_pubmed_concatenates_multi_abstracttext(monkeypatch):
    _patch_urlopen(monkeypatch, PUBMED_XML)
    out = entrez.efetch_pubmed(["30000001"])
    abstract = out["30000001"]["abstract"]
    # All three labeled sections present, with their labels.
    assert "BACKGROUND: Cognitive deficits are common." in abstract
    assert "METHODS: We mapped lesions." in abstract
    assert "RESULTS: Networks predicted outcome." in abstract
    # Year recovered from a MedlineDate (no <Year> node).
    assert out["30000001"]["year"] == "2018"


def test_efetch_pubmed_empty_input_no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not hit the network on empty input")
    monkeypatch.setattr(entrez.urllib.request, "urlopen", boom)
    assert entrez.efetch_pubmed([]) == {}


def test_efetch_pubmed_urlerror_returns_empty(monkeypatch):
    def raise_urlerror(*a, **k):
        raise urllib.error.URLError("down")
    monkeypatch.setattr(entrez.urllib.request, "urlopen", raise_urlerror)
    assert entrez.efetch_pubmed(["26980150"]) == {}


def test_efetch_pubmed_bad_xml_returns_empty(monkeypatch):
    _patch_urlopen(monkeypatch, b"<not-valid-xml><<<")
    assert entrez.efetch_pubmed(["26980150"]) == {}


# ---------------------------------------------------------------------------
# api_key must never leak into returned values
# ---------------------------------------------------------------------------

def test_api_key_never_in_returned_values(monkeypatch):
    secret = "SECRET_NCBI_KEY_DO_NOT_LEAK"
    monkeypatch.setenv("NCBI_API_KEY", secret)

    # ID converter
    _patch_urlopen(monkeypatch, IDCONV_JSON)
    id_out = entrez.pmcids_to_pmids(["PMC4734147"])
    assert secret not in repr(id_out)

    # EFetch
    _patch_urlopen(monkeypatch, PUBMED_XML)
    fetch_out = entrez.efetch_pubmed(["26980150", "30000001"])
    assert secret not in repr(fetch_out)


def test_api_key_passed_only_as_query_param(monkeypatch):
    """When set, the key rides in the URL query — and only there."""
    secret = "SECRET_NCBI_KEY_DO_NOT_LEAK"
    monkeypatch.setenv("NCBI_API_KEY", secret)
    captured: list = []
    _patch_urlopen(monkeypatch, IDCONV_JSON, capture=captured)
    entrez.pmcids_to_pmids(["PMC4734147"])
    assert captured
    assert f"api_key={secret}" in captured[0]


# ---------------------------------------------------------------------------
# Author extraction from _parse_article
# ---------------------------------------------------------------------------

def test_efetch_pubmed_extracts_authors_named(monkeypatch):
    """Named authors come back as 'LastName Initials' strings in order."""
    _patch_urlopen(monkeypatch, PUBMED_XML)
    out = entrez.efetch_pubmed(["26980150"])
    authors = out["26980150"]["authors"]
    assert authors == ["Cohen AL", "Fox MD"]


def test_efetch_pubmed_extracts_authors_initials_fallback(monkeypatch):
    """ForeName is used when Initials element is absent (not the case here,
    but the second article only has Initials — verify it still works)."""
    _patch_urlopen(monkeypatch, PUBMED_XML)
    out = entrez.efetch_pubmed(["30000001"])
    assert out["30000001"]["authors"] == ["Smith JA"]


def test_efetch_pubmed_collective_name_author(monkeypatch):
    """CollectiveName entries are included as-is in the author list."""
    _patch_urlopen(monkeypatch, PUBMED_XML_COLLECTIVE)
    out = entrez.efetch_pubmed(["99999999"])
    authors = out["99999999"]["authors"]
    assert authors == ["Jones BK", "Brain Imaging Consortium"]


def test_efetch_pubmed_no_authors_returns_empty_list(monkeypatch):
    """Articles with no AuthorList yield authors=[] without raising."""
    _patch_urlopen(monkeypatch, PUBMED_XML_NO_AUTHORS)
    out = entrez.efetch_pubmed(["11111111"])
    assert out["11111111"]["authors"] == []


# ---------------------------------------------------------------------------
# Structured authors — efetch_pubmed_authors + authors_structured field
#
# Back-compat: the bare ``authors`` (list of "Lastname Initials" strings) is
# unchanged; ``authors_structured`` is an ADDITIONAL per-article field, and
# ``efetch_pubmed_authors`` surfaces it as CSL-JSON-shaped {family, given}.
# ---------------------------------------------------------------------------

def test_authors_structured_field_back_compat(monkeypatch):
    """The string ``authors`` is untouched; ``authors_structured`` is added
    alongside it (existing callers/tests keep their contract)."""
    _patch_urlopen(monkeypatch, PUBMED_XML)
    out = entrez.efetch_pubmed(["26980150"])
    rec = out["26980150"]
    # Old shape preserved exactly.
    assert rec["authors"] == ["Cohen AL", "Fox MD"]
    # New structured field present and CSL-JSON-shaped.
    assert rec["authors_structured"] == [
        {"family": "Cohen", "given": "Alexander L"},  # full ForeName preferred
        {"family": "Fox", "given": "MD"},             # falls back to Initials
    ]


def test_efetch_pubmed_authors_named(monkeypatch):
    """efetch_pubmed_authors returns {pmid: [{family, given}]} for named authors."""
    _patch_urlopen(monkeypatch, PUBMED_XML)
    out = entrez.efetch_pubmed_authors(["26980150", "30000001"])
    assert out["26980150"] == [
        {"family": "Cohen", "given": "Alexander L"},
        {"family": "Fox", "given": "MD"},
    ]
    assert out["30000001"] == [{"family": "Smith", "given": "JA"}]


def test_efetch_pubmed_authors_collective_flagged(monkeypatch):
    """A <CollectiveName> becomes {family: <name>, given: ''} so a downstream
    corporate guard can skip it (no false person-comparison)."""
    _patch_urlopen(monkeypatch, PUBMED_XML_COLLECTIVE)
    out = entrez.efetch_pubmed_authors(["99999999"])
    assert out["99999999"] == [
        {"family": "Jones", "given": "BK"},
        {"family": "Brain Imaging Consortium", "given": ""},
    ]


def test_efetch_pubmed_authors_no_authors_empty_list(monkeypatch):
    """No AuthorList → empty structured list, no raise."""
    _patch_urlopen(monkeypatch, PUBMED_XML_NO_AUTHORS)
    out = entrez.efetch_pubmed_authors(["11111111"])
    assert out["11111111"] == []


def test_efetch_pubmed_authors_urlerror_returns_empty(monkeypatch):
    """Network failure degrades to {} (never raises), like efetch_pubmed."""
    def raise_urlerror(*a, **k):
        raise urllib.error.URLError("down")
    monkeypatch.setattr(entrez.urllib.request, "urlopen", raise_urlerror)
    assert entrez.efetch_pubmed_authors(["26980150"]) == {}


# DOI extraction: ArticleId[IdType=doi] (preferred) and ELocationID[EIdType=doi]
# (fallback). The DOI is the join key for the library-dedup + retraction screen,
# so this guards the seam end to end (no fixture above carries a DOI element).
PUBMED_XML_DOI = b"""<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>40000001</PMID>
      <Article>
        <Journal><Title>Brain</Title>
          <JournalIssue><PubDate><Year>2021</Year></PubDate></JournalIssue></Journal>
        <ArticleTitle>Both DOI carriers present.</ArticleTitle>
        <ELocationID EIdType="doi">10.2/eloc-should-not-win</ELocationID>
      </Article>
    </MedlineCitation>
    <PubmedData><ArticleIdList>
      <ArticleId IdType="pubmed">40000001</ArticleId>
      <ArticleId IdType="doi">10.1/aidlist-wins</ArticleId>
    </ArticleIdList></PubmedData>
  </PubmedArticle>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>40000002</PMID>
      <Article>
        <Journal><Title>Brain</Title>
          <JournalIssue><PubDate><Year>2022</Year></PubDate></JournalIssue></Journal>
        <ArticleTitle>ELocationID-only DOI.</ArticleTitle>
        <ELocationID EIdType="doi">10.3/eloc-only</ELocationID>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>40000003</PMID>
      <Article>
        <Journal><Title>Brain</Title>
          <JournalIssue><PubDate><Year>2023</Year></PubDate></JournalIssue></Journal>
        <ArticleTitle>No DOI anywhere.</ArticleTitle>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
</PubmedArticleSet>"""


def test_efetch_pubmed_extracts_doi(monkeypatch):
    _patch_urlopen(monkeypatch, PUBMED_XML_DOI)
    out = entrez.efetch_pubmed(["40000001", "40000002", "40000003"])
    # ArticleIdList DOI is preferred over an ELocationID DOI on the same article.
    assert out["40000001"]["doi"] == "10.1/aidlist-wins"
    # ELocationID-only falls back correctly.
    assert out["40000002"]["doi"] == "10.3/eloc-only"
    # No DOI element -> empty string (not missing key, not None).
    assert out["40000003"]["doi"] == ""


# ---------------------------------------------------------------------------
# F5 — *_status siblings expose a degraded signal so a partial / failed fetch is
# distinguishable from a genuine empty result. Bare functions stay back-compat.
# ---------------------------------------------------------------------------

def test_efetch_pubmed_status_clean(monkeypatch):
    _patch_urlopen(monkeypatch, PUBMED_XML)
    out, status = entrez.efetch_pubmed_status(["26980150", "30000001"])
    assert "26980150" in out
    assert status["degraded"] is False
    assert status["n_failed_batches"] == 0


def test_efetch_pubmed_status_network_failure_is_degraded(monkeypatch):
    def raise_urlerror(*a, **k):
        raise urllib.error.URLError("offline")
    monkeypatch.setattr(entrez.urllib.request, "urlopen", raise_urlerror)
    out, status = entrez.efetch_pubmed_status(["26980150"])
    assert out == {}
    assert status["degraded"] is True
    assert status["n_failed_batches"] == 1
    # back-compat: bare function still returns {}.
    monkeypatch.setattr(entrez.urllib.request, "urlopen", raise_urlerror)
    assert entrez.efetch_pubmed(["26980150"]) == {}


def test_efetch_pubmed_status_partial_batch_degraded(monkeypatch):
    # Batch size 1: first PMID fetch OK, second fails → partial + degraded.
    monkeypatch.setattr(entrez, "_BATCH", 1)
    seq = iter([PUBMED_XML, urllib.error.URLError("blip")])

    def fake(req, timeout=None):
        nxt = next(seq)
        if isinstance(nxt, BaseException):
            raise nxt
        return _FakeResp(nxt)
    monkeypatch.setattr(entrez.urllib.request, "urlopen", fake)
    out, status = entrez.efetch_pubmed_status(["26980150", "30000001"])
    assert status["degraded"] is True
    assert status["n_failed_batches"] == 1
    assert status["n_batches"] == 2


def test_pmcids_to_pmids_status_clean(monkeypatch):
    _patch_urlopen(monkeypatch, IDCONV_JSON)
    out, status = entrez.pmcids_to_pmids_status(["PMC4734147"])
    assert out.get("PMC4734147") == "26980150"
    assert status["degraded"] is False


def test_pmcids_to_pmids_status_failure_is_degraded(monkeypatch):
    def raise_urlerror(*a, **k):
        raise urllib.error.URLError("offline")
    monkeypatch.setattr(entrez.urllib.request, "urlopen", raise_urlerror)
    out, status = entrez.pmcids_to_pmids_status(["PMC4734147"])
    assert out == {}
    assert status["degraded"] is True


def test_esearch_pmids_status_clean(monkeypatch):
    _patch_urlopen(monkeypatch, b'{"esearchresult": {"idlist": ["111", "222"]}}')
    pmids, status = entrez.esearch_pmids_status("tubers", retmax=5)
    assert pmids == ["111", "222"]
    assert status["degraded"] is False


def test_esearch_pmids_status_zero_hits_not_degraded(monkeypatch):
    # A successful search with no hits is NOT degraded.
    _patch_urlopen(monkeypatch, b'{"esearchresult": {"idlist": []}}')
    pmids, status = entrez.esearch_pmids_status("nonsense-no-hits", retmax=5)
    assert pmids == []
    assert status["degraded"] is False


def test_esearch_pmids_status_network_failure_is_degraded(monkeypatch):
    def raise_urlerror(*a, **k):
        raise urllib.error.URLError("offline")
    monkeypatch.setattr(entrez.urllib.request, "urlopen", raise_urlerror)
    pmids, status = entrez.esearch_pmids_status("tubers", retmax=5)
    assert pmids == []
    assert status["degraded"] is True
    # back-compat: bare function still returns [].
    monkeypatch.setattr(entrez.urllib.request, "urlopen", raise_urlerror)
    assert entrez.esearch_pmids("tubers", retmax=5) == []
