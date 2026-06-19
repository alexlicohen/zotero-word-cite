"""NIH iCite API client (network-resilient, stdlib HTTP, unauthenticated).

Fetches field-normalized bibliometrics for a set of PMIDs from the public NIH
iCite API (https://icite.od.nih.gov/api/pubs). iCite exposes the Relative
Citation Ratio (RCR) — the field- and time-normalized impact metric NIH study
sections actually look at (RCR 1.0 = the field-average paper of that vintage) —
alongside the raw citation count, an NIH percentile, and the structured author
list. :mod:`zoterocite.biotailor` uses these to weight an author's own work by
its reviewer-relevant impact when tailoring a biosketch's "Products Closely
Related" set.

Design contract: **this function never raises to the caller.** Every HTTP or
parse error is swallowed and a partial-or-empty dict is returned, because the
biosketch ranker must degrade gracefully to lexical + author-role ranking when
iCite is unreachable or a PMID is unknown.

No new dependencies: stdlib ``urllib`` only, mirroring
:mod:`zoterocite.orcid_api`, :mod:`zoterocite.mybib`, and
:mod:`zoterocite.entrez`.
"""
from __future__ import annotations

import json
import urllib.parse
from typing import Optional

from . import _http

_BASE_URL = "https://icite.od.nih.gov/api/pubs"
# Tool + contact email are owned by :mod:`zoterocite._http`; we call
# ``_http.user_agent()`` for the polite-pool UA rather than carrying our own.

# iCite accepts a large id list per call; chunk conservatively to keep the URL
# length sane and stay polite. ~200 pmids/call is comfortable.
_BATCH = 200
_TIMEOUT = 15.0


def _http_get(url: str, timeout: float = _TIMEOUT) -> Optional[bytes]:
    """GET ``url`` and return the raw body, or ``None`` on ANY failure.

    Thin wrapper over :func:`zoterocite._http.http_get` (the one shared GET
    primitive).  Kept as a named function because :func:`fetch_icite` and the
    module's tests reference ``icite._http_get`` directly.  Sets
    ``Accept: application/json``; the never-raise / retry / Retry-After
    behaviour lives in ``_http``.
    """
    return _http.http_get(
        url,
        timeout=timeout,
        headers={
            "Accept": "application/json",
            "User-Agent": _http.user_agent(),
        },
    )


def _coerce_pmids(pmids) -> list[str]:
    """De-dup + clean a pmid iterable to bare digit strings, order-preserved."""
    clean: list[str] = []
    seen: set = set()
    for p in pmids or []:
        s = str(p).strip()
        if not s or not s.isdigit() or s in seen:
            continue
        seen.add(s)
        clean.append(s)
    return clean


def _parse_record(rec: dict) -> Optional[tuple[str, dict]]:
    """Parse one iCite ``data[]`` record into ``(pmid, {...})`` or ``None``.

    Returns ``None`` when no PMID is present (we key results by PMID). All
    metric fields degrade to ``None`` (not 0) when absent/null, so a missing
    RCR is distinguishable from a genuine RCR of 0 downstream.
    """
    if not isinstance(rec, dict):
        return None
    pmid = rec.get("pmid")
    if pmid is None:
        return None
    pmid = str(pmid).strip()
    if not pmid:
        return None

    def _num(key):
        v = rec.get(key)
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    rcr = _num("relative_citation_ratio")
    nih_percentile = _num("nih_percentile")

    citation_count = rec.get("citation_count")
    if citation_count is not None:
        try:
            citation_count = int(citation_count)
        except (TypeError, ValueError):
            citation_count = None

    year = rec.get("year")
    if year is not None:
        try:
            year = int(year)
        except (TypeError, ValueError):
            year = None

    # Structured authors: a list of {lastName, firstName, fullName}. iCite may
    # return this as a list of dicts already, or (older shape) a single string;
    # tolerate both and always emit a list of dicts.
    authors: list[dict] = []
    raw_authors = rec.get("authors")
    if isinstance(raw_authors, list):
        for a in raw_authors:
            if isinstance(a, dict):
                authors.append({
                    "lastName": (a.get("lastName") or "").strip(),
                    "firstName": (a.get("firstName") or "").strip(),
                    "fullName": (a.get("fullName") or "").strip(),
                })
            elif isinstance(a, str) and a.strip():
                authors.append({"lastName": "", "firstName": "", "fullName": a.strip()})
    elif isinstance(raw_authors, str) and raw_authors.strip():
        # Comma-separated "First Last, First Last" string fallback.
        for chunk in raw_authors.split(","):
            full = chunk.strip()
            if full:
                authors.append({"lastName": "", "firstName": "", "fullName": full})

    return pmid, {
        "rcr": rcr,
        "citation_count": citation_count,
        "nih_percentile": nih_percentile,
        "year": year,
        "authors": authors,
    }


def fetch_icite(pmids, *, timeout: float = _TIMEOUT) -> dict[str, dict]:
    """Fetch iCite bibliometrics for ``pmids``.

    Returns ``{pmid: {rcr, citation_count, nih_percentile, year, authors}}``
    where ``authors`` is a list of ``{lastName, firstName, fullName}`` dicts.
    PMIDs absent from iCite (unknown, too new, preprints) are simply absent
    from the result.

    Batches ids (~200/call) and is fully network-resilient: any HTTP/JSON error
    yields a partial-or-empty dict, NEVER a raise.

    Back-compat wrapper over :func:`fetch_icite_status` (F5): a partial result
    (some batches failed) is indistinguishable from "those PMIDs are unknown to
    iCite" in the bare dict.  Call :func:`fetch_icite_status` to learn how many
    batches failed, so a ranker can say "iCite unavailable; ranked on lexical
    signal only" instead of silently degrading impact-weighting.
    """
    out, _status = fetch_icite_status(pmids, timeout=timeout)
    return out


def fetch_icite_status(pmids, *, timeout: float = _TIMEOUT) -> tuple[dict[str, dict], dict]:
    """Like :func:`fetch_icite`, but also report batch-level failures (F5).

    Returns ``(out, status)`` where ``status`` is::

        {"degraded": bool, "n_failed_batches": int, "n_batches": int}

    ``degraded`` is ``True`` if ANY batch's fetch/parse failed — meaning the
    returned map may be MISSING metrics for PMIDs that iCite actually knows, as
    opposed to PMIDs iCite genuinely lacks.  A caller weighting an author's work
    by RCR can then disclose "impact metrics partial/unavailable" rather than
    treating a degraded fetch as low impact.  Never raises.
    """
    clean = _coerce_pmids(pmids)
    if not clean:
        return {}, {"degraded": False, "n_failed_batches": 0, "n_batches": 0}

    out: dict[str, dict] = {}
    n_batches = 0
    n_failed = 0
    for i in range(0, len(clean), _BATCH):
        n_batches += 1
        batch = clean[i:i + _BATCH]
        q = urllib.parse.urlencode({"pmids": ",".join(batch)})
        url = f"{_BASE_URL}?{q}"
        body = _http_get(url, timeout=timeout)
        if body is None:
            n_failed += 1
            continue  # partial result; keep going with remaining batches
        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            n_failed += 1
            continue
        if not isinstance(data, dict):
            n_failed += 1
            continue  # bare array, null, string, or other non-dict JSON
        for rec in (data.get("data") or []):
            parsed = _parse_record(rec)
            if parsed is not None:
                out[parsed[0]] = parsed[1]
    return out, {
        "degraded": n_failed > 0,
        "n_failed_batches": n_failed,
        "n_batches": n_batches,
    }
