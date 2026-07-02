"""Tests for zoterocite.icite — NIH iCite RCR/citation client.

Fully offline. The only network function (``_http_get``) is monkeypatched so no
real HTTP ever fires. Exercises JSON parsing of a realistic iCite payload,
batching across the chunk boundary, and graceful {} on bad/empty/error input.
"""
import json
import urllib.parse

import pytest

import zoterocite.icite as icite


def _pmids_from_url(url):
    """Extract the decoded pmid list from an iCite request URL."""
    q = urllib.parse.urlparse(url).query
    ids = urllib.parse.parse_qs(q).get("pmids", [""])[0]
    return [p for p in ids.split(",") if p]


# A realistic iCite /api/pubs payload (trimmed to the fields we consume).
SAMPLE_PAYLOAD = {
    "data": [
        {
            "pmid": 23456789,
            "year": 2019,
            "title": "Lesion network mapping of post-stroke cognition",
            "relative_citation_ratio": 3.42,
            "nih_percentile": 91.3,
            "citation_count": 187,
            "authors": [
                {"firstName": "Alexander L", "lastName": "Cohen", "fullName": "Alexander L Cohen"},
                {"firstName": "Michael D", "lastName": "Fox", "fullName": "Michael D Fox"},
            ],
        },
        {
            "pmid": 34567890,
            "year": 2008,
            "title": "A foundational connectome atlas",
            "relative_citation_ratio": 8.10,
            "nih_percentile": 99.0,
            "citation_count": 1450,
            "authors": [
                {"firstName": "Jane Q", "lastName": "Public", "fullName": "Jane Q Public"},
                {"firstName": "Alexander L", "lastName": "Cohen", "fullName": "Alexander L Cohen"},
            ],
        },
        {
            # A record with null metrics (too new / not yet scored).
            "pmid": 45678901,
            "year": 2024,
            "relative_citation_ratio": None,
            "nih_percentile": None,
            "citation_count": 0,
            "authors": [],
        },
    ]
}


def _patch_http(monkeypatch, payload_by_url=None, single=None):
    """Monkeypatch icite._http_get to return JSON bytes (no network)."""
    def fake_get(url, timeout=icite._TIMEOUT, **_kw):
        if single is not None:
            return json.dumps(single).encode("utf-8")
        return json.dumps(payload_by_url(url)).encode("utf-8")
    monkeypatch.setattr(icite, "_http_get", fake_get)


def test_fetch_icite_parses_realistic_payload(monkeypatch):
    _patch_http(monkeypatch, single=SAMPLE_PAYLOAD)
    out = icite.fetch_icite(["23456789", "34567890", "45678901"])
    assert set(out) == {"23456789", "34567890", "45678901"}

    rec = out["23456789"]
    assert rec["rcr"] == 3.42
    assert rec["citation_count"] == 187
    assert rec["nih_percentile"] == 91.3
    assert rec["year"] == 2019
    assert rec["authors"][0]["lastName"] == "Cohen"
    assert rec["authors"][0]["firstName"] == "Alexander L"
    assert rec["authors"][1]["lastName"] == "Fox"


def test_fetch_icite_null_metrics_become_none(monkeypatch):
    _patch_http(monkeypatch, single=SAMPLE_PAYLOAD)
    out = icite.fetch_icite(["45678901"])
    rec = out["45678901"]
    # Null RCR/percentile must be None (distinguishable from a real 0), not 0.0.
    assert rec["rcr"] is None
    assert rec["nih_percentile"] is None
    assert rec["citation_count"] == 0  # genuine 0 preserved
    assert rec["authors"] == []


def test_fetch_icite_batches_across_chunk_boundary(monkeypatch):
    """More than _BATCH pmids => multiple calls, each returning its own slice."""
    monkeypatch.setattr(icite, "_BATCH", 2)
    monkeypatch.setattr(icite, "_sleep_between_batches", lambda: None)
    calls = []

    def fake_get(url, timeout=icite._TIMEOUT, **_kw):
        # Parse the pmids back out of the (url-encoded) query string.
        ids = _pmids_from_url(url)
        calls.append(ids)
        data = [{"pmid": int(p), "relative_citation_ratio": 1.0,
                 "citation_count": 10, "nih_percentile": 50.0,
                 "year": 2020, "authors": []} for p in ids]
        return json.dumps({"data": data}).encode("utf-8")

    monkeypatch.setattr(icite, "_http_get", fake_get)
    out = icite.fetch_icite(["1", "2", "3", "4", "5"])
    assert len(out) == 5
    assert len(calls) == 3  # 2 + 2 + 1
    assert all(rec["rcr"] == 1.0 for rec in out.values())


# ---------------------------------------------------------------------------
# citation-resolve-lookup-3: a multi-batch fetch must pause BETWEEN batches
# (entrez parity) so the unauthenticated iCite endpoint is not hammered. The
# pause fires N-1 times for N batches (never after the last), and a single
# batch never sleeps.
# ---------------------------------------------------------------------------

def test_fetch_icite_sleeps_between_batches(monkeypatch):
    monkeypatch.setattr(icite, "_BATCH", 2)
    sleeps = {"n": 0}
    monkeypatch.setattr(icite, "_sleep_between_batches",
                        lambda: sleeps.__setitem__("n", sleeps["n"] + 1))

    def fake_get(url, timeout=icite._TIMEOUT, **_kw):
        ids = _pmids_from_url(url)
        data = [{"pmid": int(p), "relative_citation_ratio": 1.0,
                 "citation_count": 1, "nih_percentile": 1.0, "year": 2020,
                 "authors": []} for p in ids]
        return json.dumps({"data": data}).encode("utf-8")

    monkeypatch.setattr(icite, "_http_get", fake_get)
    # 5 pmids @ _BATCH=2 => 3 batches => exactly 2 inter-batch pauses.
    icite.fetch_icite(["1", "2", "3", "4", "5"])
    assert sleeps["n"] == 2


def test_fetch_icite_single_batch_no_sleep(monkeypatch):
    sleeps = {"n": 0}
    monkeypatch.setattr(icite, "_sleep_between_batches",
                        lambda: sleeps.__setitem__("n", sleeps["n"] + 1))
    _patch_http(monkeypatch, single={"data": [
        {"pmid": 1, "relative_citation_ratio": 2.0, "citation_count": 5,
         "nih_percentile": 80.0, "year": 2019, "authors": []}]})
    icite.fetch_icite(["1"])
    assert sleeps["n"] == 0


def test_fetch_icite_empty_input_returns_empty():
    assert icite.fetch_icite([]) == {}
    assert icite.fetch_icite(None) == {}


def test_fetch_icite_non_numeric_pmids_dropped(monkeypatch):
    _patch_http(monkeypatch, single={"data": []})
    # Non-digit "pmids" are filtered before the call; nothing left => {}.
    assert icite.fetch_icite(["PMC123", "abc", ""]) == {}


def test_fetch_icite_network_error_returns_empty(monkeypatch):
    """_http_get returns None on any failure => {}."""
    monkeypatch.setattr(icite, "_http_get", lambda url, timeout=icite._TIMEOUT, **_kw: None)
    assert icite.fetch_icite(["123", "456"]) == {}


def test_fetch_icite_bad_json_returns_empty(monkeypatch):
    monkeypatch.setattr(icite, "_http_get",
                        lambda url, timeout=icite._TIMEOUT, **_kw: b"<html>not json</html>")
    assert icite.fetch_icite(["123"]) == {}


def test_fetch_icite_missing_data_key_returns_empty(monkeypatch):
    monkeypatch.setattr(icite, "_http_get",
                        lambda url, timeout=icite._TIMEOUT, **_kw: json.dumps({"meta": {}}).encode())
    assert icite.fetch_icite(["123"]) == {}


def test_fetch_icite_record_without_pmid_skipped(monkeypatch):
    payload = {"data": [{"relative_citation_ratio": 2.0}, {"pmid": 999, "relative_citation_ratio": 1.5}]}
    monkeypatch.setattr(icite, "_http_get",
                        lambda url, timeout=icite._TIMEOUT, **_kw: json.dumps(payload).encode())
    out = icite.fetch_icite(["999"])
    assert set(out) == {"999"}
    assert out["999"]["rcr"] == 1.5


def test_fetch_icite_string_authors_fallback(monkeypatch):
    """Older/odd iCite shape: authors as a comma-separated string."""
    payload = {"data": [{"pmid": 7, "authors": "Alexander L Cohen, Michael D Fox"}]}
    monkeypatch.setattr(icite, "_http_get",
                        lambda url, timeout=icite._TIMEOUT, **_kw: json.dumps(payload).encode())
    out = icite.fetch_icite(["7"])
    auths = out["7"]["authors"]
    assert len(auths) == 2
    assert auths[0]["fullName"] == "Alexander L Cohen"


def test_fetch_icite_partial_batch_failure_keeps_good(monkeypatch):
    """One batch errors (None), the other succeeds => partial result, no raise."""
    monkeypatch.setattr(icite, "_BATCH", 1)
    monkeypatch.setattr(icite, "_sleep_between_batches", lambda: None)

    def fake_get(url, timeout=icite._TIMEOUT, **_kw):
        if "pmids=2" in url:
            return None  # simulate failure for the 2nd batch
        return json.dumps({"data": [{"pmid": 1, "relative_citation_ratio": 4.0}]}).encode()

    monkeypatch.setattr(icite, "_http_get", fake_get)
    out = icite.fetch_icite(["1", "2"])
    assert set(out) == {"1"}
    assert out["1"]["rcr"] == 4.0


# ---------------------------------------------------------------------------
# FIX 1 regression: non-dict JSON bodies must never raise
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body", [b"[]", b"null", b'"x"', b"123"])
def test_fetch_icite_non_dict_json_returns_empty(monkeypatch, body):
    """Valid-but-non-dict JSON (bare array, null, string, int) must return {}."""
    monkeypatch.setattr(icite, "_http_get", lambda url, timeout=icite._TIMEOUT, **_kw: body)
    result = icite.fetch_icite(["123"])
    assert result == {}, f"expected {{}} for body={body!r}, got {result!r}"


# ---------------------------------------------------------------------------
# F5 — fetch_icite_status: a partial (some-batches-failed) result must be
# distinguishable from "those PMIDs are unknown to iCite". Back-compat: the
# bare fetch_icite still returns just the dict.
# ---------------------------------------------------------------------------

def test_fetch_icite_status_clean_not_degraded(monkeypatch):
    _patch_http(monkeypatch, single={"data": [
        {"pmid": 1, "relative_citation_ratio": 2.0, "citation_count": 5,
         "nih_percentile": 80.0, "year": 2019, "authors": []}]})
    out, status = icite.fetch_icite_status(["1"])
    assert "1" in out
    assert status["degraded"] is False
    assert status["n_failed_batches"] == 0
    assert status["n_batches"] == 1


def test_fetch_icite_status_partial_batch_failure_is_degraded(monkeypatch):
    # Two batches: first OK, second fetch fails (None). The map is partial AND
    # the status flags it degraded (the missing pmids are not "unknown to iCite").
    monkeypatch.setattr(icite, "_BATCH", 2)
    monkeypatch.setattr(icite, "_sleep_between_batches", lambda: None)

    def fake_get(url, timeout=icite._TIMEOUT, **_kw):
        ids = _pmids_from_url(url)
        if "3" in ids or "4" in ids:
            return None  # second batch fetch failed
        data = [{"pmid": int(p), "relative_citation_ratio": 1.0,
                 "citation_count": 1, "nih_percentile": 1.0, "year": 2020,
                 "authors": []} for p in ids]
        return json.dumps({"data": data}).encode("utf-8")

    monkeypatch.setattr(icite, "_http_get", fake_get)
    out, status = icite.fetch_icite_status(["1", "2", "3", "4"])
    assert set(out) == {"1", "2"}                 # partial
    assert status["degraded"] is True
    assert status["n_failed_batches"] == 1
    assert status["n_batches"] == 2
    # back-compat: bare function returns just the partial dict, no status.
    assert set(icite.fetch_icite(["1", "2", "3", "4"])) == {"1", "2"}


def test_fetch_icite_status_empty_input_not_degraded():
    out, status = icite.fetch_icite_status([])
    assert out == {}
    assert status["degraded"] is False
