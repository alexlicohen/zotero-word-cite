"""Tests for the opt-in disk-backed response cache in ``zoterocite._http``.

Fully offline + synthetic: the network is monkeypatched (either the extracted
``_http._do_fetch`` or ``urllib.request.urlopen``) and the cache disk file is
redirected to a tmp path. NO real request is made and the real
``data/http_response_cache.json`` is never touched.

Two guards carry TEETH (mutation-confirmed with ``gf mutate-check``):

* ``test_failure_is_never_cached`` — the never-cache-failure invariant. Mutate the
  store gate in ``_http.http_get`` (``if caching and body is not None:``): flipping
  ``is not``→``is`` (or ``and``→``or``) caches the ``None`` failure as an empty
  body, so the 2nd call is served ``b""`` without a re-fetch → the test reddens.

* ``test_api_key_not_persisted_and_stripped_from_key`` — the api_key never enters
  the cache key or the disk file. Two URLs differing ONLY by ``api_key`` must map
  to the SAME entry; mutate the strip filter in ``_sanitize_url_for_cache``
  (``k.lower() != "api_key"`` → ``==``), i.e. "hash the raw url", and the two
  calls key differently → a 2nd network call happens → the test reddens.
"""
from __future__ import annotations

import json

import pytest

from zoterocite import _http
from zoterocite import refresolve


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------

@pytest.fixture
def cache_file(tmp_path, monkeypatch):
    """Redirect the ``_http`` disk cache to a known tmp path and start cold.

    Requested (not autouse) so it is set up AFTER conftest's autouse
    ``_isolate_http_cache`` and therefore WINS; returns the path so a test can
    read the on-disk cache.
    """
    path = tmp_path / "http_cache.json"
    monkeypatch.setattr(_http, "_HTTP_CACHE_PATH", path, raising=False)
    _http._reset_http_cache()
    return path


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _count_do_fetch(monkeypatch, body):
    """Patch ``_http._do_fetch`` to return *body* and count invocations.

    *body* may be raw bytes/``None`` or a callable ``url -> bytes|None`` for
    per-URL control. Exercises the REAL cache read/write wrapper around the
    (stubbed) network fetch.
    """
    calls = {"n": 0, "urls": []}

    def fake(url, timeout, req_headers, retries, _sleep):
        calls["n"] += 1
        calls["urls"].append(url)
        return body(url) if callable(body) else body

    monkeypatch.setattr(_http, "_do_fetch", fake)
    return calls


def _count_urlopen(monkeypatch, body: bytes):
    """Patch the real ``urlopen`` so the full fetch path (incl. cache) runs."""
    calls = {"n": 0}

    def fake(req, timeout=None):
        calls["n"] += 1
        return _FakeResp(body)

    monkeypatch.setattr(_http.urllib.request, "urlopen", fake)
    return calls


# ---------------------------------------------------------------------------
# GUARD 1 — a failure (None) is NEVER cached-and-served
# ---------------------------------------------------------------------------

def test_failure_is_never_cached(cache_file, monkeypatch):
    """A monkeypatched fetch that always FAILS (returns ``None``) must not be
    cached: the second call re-attempts the network rather than serving a
    poisoned empty.

    TEETH: mutate ``if caching and body is not None:`` in ``_http.http_get``
    (``is not``→``is`` or ``and``→``or``). The mutant caches the ``None`` as an
    empty body, so call #2 is served ``b""`` (not ``None``) WITHOUT a re-fetch →
    both assertions below fail.
    """
    calls = _count_do_fetch(monkeypatch, None)  # every fetch fails
    url = "http://api.example.test/works?query.bibliographic=x"

    assert _http.http_get(url, timeout=5, cache_ttl=1000) is None
    assert _http.http_get(url, timeout=5, cache_ttl=1000) is None
    assert calls["n"] == 2  # re-attempted, NOT served from a poisoned cache

    # Nothing persisted for a failure (the store is never reached).
    assert (not cache_file.exists()) or ('"body"' not in cache_file.read_text())


# ---------------------------------------------------------------------------
# GUARD 2 — the api_key never lands in the cache key or the disk file
# ---------------------------------------------------------------------------

def test_api_key_not_persisted_and_stripped_from_key(cache_file, monkeypatch):
    """Two URLs identical except for the ``api_key`` value map to the SAME cache
    entry (the key strips api_key before hashing), and no api_key value is ever
    written to disk.

    TEETH: mutate ``k.lower() != "api_key"`` in ``_sanitize_url_for_cache``
    (``!=``→``==``) — equivalently "hash the raw url". The two URLs then key
    differently, so call #2 misses and re-fetches → ``calls["n"] == 1`` fails.
    """
    calls = _count_do_fetch(monkeypatch, b'{"ok": 1}')
    base = "http://eutils.example.test/efetch?db=pubmed&id=42"
    u1 = base + "&api_key=SUPERSECRETKEY"
    u2 = base + "&api_key=OTHERKEY999"

    assert _http.http_get(u1, timeout=5, cache_ttl=1000) == b'{"ok": 1}'
    assert _http.http_get(u2, timeout=5, cache_ttl=1000) == b'{"ok": 1}'

    # api_key stripped from the key => both URLs share one entry => 2nd is cached.
    assert calls["n"] == 1

    # Data boundary: the api_key never lands in the on-disk cache (hashed key +
    # a body that does not echo it).
    disk = cache_file.read_text()
    assert "SUPERSECRETKEY" not in disk
    assert "OTHERKEY999" not in disk


# ---------------------------------------------------------------------------
# 3 — a success is cached (network called exactly once)
# ---------------------------------------------------------------------------

def test_success_is_cached(cache_file, monkeypatch):
    calls = _count_do_fetch(monkeypatch, b"BODY-200")
    url = "http://api.example.test/works/10.1000/x"

    assert _http.http_get(url, timeout=5, cache_ttl=1000) == b"BODY-200"
    assert _http.http_get(url, timeout=5, cache_ttl=1000) == b"BODY-200"
    assert calls["n"] == 1
    assert cache_file.exists()


# ---------------------------------------------------------------------------
# 4 — an entry older than cache_ttl re-fetches (never serve stale)
# ---------------------------------------------------------------------------

def test_ttl_expiry_refetches(cache_file, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(_http.time, "time", lambda: clock["t"])
    calls = _count_do_fetch(monkeypatch, b"V")
    url = "http://api.example.test/x"

    assert _http.http_get(url, timeout=5, cache_ttl=100) == b"V"  # stored at ts=1000
    # Within TTL: served from cache, no new fetch.
    clock["t"] = 1000 + 50
    assert _http.http_get(url, timeout=5, cache_ttl=100) == b"V"
    assert calls["n"] == 1
    # Past TTL: stale => re-fetch.
    clock["t"] = 1000 + 100 + 1
    assert _http.http_get(url, timeout=5, cache_ttl=100) == b"V"
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# 5 — cache_ttl=None (the default) never caches: behaviour unchanged
# ---------------------------------------------------------------------------

def test_default_no_caching(cache_file, monkeypatch):
    calls = _count_do_fetch(monkeypatch, b"X")
    url = "http://api.example.test/x"

    _http.http_get(url, timeout=5, cache_ttl=None)  # explicit None
    _http.http_get(url, timeout=5, cache_ttl=None)
    _http.http_get(url, timeout=5)                  # omitted => true default
    _http.http_get(url, timeout=5, cache_ttl=0)     # non-positive => disabled

    assert calls["n"] == 4          # every call hits the network
    assert not cache_file.exists()  # nothing written


def test_refresh_bypasses_read_and_rewrites(cache_file, monkeypatch):
    seq = {"bodies": [b"first", b"second"]}
    # _count_do_fetch increments n BEFORE calling body(url), so index off n-1.
    calls = _count_do_fetch(monkeypatch, lambda url: seq["bodies"][min(calls["n"] - 1, 1)])
    url = "http://api.example.test/x"

    assert _http.http_get(url, timeout=5, cache_ttl=1000) == b"first"
    # refresh=True skips the cache read and forces a live re-fetch + rewrite.
    assert _http.http_get(url, timeout=5, cache_ttl=1000, refresh=True) == b"second"
    assert calls["n"] == 2
    # The rewrite means a subsequent normal read serves the refreshed body.
    assert _http.http_get(url, timeout=5, cache_ttl=1000) == b"second"
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# 6 — end-to-end: resolve_reference serves the 2nd lookup from cache
# ---------------------------------------------------------------------------

def test_resolve_reference_served_from_cache(cache_file, monkeypatch):
    payload = json.dumps({
        "message": {
            "items": [{
                "DOI": "10.1000/xyz",
                "title": ["A Study of Tubers"],
                "author": [{"family": "Smith", "given": "J"}],
                "issued": {"date-parts": [[2020]]},
                "container-title": ["Journal of Things"],
                "type": "journal-article",
                "score": 42.0,
            }]
        }
    }).encode()
    calls = _count_urlopen(monkeypatch, payload)

    # No DOI/PMID in the text => the bibliographic Crossref path (one GET).
    text = "Smith J. A Study of Tubers. Journal of Things. 2020."
    r1 = refresolve.resolve_reference(text)
    r2 = refresolve.resolve_reference(text)

    assert r1["candidates"] and r2["candidates"]
    assert r1 == r2
    assert calls["n"] == 1  # 2nd resolve served from the shared response cache
