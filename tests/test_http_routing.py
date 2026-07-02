"""Routing regression tests: every public-API client GETs through _http.http_get.

After the HTTP-client consolidation there is ONE shared GET primitive,
:func:`zoterocite._http.http_get`.  These tests pin the seam: for each client
(entrez / orcid_api / mybib / icite) we monkeypatch
``module._http.http_get`` (which every module's thin ``_http_get`` wrapper
delegates to) and assert the module's fetch actually flows through it, carrying
the module's intended timeout and headers.

Fully offline: ``_http.http_get`` is replaced with a recorder, so no urllib /
network / sleep is exercised at all.
"""
from __future__ import annotations

import json

import zoterocite.entrez as entrez
import zoterocite.orcid_api as orcid_api
import zoterocite.mybib as mybib
import zoterocite.icite as icite
from zoterocite import _http


def _recorder(return_value):
    """A fake http_get that records each call's (url, timeout, headers)."""
    calls: list[dict] = []

    def fake(url, *, timeout, headers=None, retries=1, cache_ttl=None,
             refresh=False, _sleep=None):
        calls.append({
            "url": url,
            "timeout": timeout,
            "headers": headers or {},
            "cache_ttl": cache_ttl,
        })
        return return_value

    return calls, fake


# ---------------------------------------------------------------------------
# entrez — all three fetchers route through _http.http_get at the entrez timeout
# ---------------------------------------------------------------------------

def test_entrez_efetch_routes_through_http_get(monkeypatch):
    calls, fake = _recorder(b"<PubmedArticleSet></PubmedArticleSet>")
    monkeypatch.setattr(entrez._http, "http_get", fake)
    entrez.efetch_pubmed(["123"])
    assert len(calls) == 1
    assert calls[0]["timeout"] == entrez._TIMEOUT  # 30
    assert "User-Agent" in calls[0]["headers"]


def test_entrez_idconv_routes_through_http_get(monkeypatch):
    calls, fake = _recorder(b'{"records": []}')
    monkeypatch.setattr(entrez._http, "http_get", fake)
    entrez.pmcids_to_pmids(["PMC123"])
    assert len(calls) == 1
    assert calls[0]["timeout"] == entrez._TIMEOUT


def test_entrez_esearch_routes_through_http_get(monkeypatch):
    calls, fake = _recorder(json.dumps({"esearchresult": {"idlist": ["5"]}}).encode())
    monkeypatch.setattr(entrez._http, "http_get", fake)
    out = entrez.esearch_pmids("cohen[Author]", retmax=10)
    assert out == ["5"]
    assert len(calls) == 1
    # esearch URL carries the polite-pool tool + email (and never an api_key here).
    assert "tool=zotero-word-cite" in calls[0]["url"]
    assert "email=" in calls[0]["url"]


# ---------------------------------------------------------------------------
# orcid_api
# ---------------------------------------------------------------------------

def test_orcid_routes_through_http_get(monkeypatch):
    calls, fake = _recorder(b'{"group": []}')
    monkeypatch.setattr(orcid_api._http, "http_get", fake)
    orcid_api.fetch_orcid_works("0000-0002-1825-0097")
    assert len(calls) == 1
    assert calls[0]["timeout"] == orcid_api._TIMEOUT  # 10
    assert calls[0]["headers"].get("Accept") == "application/json"


# ---------------------------------------------------------------------------
# mybib
# ---------------------------------------------------------------------------

def test_mybib_routes_through_http_get(monkeypatch):
    # Return an empty page so pagination stops after one fetch.
    calls, fake = _recorder(b"<html></html>")
    monkeypatch.setattr(mybib._http, "http_get", fake)
    mybib.fetch_mybib_works(
        "https://www.ncbi.nlm.nih.gov/myncbi/examplelab/bibliography/public/"
    )
    assert len(calls) == 1
    assert calls[0]["timeout"] == 10.0
    assert "text/html" in calls[0]["headers"].get("Accept", "")


# ---------------------------------------------------------------------------
# icite
# ---------------------------------------------------------------------------

def test_icite_routes_through_http_get(monkeypatch):
    calls, fake = _recorder(b'{"data": []}')
    monkeypatch.setattr(icite._http, "http_get", fake)
    icite.fetch_icite(["12345678"])
    assert len(calls) == 1
    assert calls[0]["timeout"] == icite._TIMEOUT  # 15
    assert calls[0]["headers"].get("Accept") == "application/json"
