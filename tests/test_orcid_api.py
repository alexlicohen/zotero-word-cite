"""Tests for zoterocite.orcid_api — all network calls monkeypatched."""
from __future__ import annotations

import json

import pytest

from zoterocite import orcid_api
from zoterocite.orcid_api import _normalize_orcid_id, fetch_orcid_works


# ---------------------------------------------------------------------------
# Realistic ORCID /works JSON fixture
# ---------------------------------------------------------------------------

def _make_orcid_works_json() -> bytes:
    """Return a minimal but realistic ORCID /works response with 4 groups:
    - Work 1: doi + pmid
    - Work 2: title + year only (no ids)
    - Work 3: pmc id + title
    - Work 4: completely empty work-summary (resilience test)
    """
    data = {
        "group": [
            {
                "work-summary": [
                    {
                        "title": {"title": {"value": "Lesion network mapping of stroke outcomes"}},
                        "publication-date": {"year": {"value": "2020"}},
                        "external-ids": {
                            "external-id": [
                                {"external-id-type": "doi", "external-id-value": "10.1093/brain/awaa001"},
                                {"external-id-type": "pmid", "external-id-value": "32100001"},
                            ]
                        },
                    }
                ]
            },
            {
                "work-summary": [
                    {
                        "title": {"title": {"value": "Network localization of cognitive deficits"}},
                        "publication-date": {"year": {"value": "2021"}},
                        "external-ids": {"external-id": []},
                    }
                ]
            },
            {
                "work-summary": [
                    {
                        "title": {"title": {"value": "Causal brain network mapping"}},
                        "publication-date": {"year": {"value": "2022"}},
                        "external-ids": {
                            "external-id": [
                                {"external-id-type": "pmc", "external-id-value": "PMC9999999"},
                            ]
                        },
                    }
                ]
            },
            # Completely empty/garbage work-summary — should be skipped.
            {
                "work-summary": [{}]
            },
        ]
    }
    return json.dumps(data).encode()


# ---------------------------------------------------------------------------
# _normalize_orcid_id tests
# ---------------------------------------------------------------------------

class TestNormalizeOrcidId:
    def test_bare_id(self):
        assert _normalize_orcid_id("0000-0002-1825-0097") == "0000-0002-1825-0097"

    def test_https_url(self):
        assert _normalize_orcid_id("https://orcid.org/0000-0002-1825-0097") == "0000-0002-1825-0097"

    def test_http_url(self):
        assert _normalize_orcid_id("http://orcid.org/0000-0002-1825-0097") == "0000-0002-1825-0097"

    def test_trailing_slash(self):
        assert _normalize_orcid_id("https://orcid.org/0000-0002-1825-0097/") == "0000-0002-1825-0097"

    def test_x_checksum(self):
        assert _normalize_orcid_id("0000-0002-1694-233X") == "0000-0002-1694-233X"

    def test_garbage_returns_none(self):
        assert _normalize_orcid_id("not-an-orcid") is None

    def test_empty_string_returns_none(self):
        assert _normalize_orcid_id("") is None

    def test_none_returns_none(self):
        assert _normalize_orcid_id(None) is None


# ---------------------------------------------------------------------------
# fetch_orcid_works tests
# ---------------------------------------------------------------------------

class TestFetchOrcidWorks:
    def test_extracts_doi_and_pmid(self, monkeypatch):
        monkeypatch.setattr(orcid_api, "_http_get", lambda url, timeout=10: _make_orcid_works_json())
        works = fetch_orcid_works("0000-0002-1825-0097")
        w1 = next(w for w in works if w["doi"])
        assert w1["doi"] == "10.1093/brain/awaa001"
        assert w1["pmid"] == "32100001"
        assert w1["title"] == "Lesion network mapping of stroke outcomes"
        assert w1["year"] == "2020"
        assert w1["source"] == "orcid"

    def test_extracts_title_year_only(self, monkeypatch):
        monkeypatch.setattr(orcid_api, "_http_get", lambda url, timeout=10: _make_orcid_works_json())
        works = fetch_orcid_works("0000-0002-1825-0097")
        w2 = next(w for w in works if w["title"] == "Network localization of cognitive deficits")
        assert w2["doi"] == ""
        assert w2["pmid"] == ""
        assert w2["year"] == "2021"

    def test_extracts_pmc_id(self, monkeypatch):
        monkeypatch.setattr(orcid_api, "_http_get", lambda url, timeout=10: _make_orcid_works_json())
        works = fetch_orcid_works("0000-0002-1825-0097")
        w3 = next(w for w in works if w["pmcid"])
        assert w3["pmcid"] == "PMC9999999"
        assert w3["title"] == "Causal brain network mapping"

    def test_skips_completely_empty_work(self, monkeypatch):
        monkeypatch.setattr(orcid_api, "_http_get", lambda url, timeout=10: _make_orcid_works_json())
        works = fetch_orcid_works("0000-0002-1825-0097")
        # Should get 3 real works, not 4 (the empty one is skipped).
        assert len(works) == 3

    def test_citation_synthesized_with_year(self, monkeypatch):
        monkeypatch.setattr(orcid_api, "_http_get", lambda url, timeout=10: _make_orcid_works_json())
        works = fetch_orcid_works("0000-0002-1825-0097")
        w1 = next(w for w in works if w["doi"])
        assert w1["citation"] == "Lesion network mapping of stroke outcomes (2020)"

    def test_bare_id_works(self, monkeypatch):
        monkeypatch.setattr(orcid_api, "_http_get", lambda url, timeout=10: _make_orcid_works_json())
        works = fetch_orcid_works("0000-0002-1825-0097")
        assert len(works) == 3

    def test_full_url_works(self, monkeypatch):
        monkeypatch.setattr(orcid_api, "_http_get", lambda url, timeout=10: _make_orcid_works_json())
        works = fetch_orcid_works("https://orcid.org/0000-0002-1825-0097")
        assert len(works) == 3

    def test_invalid_id_returns_empty(self, monkeypatch):
        called = []
        monkeypatch.setattr(orcid_api, "_http_get", lambda url, timeout=10: called.append(url) or b"{}")
        result = fetch_orcid_works("not-an-orcid")
        assert result == []
        assert not called  # _http_get should not have been called

    def test_none_id_returns_empty(self, monkeypatch):
        called = []
        monkeypatch.setattr(orcid_api, "_http_get", lambda url, timeout=10: called.append(url) or b"{}")
        result = fetch_orcid_works(None)
        assert result == []
        assert not called

    def test_network_failure_returns_empty(self, monkeypatch):
        monkeypatch.setattr(orcid_api, "_http_get", lambda url, timeout=10: None)
        result = fetch_orcid_works("0000-0002-1825-0097")
        assert result == []

    def test_garbage_json_returns_empty(self, monkeypatch):
        monkeypatch.setattr(orcid_api, "_http_get", lambda url, timeout=10: b"<html>not json</html>")
        result = fetch_orcid_works("0000-0002-1825-0097")
        assert result == []

    def test_empty_json_object_returns_empty(self, monkeypatch):
        monkeypatch.setattr(orcid_api, "_http_get", lambda url, timeout=10: b"{}")
        result = fetch_orcid_works("0000-0002-1825-0097")
        assert result == []

    def test_all_returned_works_have_source_orcid(self, monkeypatch):
        monkeypatch.setattr(orcid_api, "_http_get", lambda url, timeout=10: _make_orcid_works_json())
        works = fetch_orcid_works("0000-0002-1825-0097")
        assert all(w["source"] == "orcid" for w in works)


# ---------------------------------------------------------------------------
# F5 — fetch_orcid_works_status: a genuinely-empty record must be distinguishable
# from a fetch FAILURE (both return [] from the bare function).
# ---------------------------------------------------------------------------

from zoterocite.orcid_api import fetch_orcid_works_status  # noqa: E402

_VALID_ORCID = "0000-0002-1825-0097"


def test_orcid_status_success_not_degraded(monkeypatch):
    monkeypatch.setattr(orcid_api, "_http_get",
                        lambda url, timeout=10: _make_orcid_works_json())
    works, status = fetch_orcid_works_status(_VALID_ORCID)
    assert works  # has works
    assert status["degraded"] is False


def test_orcid_status_empty_record_not_degraded(monkeypatch):
    # A successful fetch of an empty group is NOT degraded — the author has 0 works.
    monkeypatch.setattr(orcid_api, "_http_get",
                        lambda url, timeout=10: b'{"group": []}')
    works, status = fetch_orcid_works_status(_VALID_ORCID)
    assert works == []
    assert status["degraded"] is False


def test_orcid_status_fetch_failure_is_degraded(monkeypatch):
    # _http_get returns None on any network/HTTP failure → degraded, not empty.
    monkeypatch.setattr(orcid_api, "_http_get", lambda url, timeout=10: None)
    works, status = fetch_orcid_works_status(_VALID_ORCID)
    assert works == []
    assert status["degraded"] is True
    assert "fetch" in status["reason"].lower()
    # back-compat: bare function still returns [].
    monkeypatch.setattr(orcid_api, "_http_get", lambda url, timeout=10: None)
    assert fetch_orcid_works(_VALID_ORCID) == []


def test_orcid_status_bad_json_is_degraded(monkeypatch):
    monkeypatch.setattr(orcid_api, "_http_get", lambda url, timeout=10: b"not json{")
    works, status = fetch_orcid_works_status(_VALID_ORCID)
    assert works == []
    assert status["degraded"] is True


def test_orcid_status_invalid_id_not_degraded(monkeypatch):
    # A malformed iD is a caller error, not a degraded fetch.
    works, status = fetch_orcid_works_status("not-an-orcid")
    assert works == []
    assert status["degraded"] is False
