"""Open-access PDF resolution + a hardened (SSRF-guarded) PDF fetcher.

Given identifiers we have ALREADY resolved (DOI / PMID / arXiv id), this module
finds the best open-access PDF URL via a four-source cascade and, separately,
downloads it through an SSRF-guarded GET.  It is the load-bearing safety piece
for attaching auto-discovered PDFs: the candidate URL comes from third-party
metadata APIs (Unpaywall / Semantic Scholar / Crossref / PMC) and is therefore
attacker-influenceable, so the fetch is gated by a host/redirect SSRF guard plus
content-type and size validation.

Design contract
---------------
**Never-raise / offline-degrade.**  Every public-API failure, network error, or
parse error degrades to ``None`` (no PDF) — it never breaks the caller (the
Zotero add path).  The cascade reuses :mod:`zoterocite._http` for its read-only
JSON GETs (shared User-Agent, scheme allow-list, bounded retry) and OUR
polite-pool contact email (:func:`zoterocite._http.contact_email`) rather than
any hard-coded address.

Provenance
----------
The SSRF guard (:func:`_url_resolves_to_public_host`) and the four-source OA
cascade (Unpaywall → arXiv-from-Crossref → Semantic Scholar → PMC) are ported
from zotero-mcp (``src/zotero_mcp/tools/_helpers.py``), MIT-licensed:

    The MIT License (MIT)
    Copyright (c) 2024 Yu Yang (54yyyu) and zotero-mcp contributors

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in
    all copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.

The port targets the stdlib (``urllib``/``socket``/``ipaddress``) instead of
``requests`` — zotero-word-cite standardises every read-only public-API client on
:mod:`zoterocite._http` and does not depend on ``requests``.  The guard's
algorithm and threat model are preserved verbatim; only the HTTP transport and
the polite-pool email differ.
"""
from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from ipaddress import ip_address
from typing import Optional

from . import _http
from . import entrez as _entrez

# Default per-request timeout for the cascade's metadata GETs and the PDF fetch.
_DEFAULT_TIMEOUT = 15.0

# A downloaded PDF smaller than this is almost certainly an error/landing page,
# not a real article — reject it (matches the source's 1 KB floor).
_MIN_PDF_BYTES = 1000
# Hard cap on a downloaded PDF: refuse to buffer an unbounded/oversize body from
# an attacker-influenceable URL (the source streamed to a temp file; we bound
# the read because we return bytes for the Zotero attach path).
_MAX_PDF_BYTES = 100 * 1024 * 1024  # 100 MB

# ---------------------------------------------------------------------------
# SSRF guard  (ported VERBATIM from zotero-mcp — see module copyright above)
# ---------------------------------------------------------------------------

_MAX_PDF_REDIRECTS = 5
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


def _url_resolves_to_public_host(url: str) -> bool:
    """Return ``True`` only if ``url`` is http(s) and its host resolves
    entirely to globally-routable IP addresses.

    SSRF guard for the open-access PDF download path: the candidate URL comes
    from third-party metadata APIs (Unpaywall / Semantic Scholar) and is
    therefore attacker-influenceable (a hostile paper record, or prompt
    injection steering the add-by-DOI path).  We reject non-http(s) schemes
    and any host that resolves to a private, loopback, link-local, reserved,
    or otherwise non-global address — including the 169.254.169.254
    cloud-metadata endpoint, which matters for HTTP/SSE-transport deployments.

    Note: a determined DNS-rebinding attacker could still flip the record
    between this check and the socket connect. Re-validating every redirect
    hop (see :func:`fetch_pdf_guarded`) and rejecting on the first non-global
    result narrows that window to a non-practical vector for this tool's
    threat model; full pinning would require a custom connection adapter.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or None)
    except (socket.gaierror, UnicodeError, ValueError):
        return False
    if not infos:
        return False
    for info in infos:
        sockaddr = info[4]
        try:
            ip = ip_address(sockaddr[0])
        except ValueError:
            return False
        if not ip.is_global or ip.is_reserved or ip.is_multicast:
            return False
    return True


def fetch_pdf_guarded(
    url: str, *, timeout: float = _DEFAULT_TIMEOUT
) -> Optional[bytes]:
    """GET ``url`` with SSRF protection and return the PDF bytes, or ``None``.

    Ported from zotero-mcp's ``_guarded_pdf_get`` + ``_download_and_attach_pdf``
    (SSRF guard + manual redirect re-validation), retargeted onto stdlib
    ``urllib`` (the source used ``requests``).  Because :func:`urllib.request`
    has no ``allow_redirects=False`` flag, we install a no-redirect opener and
    follow the ``Location`` chain manually — re-running
    :func:`_url_resolves_to_public_host` on EVERY hop so a redirect that lands
    on a private/loopback/link-local host is rejected.

    Validation, in order:

    1. Each URL in the redirect chain must resolve to public IPs only.
    2. At most :data:`_MAX_PDF_REDIRECTS` redirect hops.
    3. The final ``Content-Type`` must be a PDF (or ``octet-stream``).
    4. The body must be ``>= _MIN_PDF_BYTES`` and ``<= _MAX_PDF_BYTES``.

    Any failure — rejected host, too many redirects, wrong content-type, over-
    or under-size body, network/parse error — returns ``None``.  Never raises.
    """
    # No-redirect opener: HTTPRedirectHandler.redirect_request -> None makes
    # urllib surface the 3xx response instead of transparently following it, so
    # we can re-validate each hop's host before connecting to the next one.
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):  # noqa: D401, ANN001, ANN002
            return None

    opener = urllib.request.build_opener(_NoRedirect)

    current = url
    try:
        for _ in range(_MAX_PDF_REDIRECTS + 1):
            if not _url_resolves_to_public_host(current):
                return None
            req = urllib.request.Request(
                current,
                method="GET",
                headers={"User-Agent": _http.user_agent()},
            )
            try:
                resp = opener.open(req, timeout=timeout)  # noqa: S310
            except urllib.error.HTTPError as exc:
                status = getattr(exc, "code", None)
                if status in _REDIRECT_STATUSES:
                    location = (
                        exc.headers.get("Location") if exc.headers else None
                    )
                    try:
                        exc.close()
                    except Exception:  # noqa: BLE001
                        pass
                    if not location:
                        return None
                    current = urllib.parse.urljoin(current, location)
                    continue
                return None  # non-redirect HTTP error (404, 403, 5xx, …)
            except Exception:  # noqa: BLE001 — never propagate
                return None

            # A 2xx with the no-redirect opener: this is the terminal response.
            with resp:
                status = getattr(resp, "status", None) or resp.getcode()
                if status in _REDIRECT_STATUSES:
                    location = resp.headers.get("Location")
                    if not location:
                        return None
                    current = urllib.parse.urljoin(current, location)
                    continue
                content_type = (resp.headers.get("Content-Type") or "").lower()
                if "pdf" not in content_type and "octet-stream" not in content_type:
                    return None
                # Bounded read: stop at one byte over the cap so we can reject
                # an oversize body without buffering it all.
                body = resp.read(_MAX_PDF_BYTES + 1)
                if len(body) > _MAX_PDF_BYTES:
                    return None
                if len(body) < _MIN_PDF_BYTES:
                    return None
                return body
        return None  # too many redirects
    except Exception:  # noqa: BLE001 — offline-degrade contract
        return None


# ---------------------------------------------------------------------------
# OA cascade sources  (ported from zotero-mcp — our contact email, our HTTP)
# ---------------------------------------------------------------------------

def _get_json(url: str, *, timeout: float) -> Optional[dict]:
    """GET ``url`` via the shared HTTP primitive and parse JSON, or ``None``.

    Routes through :func:`zoterocite._http.http_get` so the cascade inherits the
    shared User-Agent (our polite-pool contact email), the http(s) scheme
    allow-list, and the bounded 429/5xx retry — and the never-raise contract.
    """
    body = _http.http_get(url, timeout=timeout, headers={"Accept": "application/json"})
    if body is None:
        return None
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def _try_unpaywall(doi: str, *, timeout: float) -> Optional[str]:
    """Try the Unpaywall API for an open-access PDF URL."""
    params = urllib.parse.urlencode({"email": _http.contact_email()})
    url = f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi, safe='/')}?{params}"
    oa_data = _get_json(url, timeout=timeout)
    if not oa_data:
        return None

    best = oa_data.get("best_oa_location") or {}
    pdf_url = best.get("url_for_pdf")
    if pdf_url:
        return pdf_url

    for loc in oa_data.get("oa_locations") or []:
        if isinstance(loc, dict) and loc.get("url_for_pdf"):
            return loc["url_for_pdf"]

    landing = best.get("url")
    if landing:
        return landing
    return None


def _crossref_message(doi: str, *, timeout: float) -> Optional[dict]:
    """Fetch the RAW Crossref ``works`` message for *doi* (relation / link /
    alternative-id intact), or ``None``.

    refresolve's :func:`_crossref_doi_fetch` returns a *parsed* item that drops
    ``relation`` / ``alternative-id`` / ``link`` — exactly the fields the
    arXiv-from-Crossref source needs — so we fetch the raw message here through
    the shared HTTP primitive (same UA / contact email) rather than widening
    refresolve's public surface.
    """
    safe_doi = urllib.parse.quote(doi, safe="/")
    data = _get_json(f"https://api.crossref.org/works/{safe_doi}", timeout=timeout)
    if not data:
        return None
    msg = data.get("message")
    return msg if isinstance(msg, dict) else None


def _try_arxiv_from_crossref(crossref_metadata: Optional[dict]) -> Optional[str]:
    """Check Crossref metadata for an arXiv id and return an arXiv PDF URL."""
    if not crossref_metadata:
        return None
    try:
        relations = crossref_metadata.get("relation", {}) or {}
        for rel_type in ("has-preprint", "is-preprint-of", "is-identical-to",
                         "is-version-of", "has-version"):
            for rel in relations.get(rel_type, []) or []:
                if not isinstance(rel, dict):
                    continue
                rel_id = rel.get("id", "") or ""
                if rel.get("id-type") == "arxiv" and rel_id:
                    return f"https://arxiv.org/pdf/{rel_id}.pdf"
                if rel.get("id-type") == "doi" and "arxiv" in rel_id.lower():
                    m = re.search(r"arXiv\.(\d{4}\.\d{4,5}(?:v\d+)?)", rel_id, re.IGNORECASE)
                    if m:
                        return f"https://arxiv.org/pdf/{m.group(1)}.pdf"

        for alt_id in crossref_metadata.get("alternative-id", []) or []:
            if re.match(r"\d{4}\.\d{4,5}", str(alt_id)):
                return f"https://arxiv.org/pdf/{alt_id}.pdf"

        for link in crossref_metadata.get("link", []) or []:
            if not isinstance(link, dict):
                continue
            url = link.get("URL", "") or ""
            if "arxiv.org" in url:
                m = re.search(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5}(?:v\d+)?)", url)
                if m:
                    return f"https://arxiv.org/pdf/{m.group(1)}.pdf"
        return None
    except Exception:  # noqa: BLE001
        return None


def _try_semantic_scholar(doi: str, *, timeout: float) -> Optional[str]:
    """Try the Semantic Scholar API for an open-access PDF URL."""
    safe_doi = urllib.parse.quote(doi, safe="/")
    params = urllib.parse.urlencode({"fields": "openAccessPdf"})
    url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{safe_doi}?{params}"
    data = _get_json(url, timeout=timeout)
    if not data:
        return None
    oa_pdf = data.get("openAccessPdf") or {}
    if isinstance(oa_pdf, dict) and oa_pdf.get("url"):
        return oa_pdf["url"]
    return None


def _try_pmc(doi: Optional[str], pmid: Optional[str], *, timeout: float) -> Optional[str]:
    """Try PubMed Central for a free PDF via DOI/PMID → PMCID conversion.

    Delegates to :func:`zoterocite.entrez.doi_or_pmid_to_pmcid` — the single
    canonical DOI/PMID→PMCID helper — which uses the shared endpoint constant
    and api_key wiring from :mod:`zoterocite.entrez`.  A DOI is tried first (it
    is the more precise key); a bare PMID is the fallback.
    """
    for ids in (doi, pmid):
        if not ids:
            continue
        pmcid = _entrez.doi_or_pmid_to_pmcid(ids, timeout=timeout)
        if pmcid:
            return f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf/"
    return None


def find_oa_pdf_url(
    doi: Optional[str] = None,
    pmid: Optional[str] = None,
    arxiv: Optional[str] = None,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Optional[str]:
    """Return the best open-access PDF URL for the given identifiers, or ``None``.

    The four-source cascade, ported from zotero-mcp's ``_try_attach_oa_pdf``:

    1. **arXiv (direct)** — if an ``arxiv`` id is supplied, use it immediately
       (it is a definitive OA source; no API round-trip needed).
    2. **Unpaywall** — ``best_oa_location`` → alternate ``oa_locations`` →
       landing page (needs a DOI).
    3. **arXiv via Crossref** — fetch the raw Crossref message for the DOI and
       mine ``relation`` / ``alternative-id`` / ``link`` for an arXiv id.
    4. **Semantic Scholar** — ``openAccessPdf.url`` (needs a DOI).
    5. **PMC** — DOI/PMID → PMCID → the PMC article PDF endpoint.

    The first source to yield a URL wins.  Identifiers are passed in already
    resolved (the caller owns DOI→PMID/arXiv resolution via
    :mod:`zoterocite.refresolve` / :mod:`zoterocite.entrez`).  Never raises —
    any failure along the way degrades to ``None``.
    """
    if arxiv:
        return f"https://arxiv.org/pdf/{arxiv}.pdf"

    if doi:
        url = _try_unpaywall(doi, timeout=timeout)
        if url:
            return url

        crossref_msg = _crossref_message(doi, timeout=timeout)
        url = _try_arxiv_from_crossref(crossref_msg)
        if url:
            return url

        url = _try_semantic_scholar(doi, timeout=timeout)
        if url:
            return url

    url = _try_pmc(doi, pmid, timeout=timeout)
    if url:
        return url

    return None
