"""Offline tests for the open-access PDF cascade + SSRF guard (zoterocite.oapdf)
and its opt-in attach wiring through zoterocite.zotero.create_items.

Fully offline: no real network is touched.  The cascade's JSON GETs go through
``zoterocite._http.http_get`` (monkeypatched to canned bodies); the guarded PDF
fetch's host resolution (``oapdf.socket.getaddrinfo``) and HTTP opener are both
monkeypatched.
"""
from __future__ import annotations

import io
import json

import pytest

from zoterocite import oapdf
from zoterocite import zotero


# ---------------------------------------------------------------------------
# Helpers: canned JSON for the cascade (routes through _http.http_get)
# ---------------------------------------------------------------------------

def _route_http_get(monkeypatch, mapping):
    """Patch oapdf._http.http_get to answer from a {url-substring: dict|None} map.

    The first substring that appears in the requested URL wins; an explicit
    ``None`` value means "this source returns no usable body" (the GET failed).
    A URL matching no key returns None (offline default).
    """
    def fake_get(url, *, timeout, headers=None, retries=1, _sleep=None):
        for needle, payload in mapping.items():
            if needle in url:
                if payload is None:
                    return None
                return json.dumps(payload).encode("utf-8")
        return None

    monkeypatch.setattr(oapdf._http, "http_get", fake_get)


# ---------------------------------------------------------------------------
# Cascade: each source path yields a URL
# ---------------------------------------------------------------------------

def test_cascade_unpaywall_best_oa_location(monkeypatch):
    _route_http_get(monkeypatch, {
        "api.unpaywall.org": {
            "best_oa_location": {"url_for_pdf": "https://oa.example.org/paper.pdf"},
        },
    })
    url = oapdf.find_oa_pdf_url(doi="10.1234/abc")
    assert url == "https://oa.example.org/paper.pdf"


def test_cascade_unpaywall_alternate_then_landing(monkeypatch):
    # best_oa_location has no url_for_pdf; fall to alternate oa_locations.
    _route_http_get(monkeypatch, {
        "api.unpaywall.org": {
            "best_oa_location": {"url": "https://landing.example.org/x"},
            "oa_locations": [
                {"url_for_pdf": "https://alt.example.org/paper.pdf"},
            ],
        },
    })
    url = oapdf.find_oa_pdf_url(doi="10.1234/abc")
    assert url == "https://alt.example.org/paper.pdf"


def test_cascade_arxiv_from_crossref_relation(monkeypatch):
    # Unpaywall returns nothing; Crossref relation carries an arXiv id.
    _route_http_get(monkeypatch, {
        "api.unpaywall.org": {},  # no OA location
        "api.crossref.org": {
            "message": {
                "relation": {
                    "has-preprint": [{"id-type": "arxiv", "id": "2401.01234"}],
                },
            },
        },
    })
    url = oapdf.find_oa_pdf_url(doi="10.1234/abc")
    assert url == "https://arxiv.org/pdf/2401.01234.pdf"


def test_cascade_arxiv_from_crossref_alternative_id(monkeypatch):
    _route_http_get(monkeypatch, {
        "api.unpaywall.org": {},
        "api.crossref.org": {
            "message": {"alternative-id": ["2312.99999"]},
        },
    })
    url = oapdf.find_oa_pdf_url(doi="10.1234/abc")
    assert url == "https://arxiv.org/pdf/2312.99999.pdf"


def test_cascade_semantic_scholar(monkeypatch):
    # Unpaywall + Crossref dry; S2 has openAccessPdf.
    _route_http_get(monkeypatch, {
        "api.unpaywall.org": {},
        "api.crossref.org": {"message": {}},
        "api.semanticscholar.org": {
            "openAccessPdf": {"url": "https://s2.example.org/paper.pdf"},
        },
    })
    url = oapdf.find_oa_pdf_url(doi="10.1234/abc")
    assert url == "https://s2.example.org/paper.pdf"


def test_cascade_pmc_via_doi(monkeypatch):
    # All of the DOI-only sources dry; PMC idconv resolves a PMCID.
    _route_http_get(monkeypatch, {
        "api.unpaywall.org": {},
        "api.crossref.org": {"message": {}},
        "api.semanticscholar.org": {},
        "idconv": {"records": [{"pmcid": "PMC1234567"}]},
    })
    url = oapdf.find_oa_pdf_url(doi="10.1234/abc")
    assert url == "https://pmc.ncbi.nlm.nih.gov/articles/PMC1234567/pdf/"


def test_cascade_pmc_via_pmid_only(monkeypatch):
    # No DOI at all — PMID drives the PMC idconv path.
    _route_http_get(monkeypatch, {
        "idconv": {"records": [{"pmcid": "PMC7654321"}]},
    })
    url = oapdf.find_oa_pdf_url(pmid="32000000")
    assert url == "https://pmc.ncbi.nlm.nih.gov/articles/PMC7654321/pdf/"


def test_cascade_arxiv_direct_short_circuits(monkeypatch):
    # A direct arXiv id wins with NO network at all.
    def boom(*a, **k):  # pragma: no cover — must not be called
        raise AssertionError("network should not be touched for a direct arXiv id")

    monkeypatch.setattr(oapdf._http, "http_get", boom)
    url = oapdf.find_oa_pdf_url(arxiv="2401.05555")
    assert url == "https://arxiv.org/pdf/2401.05555.pdf"


def test_cascade_returns_none_when_all_dry(monkeypatch):
    _route_http_get(monkeypatch, {
        "api.unpaywall.org": {},
        "api.crossref.org": {"message": {}},
        "api.semanticscholar.org": {},
        "idconv": {"records": []},
    })
    assert oapdf.find_oa_pdf_url(doi="10.1234/abc") is None


def test_cascade_offline_degrades_to_none(monkeypatch):
    # Every GET fails (offline) -> None, never raises.
    monkeypatch.setattr(oapdf._http, "http_get",
                        lambda *a, **k: None)
    assert oapdf.find_oa_pdf_url(doi="10.1234/abc", pmid="1", arxiv=None) is None


def test_cascade_uses_our_contact_email(monkeypatch):
    captured = {}

    def fake_get(url, *, timeout, headers=None, retries=1, _sleep=None):
        captured["url"] = url
        return json.dumps({}).encode("utf-8")

    monkeypatch.setenv("ZOTERO_WORD_CITE_CONTACT_EMAIL", "lab@example.edu")
    monkeypatch.setattr(oapdf._http, "http_get", fake_get)
    oapdf.find_oa_pdf_url(doi="10.1/x")
    assert "lab%40example.edu" in captured["url"] or "lab@example.edu" in captured["url"]
    # And NOT the upstream hard-coded noreply address.
    assert "noreply" not in captured["url"]


# ---------------------------------------------------------------------------
# SSRF guard: _url_resolves_to_public_host
# ---------------------------------------------------------------------------

def _patch_getaddrinfo(monkeypatch, host_to_ip):
    """Patch oapdf.socket.getaddrinfo to map hostname -> a single IP string.

    A host absent from the map raises gaierror (unresolvable).
    """
    import socket as _socket

    def fake(host, port, *a, **k):
        if host in host_to_ip:
            ip = host_to_ip[host]
            return [(2, 1, 6, "", (ip, port or 0))]
        raise _socket.gaierror("name not resolved")

    monkeypatch.setattr(oapdf.socket, "getaddrinfo", fake)


@pytest.mark.parametrize("ip", [
    "127.0.0.1",        # loopback
    "10.0.0.5",         # private
    "192.168.1.1",      # private
    "172.16.0.1",       # private
    "169.254.169.254",  # link-local / cloud-metadata
    "0.0.0.0",          # reserved
    "::1",              # IPv6 loopback
    "fd00::1",          # IPv6 unique-local
])
def test_ssrf_rejects_non_public_ip(monkeypatch, ip):
    _patch_getaddrinfo(monkeypatch, {"evil.example.com": ip})
    assert oapdf._url_resolves_to_public_host("https://evil.example.com/x.pdf") is False


def test_ssrf_accepts_public_ip(monkeypatch):
    _patch_getaddrinfo(monkeypatch, {"oa.example.org": "93.184.216.34"})
    assert oapdf._url_resolves_to_public_host("https://oa.example.org/x.pdf") is True


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://host/x",
    "gopher://host/x",
    "https://",            # no hostname
])
def test_ssrf_rejects_bad_scheme_or_no_host(monkeypatch, url):
    # getaddrinfo should never even be reached for a bad scheme; if it is, fail.
    _patch_getaddrinfo(monkeypatch, {})
    assert oapdf._url_resolves_to_public_host(url) is False


def test_ssrf_rejects_unresolvable_host(monkeypatch):
    _patch_getaddrinfo(monkeypatch, {})  # nothing resolves
    assert oapdf._url_resolves_to_public_host("https://nope.invalid/x.pdf") is False


# ---------------------------------------------------------------------------
# Guarded GET: fetch_pdf_guarded — content-type / size / redirect handling
# ---------------------------------------------------------------------------

class _FakeHTTPResp:
    """Minimal urllib-response stand-in for a terminal (2xx) response."""

    def __init__(self, body: bytes, content_type="application/pdf", status=200):
        self._body = body
        self.status = status
        import email.message
        self.headers = email.message.Message()
        self.headers["Content-Type"] = content_type

    def read(self, n=-1):
        if n is None or n < 0:
            return self._body
        return self._body[:n]

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_opener(monkeypatch, responder):
    """Patch build_opener so opener.open(req, timeout=...) -> responder(req)."""
    class _Opener:
        def open(self, req, timeout=None):
            return responder(req)

    monkeypatch.setattr(oapdf.urllib.request, "build_opener", lambda *a, **k: _Opener())


_GOOD_PDF = b"%PDF-1.7\n" + b"x" * 4000  # > _MIN_PDF_BYTES, valid-ish


def test_guarded_get_returns_pdf_bytes(monkeypatch):
    _patch_getaddrinfo(monkeypatch, {"oa.example.org": "93.184.216.34"})
    _patch_opener(monkeypatch, lambda req: _FakeHTTPResp(_GOOD_PDF))
    out = oapdf.fetch_pdf_guarded("https://oa.example.org/p.pdf")
    assert out == _GOOD_PDF


def test_guarded_get_rejects_wrong_content_type(monkeypatch):
    _patch_getaddrinfo(monkeypatch, {"oa.example.org": "93.184.216.34"})
    _patch_opener(
        monkeypatch,
        lambda req: _FakeHTTPResp(b"<html>not a pdf</html>" * 100, content_type="text/html"),
    )
    assert oapdf.fetch_pdf_guarded("https://oa.example.org/p.pdf") is None


def test_guarded_get_rejects_too_small(monkeypatch):
    _patch_getaddrinfo(monkeypatch, {"oa.example.org": "93.184.216.34"})
    _patch_opener(monkeypatch, lambda req: _FakeHTTPResp(b"%PDF-1.7 tiny"))
    assert oapdf.fetch_pdf_guarded("https://oa.example.org/p.pdf") is None


def test_guarded_get_rejects_oversize(monkeypatch):
    _patch_getaddrinfo(monkeypatch, {"oa.example.org": "93.184.216.34"})
    big = b"%PDF-1.7\n" + b"x" * (oapdf._MAX_PDF_BYTES + 10)
    _patch_opener(monkeypatch, lambda req: _FakeHTTPResp(big))
    assert oapdf.fetch_pdf_guarded("https://oa.example.org/p.pdf") is None


def test_guarded_get_rejects_private_host_before_connect(monkeypatch):
    # Host resolves to loopback — must be rejected; opener must never be called.
    _patch_getaddrinfo(monkeypatch, {"oa.example.org": "127.0.0.1"})

    def boom(req, timeout=None):  # pragma: no cover
        raise AssertionError("opener.open must not run for a private host")

    _patch_opener(monkeypatch, boom)
    assert oapdf.fetch_pdf_guarded("https://oa.example.org/p.pdf") is None


def test_guarded_get_rejects_redirect_to_private_host(monkeypatch):
    """A first hop on a public host that 302-redirects to a private host must
    be rejected on re-validation of the second hop."""
    _patch_getaddrinfo(monkeypatch, {
        "public.example.org": "93.184.216.34",
        "internal.evil": "169.254.169.254",  # cloud metadata
    })

    import urllib.error
    import email.message

    def responder(req):
        url = req.full_url
        if "public.example.org" in url:
            hdrs = email.message.Message()
            hdrs["Location"] = "https://internal.evil/secret"
            raise urllib.error.HTTPError(
                url=url, code=302, msg="redir", hdrs=hdrs, fp=io.BytesIO(b""),
            )
        # If we ever reach the private host, return a "PDF" — the guard must
        # have already rejected it, so this body must NOT come back.
        return _FakeHTTPResp(_GOOD_PDF)

    _patch_opener(monkeypatch, responder)
    assert oapdf.fetch_pdf_guarded("https://public.example.org/p.pdf") is None


def test_guarded_get_follows_redirect_to_public_host(monkeypatch):
    """A redirect to another PUBLIC host is followed and yields the PDF."""
    _patch_getaddrinfo(monkeypatch, {
        "a.example.org": "93.184.216.34",
        "b.example.org": "93.184.216.35",
    })
    import urllib.error
    import email.message

    def responder(req):
        if "a.example.org" in req.full_url:
            hdrs = email.message.Message()
            hdrs["Location"] = "https://b.example.org/real.pdf"
            raise urllib.error.HTTPError(
                url=req.full_url, code=302, msg="redir", hdrs=hdrs, fp=io.BytesIO(b""),
            )
        return _FakeHTTPResp(_GOOD_PDF)

    _patch_opener(monkeypatch, responder)
    assert oapdf.fetch_pdf_guarded("https://a.example.org/p.pdf") == _GOOD_PDF


# ---------------------------------------------------------------------------
# TEETH: a mutation that disables the SSRF host check must make a guard test FAIL
# ---------------------------------------------------------------------------

def test_ssrf_guard_has_teeth_when_disabled(monkeypatch):
    """Mutation test: if ``_url_resolves_to_public_host`` is neutered to always
    return True, fetch_pdf_guarded would happily fetch from a loopback host —
    proving the real guard is load-bearing. This test asserts the *neutered*
    behaviour, so if someone removes the guard call in production code, the
    parametrized rejection tests above break instead of silently passing.
    """
    # 1) With the REAL guard, a loopback host is rejected (no bytes).
    _patch_getaddrinfo(monkeypatch, {"oa.example.org": "127.0.0.1"})
    _patch_opener(monkeypatch, lambda req: _FakeHTTPResp(_GOOD_PDF))
    assert oapdf.fetch_pdf_guarded("https://oa.example.org/p.pdf") is None

    # 2) Now NEUTER the guard. The fetch should now succeed against loopback,
    #    demonstrating the guard is what was blocking it (i.e. it has teeth).
    monkeypatch.setattr(oapdf, "_url_resolves_to_public_host", lambda url: True)
    assert oapdf.fetch_pdf_guarded("https://oa.example.org/p.pdf") == _GOOD_PDF


# ---------------------------------------------------------------------------
# Attach wiring through zotero.create_items(attach_pdfs=...)
# ---------------------------------------------------------------------------

def _setup_zotero_env(monkeypatch):
    monkeypatch.setenv("ZOTERO_API_KEY", "k")
    monkeypatch.setenv("ZOTERO_GROUP_ID", "999")
    monkeypatch.delenv("ZOTERO_USER_ID", raising=False)


def test_attach_off_by_default_no_pdf_calls(monkeypatch):
    """attach_pdfs defaults False: no OA lookup, no pdf_* keys in the result."""
    _setup_zotero_env(monkeypatch)
    monkeypatch.setattr(zotero, "key_can_write_status", lambda: True)
    monkeypatch.setattr(zotero, "ensure_collection", lambda name: "COLLKEY")
    monkeypatch.setattr(zotero, "get_item_by_doi", lambda *a, **k: None)
    monkeypatch.setattr(zotero, "_title_exists_in_library", lambda t: None)
    monkeypatch.setattr(
        zotero, "_post_json",
        lambda path, body, **k: {"successful": {"0": {"key": "ITEMKEY"}}},
    )

    def boom(*a, **k):  # pragma: no cover
        raise AssertionError("find_oa_pdf_url must not run when attach_pdfs is off")

    monkeypatch.setattr(oapdf, "find_oa_pdf_url", boom)

    out = zotero.create_items([{"title": "T", "doi": "10.1/x"}], dedup=False)
    assert out["created"] and out["created"][0]["key"] == "ITEMKEY"
    assert "pdf_attached" not in out
    assert "pdf_skipped" not in out


def test_attach_runs_after_create_and_records_success(monkeypatch):
    _setup_zotero_env(monkeypatch)
    monkeypatch.setattr(zotero, "key_can_write_status", lambda: True)
    monkeypatch.setattr(zotero, "get_item_by_doi", lambda *a, **k: None)
    monkeypatch.setattr(zotero, "_title_exists_in_library", lambda t: None)
    monkeypatch.setattr(
        zotero, "_post_json",
        lambda path, body, **k: {"successful": {"0": {"key": "ITEMKEY"}}},
    )
    monkeypatch.setattr(oapdf, "find_oa_pdf_url",
                        lambda **k: "https://oa.example.org/p.pdf")
    monkeypatch.setattr(oapdf, "fetch_pdf_guarded", lambda url, **k: _GOOD_PDF)

    attached = {}

    def fake_attach(key, pdf_bytes, filename, **k):
        attached["key"] = key
        attached["filename"] = filename
        attached["bytes"] = pdf_bytes
        return True

    monkeypatch.setattr(zotero, "attach_pdf_to_item", fake_attach)

    out = zotero.create_items(
        [{"title": "T", "doi": "10.1/x"}], dedup=False, attach_pdfs=True,
    )
    assert out["created"][0]["key"] == "ITEMKEY"
    assert out["pdf_attached"] == [{"key": "ITEMKEY", "source_url": "https://oa.example.org/p.pdf"}]
    assert attached["key"] == "ITEMKEY"
    assert attached["bytes"] == _GOOD_PDF
    assert attached["filename"].endswith(".pdf")


def test_attach_graceful_when_no_pdf_found(monkeypatch):
    """No OA PDF: the create still succeeds; outcome recorded under pdf_skipped."""
    _setup_zotero_env(monkeypatch)
    monkeypatch.setattr(zotero, "key_can_write_status", lambda: True)
    monkeypatch.setattr(zotero, "get_item_by_doi", lambda *a, **k: None)
    monkeypatch.setattr(zotero, "_title_exists_in_library", lambda t: None)
    monkeypatch.setattr(
        zotero, "_post_json",
        lambda path, body, **k: {"successful": {"0": {"key": "ITEMKEY"}}},
    )
    monkeypatch.setattr(oapdf, "find_oa_pdf_url", lambda **k: None)

    def boom(*a, **k):  # pragma: no cover
        raise AssertionError("attach must not run when no PDF URL was found")

    monkeypatch.setattr(zotero, "attach_pdf_to_item", boom)

    out = zotero.create_items(
        [{"title": "T", "doi": "10.1/x"}], dedup=False, attach_pdfs=True,
    )
    assert out["created"][0]["key"] == "ITEMKEY"  # add still succeeded
    assert out["pdf_attached"] == []
    assert out["pdf_skipped"] == [{"key": "ITEMKEY", "reason": "no open-access PDF found"}]


def test_attach_not_attempted_when_write_gate_denies(monkeypatch):
    """No write access: create_items refuses BEFORE creating, so no attach runs."""
    _setup_zotero_env(monkeypatch)
    monkeypatch.setattr(zotero, "key_can_write_status", lambda: False)

    def boom_post(*a, **k):  # pragma: no cover
        raise AssertionError("no POST when the key cannot write")

    def boom_find(*a, **k):  # pragma: no cover
        raise AssertionError("no OA lookup when the key cannot write")

    monkeypatch.setattr(zotero, "_post_json", boom_post)
    monkeypatch.setattr(oapdf, "find_oa_pdf_url", boom_find)

    out = zotero.create_items(
        [{"title": "T", "doi": "10.1/x"}], dedup=False, attach_pdfs=True,
    )
    assert out["created"] == []
    assert out["failed"] and "write access" in out["failed"][0]["reason"]


def test_attach_fetch_failure_does_not_break_create(monkeypatch):
    """URL found but the guarded fetch yields no bytes: create still succeeds."""
    _setup_zotero_env(monkeypatch)
    monkeypatch.setattr(zotero, "key_can_write_status", lambda: True)
    monkeypatch.setattr(zotero, "get_item_by_doi", lambda *a, **k: None)
    monkeypatch.setattr(zotero, "_title_exists_in_library", lambda t: None)
    monkeypatch.setattr(
        zotero, "_post_json",
        lambda path, body, **k: {"successful": {"0": {"key": "ITEMKEY"}}},
    )
    monkeypatch.setattr(oapdf, "find_oa_pdf_url",
                        lambda **k: "https://oa.example.org/p.pdf")
    monkeypatch.setattr(oapdf, "fetch_pdf_guarded", lambda url, **k: None)

    def boom(*a, **k):  # pragma: no cover
        raise AssertionError("attach must not run when fetch returned no bytes")

    monkeypatch.setattr(zotero, "attach_pdf_to_item", boom)

    out = zotero.create_items(
        [{"title": "T", "doi": "10.1/x"}], dedup=False, attach_pdfs=True,
    )
    assert out["created"][0]["key"] == "ITEMKEY"
    assert out["pdf_attached"] == []
    assert out["pdf_skipped"][0]["key"] == "ITEMKEY"
    assert "no valid PDF" in out["pdf_skipped"][0]["reason"]
