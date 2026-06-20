"""Tests for zoterocite._http — the one shared GET primitive.

Fully offline: ``urllib.request.urlopen`` is monkeypatched so NO real request
is made, and the retry back-off ``sleep`` is injected as a no-op so NO real
sleeping happens.  We assert:

* a single success returns the body bytes;
* a 429 with ``Retry-After`` is retried exactly once, then succeeds;
* a persistent 5xx returns ``None`` (never raises) after one retry;
* a ``URLError`` returns ``None`` (never raises) and is NOT retried;
* the never-raise contract holds for an arbitrary unexpected error;
* ``retries=0`` disables retrying.
"""
from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from zoterocite import _http


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


def _http_error(code: int, retry_after=None):
    """Build an HTTPError with a real headers object carrying Retry-After."""
    import email.message

    hdrs = email.message.Message()
    if retry_after is not None:
        hdrs["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError(
        url="http://example/x", code=code, msg="err", hdrs=hdrs, fp=None
    )


def _patch_urlopen(monkeypatch, side_effects):
    """Drive urlopen from a list of *side_effects* (one per attempt).

    Each item is either a ``bytes`` body (-> success) or an ``Exception``
    instance (-> raised).  Records how many times urlopen was called.
    """
    calls = {"n": 0}

    def fake(req, timeout=None):
        i = calls["n"]
        calls["n"] += 1
        effect = side_effects[min(i, len(side_effects) - 1)]
        if isinstance(effect, BaseException):
            raise effect
        return _FakeResp(effect)

    monkeypatch.setattr(_http.urllib.request, "urlopen", fake)
    return calls


def _no_sleep():
    """A sleep stand-in that records calls but never actually sleeps."""
    waits: list = []
    return waits, (lambda secs: waits.append(secs))


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------

def test_single_success_returns_bytes(monkeypatch):
    _patch_urlopen(monkeypatch, [b"hello-body"])
    waits, sleep = _no_sleep()
    out = _http.http_get("http://x/", timeout=10, _sleep=sleep)
    assert out == b"hello-body"
    assert waits == []  # no retry, no sleep


def test_default_user_agent_and_header_override(monkeypatch):
    captured = {}

    def fake(req, timeout=None):
        captured["ua"] = req.get_header("User-agent")
        captured["accept"] = req.get_header("Accept")
        return _FakeResp(b"ok")

    monkeypatch.setattr(_http.urllib.request, "urlopen", fake)
    _http.http_get(
        "http://x/", timeout=5, headers={"Accept": "application/json"}
    )
    assert captured["ua"] == _http._DEFAULT_UA
    assert captured["accept"] == "application/json"

    # Caller may override the default UA.
    _http.http_get("http://x/", timeout=5, headers={"User-Agent": "custom/9"})
    assert captured["ua"] == "custom/9"


# ---------------------------------------------------------------------------
# Retry path — 429 with Retry-After, retried exactly once, then succeeds
# ---------------------------------------------------------------------------

def test_retries_once_on_429_then_succeeds(monkeypatch):
    calls = _patch_urlopen(
        monkeypatch,
        [_http_error(429, retry_after=2), b"after-retry"],
    )
    waits, sleep = _no_sleep()
    out = _http.http_get("http://x/", timeout=10, retries=1, _sleep=sleep)
    assert out == b"after-retry"
    assert calls["n"] == 2          # exactly one retry (2 attempts total)
    assert waits == [2.0]           # honoured Retry-After: 2, clamped to <= 3


def test_retry_after_clamped_to_max(monkeypatch):
    _patch_urlopen(
        monkeypatch,
        [_http_error(503, retry_after=9999), b"ok"],
    )
    waits, sleep = _no_sleep()
    out = _http.http_get("http://x/", timeout=10, _sleep=sleep)
    assert out == b"ok"
    assert waits == [_http._MAX_RETRY_WAIT]  # clamped, never a long sleep


def test_missing_retry_after_uses_small_default(monkeypatch):
    _patch_urlopen(monkeypatch, [_http_error(429, retry_after=None), b"ok"])
    waits, sleep = _no_sleep()
    out = _http.http_get("http://x/", timeout=10, _sleep=sleep)
    assert out == b"ok"
    assert waits == [_http._DEFAULT_RETRY_WAIT]


# ---------------------------------------------------------------------------
# Failure paths — never raise, return None
# ---------------------------------------------------------------------------

def test_persistent_5xx_returns_none(monkeypatch):
    calls = _patch_urlopen(
        monkeypatch,
        [_http_error(500, retry_after=1), _http_error(500, retry_after=1)],
    )
    waits, sleep = _no_sleep()
    out = _http.http_get("http://x/", timeout=10, retries=1, _sleep=sleep)
    assert out is None              # never raises
    assert calls["n"] == 2          # original + one retry, then give up
    assert waits == [1.0]           # slept once before the (failed) retry


def test_urlerror_returns_none_and_not_retried(monkeypatch):
    calls = _patch_urlopen(
        monkeypatch,
        [urllib.error.URLError("offline"), b"never-reached"],
    )
    waits, sleep = _no_sleep()
    out = _http.http_get("http://x/", timeout=10, retries=1, _sleep=sleep)
    assert out is None
    assert calls["n"] == 1          # URLError is NOT retried
    assert waits == []              # no sleep


def test_arbitrary_exception_returns_none(monkeypatch):
    _patch_urlopen(monkeypatch, [ValueError("boom")])
    out = _http.http_get("http://x/", timeout=10)
    assert out is None              # never propagates


def test_non_retryable_http_status_returns_none(monkeypatch):
    # 404 is not in _RETRY_STATUSES -> no retry, returns None.
    calls = _patch_urlopen(monkeypatch, [_http_error(404), b"never"])
    waits, sleep = _no_sleep()
    out = _http.http_get("http://x/", timeout=10, _sleep=sleep)
    assert out is None
    assert calls["n"] == 1
    assert waits == []


def test_retries_zero_disables_retry(monkeypatch):
    calls = _patch_urlopen(monkeypatch, [_http_error(429, retry_after=1), b"x"])
    waits, sleep = _no_sleep()
    out = _http.http_get("http://x/", timeout=10, retries=0, _sleep=sleep)
    assert out is None              # retryable status but no retries allowed
    assert calls["n"] == 1
    assert waits == []


# ---------------------------------------------------------------------------
# Retry-After parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value,expected",
    [
        ("2", 2.0),
        ("0", 0.0),
        ("  3 ", 3.0),
        ("100", _http._MAX_RETRY_WAIT),       # clamped
        ("-5", _http._DEFAULT_RETRY_WAIT),     # negative -> default
        ("Wed, 21 Oct 2099 07:28:00 GMT", _http._DEFAULT_RETRY_WAIT),  # HTTP-date -> default
        (None, _http._DEFAULT_RETRY_WAIT),
        ("garbage", _http._DEFAULT_RETRY_WAIT),
    ],
)
def test_parse_retry_after(value, expected):
    assert _http._parse_retry_after(value) == expected


# ---------------------------------------------------------------------------
# W9-1 — single-owner contact email / User-Agent
# ---------------------------------------------------------------------------

def test_contact_email_default_no_env(monkeypatch):
    monkeypatch.delenv("ZOTERO_WORD_CITE_CONTACT_EMAIL", raising=False)
    monkeypatch.delenv("NCBI_EMAIL", raising=False)
    assert _http.contact_email() == _http._DEFAULT_EMAIL


def test_contact_email_zoterocite_override(monkeypatch):
    monkeypatch.delenv("NCBI_EMAIL", raising=False)
    monkeypatch.setenv("ZOTERO_WORD_CITE_CONTACT_EMAIL", "lab@example.org")
    assert _http.contact_email() == "lab@example.org"


def test_contact_email_zoterocite_beats_ncbi(monkeypatch):
    # In the shared owner, ZOTERO_WORD_CITE_CONTACT_EMAIL takes precedence over NCBI_EMAIL.
    monkeypatch.setenv("ZOTERO_WORD_CITE_CONTACT_EMAIL", "lab@example.org")
    monkeypatch.setenv("NCBI_EMAIL", "ncbi@example.org")
    assert _http.contact_email() == "lab@example.org"


def test_contact_email_falls_through_to_ncbi(monkeypatch):
    monkeypatch.delenv("ZOTERO_WORD_CITE_CONTACT_EMAIL", raising=False)
    monkeypatch.setenv("NCBI_EMAIL", "ncbi@example.org")
    assert _http.contact_email() == "ncbi@example.org"


def test_user_agent_reflects_contact_email(monkeypatch):
    monkeypatch.delenv("ZOTERO_WORD_CITE_CONTACT_EMAIL", raising=False)
    monkeypatch.delenv("NCBI_EMAIL", raising=False)
    # No env: byte-identical to the historical default UA.
    assert _http.user_agent() == _http._DEFAULT_UA
    # Override: the UA carries the overridden mailto.
    monkeypatch.setenv("ZOTERO_WORD_CITE_CONTACT_EMAIL", "lab@example.org")
    assert _http.user_agent() == "zotero-word-cite/1.0 (mailto:lab@example.org)"
    assert "lab@example.org" in _http.user_agent()


def test_user_agent_read_at_call_time(monkeypatch):
    # The env var is read on each call, not frozen at import.
    monkeypatch.delenv("ZOTERO_WORD_CITE_CONTACT_EMAIL", raising=False)
    monkeypatch.delenv("NCBI_EMAIL", raising=False)
    assert _http.contact_email() == _http._DEFAULT_EMAIL
    monkeypatch.setenv("ZOTERO_WORD_CITE_CONTACT_EMAIL", "after@example.org")
    assert _http.contact_email() == "after@example.org"


def test_http_get_default_ua_honours_override(monkeypatch):
    # A caller that passes NO User-Agent header still gets the overridden mailto,
    # because http_get computes its default UA via user_agent() at call time.
    monkeypatch.delenv("NCBI_EMAIL", raising=False)
    monkeypatch.setenv("ZOTERO_WORD_CITE_CONTACT_EMAIL", "lab@example.org")
    captured = {}

    def fake(req, timeout=None):
        captured["ua"] = req.get_header("User-agent")
        return _FakeResp(b"ok")

    monkeypatch.setattr(_http.urllib.request, "urlopen", fake)
    _http.http_get("http://x/", timeout=5)
    assert captured["ua"] == "zotero-word-cite/1.0 (mailto:lab@example.org)"


def test_consumer_ua_reflects_override(monkeypatch):
    # End-to-end: a representative consumer (icite) sends the overridden email in
    # its request User-Agent when the env var is set. We patch the real urlopen
    # (not http_get) so the consumer's headers actually flow into the request.
    import zoterocite.icite as icite

    monkeypatch.delenv("NCBI_EMAIL", raising=False)
    monkeypatch.setenv("ZOTERO_WORD_CITE_CONTACT_EMAIL", "lab@example.org")
    captured = {}

    def fake(req, timeout=None):
        captured["ua"] = req.get_header("User-agent")
        return _FakeResp(b'{"data": []}')

    monkeypatch.setattr(_http.urllib.request, "urlopen", fake)
    icite.fetch_icite(["12345678"])
    assert captured["ua"] == "zotero-word-cite/1.0 (mailto:lab@example.org)"


def test_http_get_rejects_non_http_scheme(monkeypatch):
    # The shared fetch primitive must never open a file:// (or other non-web) URL.
    import urllib.request
    from zoterocite import _http
    def _boom(*a, **k):
        raise AssertionError("urlopen must not be called for a non-http scheme")
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    assert _http.http_get("file:///etc/passwd", timeout=1) is None
    assert _http.http_get("ftp://example.com/x", timeout=1) is None
