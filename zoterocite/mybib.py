"""NCBI My Bibliography public-page scraper (network-resilient, stdlib HTTP).

Fetches a researcher's public My Bibliography page at::

    https://www.ncbi.nlm.nih.gov/myncbi/<id>/bibliography/public/

The page is server-rendered HTML — no JS/XHR needed.  PMIDs are embedded as
``pmid="NNNNNNNN"`` attributes; DOIs appear as bare ``10.xxx/...`` substrings.
Pagination is via ``?page=N``; a page beyond the end returns HTTP 200 with zero
``pmid="..."`` matches.

Design contract: **this function never raises to the caller.**  Any HTTP, parse,
or enrichment error is swallowed and an empty list returned — matching
:func:`zoterocite.orcid_api.fetch_orcid_works`.

No new dependencies: stdlib ``urllib`` only, mirroring
:mod:`zoterocite.orcid_api` and :mod:`zoterocite.entrez`.
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Optional

from . import _http
from . import entrez
from .textpatterns import extract_dois

# Tool + contact email are owned by :mod:`zoterocite._http`; we call
# ``_http.user_agent()`` for the polite-pool UA rather than carrying our own.
_MYBIB_HOST = "www.ncbi.nlm.nih.gov"

# Matches pmid="NNNNNNNN" — 6-9 digits as specified.
_PMID_ATTR_RE = re.compile(r'pmid="(\d{6,9})"')

def _is_mybib_url(url: str) -> bool:
    """Return True only if *url* is a My Bibliography public page on the NCBI host.

    Uses ``urlsplit`` to check the netloc precisely, preventing path-based SSRF
    (e.g. ``https://evil.com/www.ncbi.nlm.nih.gov/myncbi/...`` is rejected).
    """
    if not url:
        return False
    try:
        parts = urllib.parse.urlsplit(url)
    except Exception:  # noqa: BLE001
        return False
    netloc = parts.netloc.lower()
    # Accept www.ncbi.nlm.nih.gov and any *.nlm.nih.gov subdomain.
    if netloc != "www.ncbi.nlm.nih.gov" and not netloc.endswith(".nlm.nih.gov"):
        return False
    return "/myncbi/" in parts.path.lower()


def _normalize_mybib_url(url: str) -> Optional[str]:
    """Strip any existing query string from *url* and ensure it ends with '/'.

    Returns ``None`` if the URL does not look like a My Bibliography URL (uses
    the strict netloc check from :func:`_is_mybib_url`).
    """
    if not _is_mybib_url(url):
        return None
    # Split off query string.
    base = url.split("?")[0].rstrip("/") + "/"
    return base


def _http_get(url: str, timeout: float = 10.0) -> Optional[bytes]:
    """GET *url* and return the raw body, or ``None`` on ANY failure.

    Thin wrapper over :func:`zoterocite._http.http_get` (the one shared GET
    primitive).  Kept as a named function because :func:`fetch_mybib_works` and
    the module's tests reference ``mybib._http_get`` directly.  Sets a
    descriptive User-Agent (recommended by NCBI) and an HTML ``Accept``; the
    never-raise / retry / Retry-After behaviour lives in ``_http``.
    """
    return _http.http_get(
        url,
        timeout=timeout,
        headers={
            "User-Agent": _http.user_agent(),
            "Accept": "text/html,application/xhtml+xml",
        },
    )


def fetch_mybib_works(
    public_url: str,
    *,
    max_pages: int = 25,
    timeout: float = 10.0,
) -> list[dict]:
    """Fetch all works from a researcher's public NCBI My Bibliography page.

    ``public_url`` must be the public My Bibliography URL in the form::

        https://www.ncbi.nlm.nih.gov/myncbi/<id>/bibliography/public/

    Any query string is stripped; pagination is handled automatically.  Returns
    ``[]`` on any failure (wrong URL form, network error, parse failure) so the
    caller always gets a list and never sees a raise.

    Each returned dict has keys:
    ``pmid``, ``doi``, ``pmcid``, ``title``, ``year``, ``citation``,
    ``source`` (always ``"mybib"``).

    PMIDs are enriched via :func:`zoterocite.entrez.efetch_pubmed` to populate
    ``title``, ``year``, and a formatted ``citation`` string.  DOI-only entries
    (datasets, preprints with no PMID) get a minimal dict with just ``doi``
    and ``source``.

    Pagination stops when a page returns zero new PMIDs AND zero new DOIs, or
    when ``max_pages`` is reached.

    Back-compat: returns just the works list.  If you need to know whether the
    list is COMPLETE (a mid-pagination fetch failure silently truncates a
    bibliography — F5), call :func:`fetch_mybib_works_status`, which returns the
    same list plus an ``incomplete`` flag.
    """
    works, _status = fetch_mybib_works_status(
        public_url, max_pages=max_pages, timeout=timeout
    )
    return works


def fetch_mybib_works_status(
    public_url: str,
    *,
    max_pages: int = 25,
    timeout: float = 10.0,
) -> tuple[list[dict], dict]:
    """Like :func:`fetch_mybib_works`, but also report completeness (F5).

    Returns ``(works, status)`` where ``status`` is::

        {"incomplete": bool, "reason": str | None, "pages_fetched": int}

    ``incomplete`` is ``True`` when pagination stopped because a page FETCH
    FAILED (network/decode) — i.e. the bibliography may be TRUNCATED, distinct
    from the clean end-of-results case (a page that returned 0 new items). It is
    also ``True`` if PMID enrichment failed (titles/years missing). A caller can
    surface "My Bibliography fetch was incomplete — results may be partial"
    instead of treating a truncated list as the full bibliography.

    Never raises; a malformed URL yields ``([], {"incomplete": False, ...})``
    (nothing to fetch is not an incomplete fetch).
    """
    status = {"incomplete": False, "reason": None, "pages_fetched": 0}
    base = _normalize_mybib_url(public_url)
    if base is None:
        return [], status

    seen_pmids: set[str] = set()
    seen_dois: set[str] = set()
    all_pmids: list[str] = []      # ordered, deduped
    doi_only: list[str] = []       # DOIs that never appeared with a PMID on the page
    paired_dois: set[str] = set()  # DOIs that sit in a PMID's citation block (lowercased)

    try:
        for page_num in range(1, max_pages + 1):
            url = f"{base}?page={page_num}"
            body = _http_get(url, timeout=timeout)
            if body is None:
                # A fetch failure mid-pagination TRUNCATES the bibliography. Mark
                # the result incomplete rather than passing a partial list off as
                # the whole thing (F5).
                if page_num <= max_pages:
                    status["incomplete"] = True
                    status["reason"] = f"page {page_num} fetch failed"
                break

            status["pages_fetched"] = page_num

            try:
                html = body.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                status["incomplete"] = True
                status["reason"] = f"page {page_num} decode failed"
                break

            # Extract PMIDs from pmid="NNNNNNNN" attributes.
            page_pmids = _PMID_ATTR_RE.findall(html)

            # Extract DOIs (bare or with prefix) via the project extractor.
            page_dois = extract_dois(html)

            # Pair each PMID with the first DOI inside its citation block (the
            # span up to the next pmid= attribute): that DOI is the paper's own
            # and must NOT later be emitted as a standalone "doi-only" work even
            # when PubMed efetch fails to echo the DOI back for that PMID — the
            # common phantom-duplicate. Suppression only; efetch stays
            # authoritative for a work's doi field.
            pmid_starts = [m.start() for m in _PMID_ATTR_RE.finditer(html)]
            if pmid_starts and page_dois:
                low = html.lower()
                doi_at: list[tuple[int, str]] = []
                for d in page_dois:
                    pos = low.find(d.lower())
                    if pos >= 0:
                        doi_at.append((pos, d))
                doi_at.sort()
                for i, ps in enumerate(pmid_starts):
                    nxt = pmid_starts[i + 1] if i + 1 < len(pmid_starts) else len(html)
                    for dpos, d in doi_at:
                        if ps < dpos < nxt:
                            paired_dois.add(d.lower())
                            break

            # Check for new content.
            new_pmids = [p for p in page_pmids if p not in seen_pmids]
            new_dois = [d for d in page_dois if d.lower() not in seen_dois]

            if not new_pmids and not new_dois:
                break  # empty page or duplicate page → stop paginating

            for p in new_pmids:
                seen_pmids.add(p)
                all_pmids.append(p)

            for d in new_dois:
                # Trim query junk that can be captured alongside a bare DOI
                # (e.g. "10.1/foo&amp;bar" or "10.1/foo?utm=...").
                d = d.split("&")[0].split("?")[0]
                seen_dois.add(d.lower())
                doi_only.append(d)

    except Exception as exc:  # noqa: BLE001 — outer safety net
        # An unexpected error mid-scrape means we never finished: the list so far
        # is partial. Surface it as incomplete instead of returning a bare [].
        return [], {"incomplete": True, "reason": f"scrape error: {exc}",
                    "pages_fetched": status["pages_fetched"]}

    # Enrich PMIDs via Entrez to get titles + years.
    meta: dict[str, dict] = {}
    if all_pmids:
        try:
            meta = entrez.efetch_pubmed(all_pmids)
        except Exception:  # noqa: BLE001
            meta = {}
        # efetch_pubmed never raises and may return a PARTIAL map; missing PMIDs
        # yield works with empty title/year. Flag that as incomplete enrichment
        # so the caller knows the metadata (not necessarily the work set) is partial.
        if any(p not in meta for p in all_pmids):
            status["incomplete"] = True
            if status["reason"] is None:
                status["reason"] = "PMID enrichment incomplete"

    results: list[dict] = []

    for pmid in all_pmids:
        rec = meta.get(pmid) or {}
        title = rec.get("title") or ""
        year = rec.get("year") or ""
        # rec.get("doi") may return None explicitly — coerce to str so .lower() is safe.
        doi_from_meta = rec.get("doi") or ""
        if not isinstance(doi_from_meta, str):
            doi_from_meta = ""

        # Mark the DOI from meta as seen so we don't emit a duplicate doi-only entry.
        if doi_from_meta:
            seen_dois.add(doi_from_meta.lower())

        # Build citation string.
        if title and year:
            citation = f"{title} ({year})"
        elif title:
            citation = title
        else:
            citation = pmid  # last-resort fallback

        results.append({
            "pmid": pmid,
            "doi": doi_from_meta,
            "pmcid": "",
            "title": title,
            "year": year,
            "citation": citation,
            "source": "mybib",
        })

    # DOI-only entries: those that were NOT also seen paired with a PMID.
    # Coerce each meta doi to str (tolerates doi=None) before .lower().
    pmid_doi_set = {((meta.get(p) or {}).get("doi") or "").lower() for p in all_pmids}
    for doi in doi_only:
        if doi.lower() in pmid_doi_set or doi.lower() in paired_dois:
            continue  # already a PMID's DOI (efetch echo OR page citation-block pairing)
        results.append({
            "doi": doi,
            "pmid": "",
            "pmcid": "",
            "title": "",
            "year": "",
            "citation": doi,
            "source": "mybib",
        })

    return results, status
