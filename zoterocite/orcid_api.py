"""ORCID Public API client (network-resilient, stdlib HTTP, unauthenticated).

Fetches the public works list for an ORCID iD from the ORCID public registry
(https://pub.orcid.org/v3.0/) using only the unauthenticated read-public API.
No credentials or OAuth flow are required.

Design contract: **these functions never raise to the caller.**  Every HTTP or
parse error is swallowed and an empty list is returned, because the biosketch
tailor ranker must degrade gracefully to biosketch-only ranking when the network
is unavailable or when the ORCID record is private/missing.

This module is intentionally self-contained and adds no third-party dependencies
(stdlib urllib only, mirroring :mod:`zoterocite.entrez`).
"""
from __future__ import annotations

import json
import re
from typing import Optional

from . import _http

_BASE_URL = "https://pub.orcid.org/v3.0"
# Tool + contact email are owned by :mod:`zoterocite._http`; we call
# ``_http.user_agent()`` for the polite-pool UA rather than carrying our own.

# Matches a valid ORCID iD: four groups of four digits, last char may be X.
_ORCID_RE = re.compile(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]")

_TIMEOUT = 10


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _normalize_orcid_id(orcid: Optional[str]) -> Optional[str]:
    """Extract and return a bare ORCID iD from any input form, or ``None``.

    Accepts a bare ``"0000-0002-1825-0097"``, a full URL
    ``"https://orcid.org/0000-0002-1825-0097"`` (http or https, with or
    without a trailing slash), or any string containing a valid iD.
    Returns the bare iD (``"XXXX-XXXX-XXXX-XXXX"``) or ``None`` if no
    valid iD is found or the input is empty/None.
    """
    if not orcid:
        return None
    m = _ORCID_RE.search(str(orcid).strip())
    return m.group(0) if m else None


def _http_get(url: str, timeout: float = _TIMEOUT) -> Optional[bytes]:
    """GET ``url`` and return the raw body, or ``None`` on ANY failure.

    Thin wrapper over :func:`zoterocite._http.http_get` (the one shared GET
    primitive).  Kept as a named function because :func:`fetch_orcid_works` and
    the module's tests reference ``orcid_api._http_get`` directly.  Sets
    ``Accept: application/json`` so ORCID returns JSON rather than XML; the
    never-raise / retry / Retry-After behaviour lives in ``_http``.
    """
    return _http.http_get(
        url,
        timeout=timeout,
        headers={
            "Accept": "application/json",
            "User-Agent": _http.user_agent(),
        },
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_orcid_works(orcid_id: Optional[str], *, timeout: float = _TIMEOUT) -> list[dict]:
    """Return all works from a researcher's public ORCID record.

    ``orcid_id`` may be a bare iD (``"0000-0002-1825-0097"``) or a full
    ORCID URL.  Returns ``[]`` on any failure (invalid iD, network error,
    private record, JSON parse failure).

    Each returned dict has keys:
    ``title``, ``doi``, ``pmid``, ``pmcid``, ``year``, ``citation``,
    ``source`` (always ``"orcid"``).

    The ORCID response shape:
    ``data["group"]`` is a list; each group's preferred work is
    ``group["work-summary"][0]``.  External IDs live in
    ``work_summary["external-ids"]["external-id"]`` as a list.

    Back-compat wrapper over :func:`fetch_orcid_works_status` (F5): if you need
    to tell a genuine empty record from a fetch FAILURE — both currently return
    ``[]`` — call that variant for a ``degraded`` flag.
    """
    works, _status = fetch_orcid_works_status(orcid_id, timeout=timeout)
    return works


def fetch_orcid_works_status(
    orcid_id: Optional[str], *, timeout: float = _TIMEOUT
) -> tuple[list[dict], dict]:
    """Like :func:`fetch_orcid_works`, but also report whether the fetch was
    DEGRADED (F5).

    Returns ``(works, status)`` where ``status`` is
    ``{"degraded": bool, "reason": str | None}``.  ``degraded`` is ``True`` only
    when the works list is empty *because the fetch/parse failed* (network down,
    private record, bad JSON) — NOT when the record genuinely has zero works
    (a successful fetch of an empty ``group``).  This lets a ranker say "ORCID
    unavailable; ranked on biosketch signal only" instead of silently treating a
    failed fetch as "this author has no works".  Never raises.
    """
    bare_id = _normalize_orcid_id(orcid_id)
    if not bare_id:
        # A malformed/absent iD is a caller error, not a degraded fetch.
        return [], {"degraded": False, "reason": "no valid ORCID iD"}

    url = f"{_BASE_URL}/{bare_id}/works"
    raw = _http_get(url, timeout=timeout)
    if raw is None:
        return [], {"degraded": True, "reason": "ORCID fetch failed (network/HTTP)"}

    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return [], {"degraded": True, "reason": "ORCID response was not valid JSON"}

    results: list[dict] = []
    try:
        groups = data.get("group") or []
        for group in groups:
            try:
                summaries = group.get("work-summary") or []
                if not summaries:
                    continue
                summary = summaries[0]  # preferred version

                # --- title ---
                title = ""
                try:
                    title = summary["title"]["title"]["value"] or ""
                except (KeyError, TypeError):
                    pass

                # --- year ---
                year = ""
                try:
                    year = summary["publication-date"]["year"]["value"] or ""
                except (KeyError, TypeError):
                    pass

                # --- external ids ---
                doi = ""
                pmid = ""
                pmcid = ""
                try:
                    ext_ids = (summary.get("external-ids") or {}).get("external-id") or []
                    for eid in ext_ids:
                        id_type = (eid.get("external-id-type") or "").lower()
                        id_value = (eid.get("external-id-value") or "").strip()
                        if not id_value:
                            continue
                        if id_type == "doi" and not doi:
                            doi = id_value
                        elif id_type == "pmid" and not pmid:
                            pmid = id_value
                        elif id_type == "pmc" and not pmcid:
                            pmcid = id_value
                except (KeyError, TypeError):
                    pass

                # Skip completely empty works (no title AND no doi AND no pmid AND no pmcid).
                if not title and not doi and not pmid and not pmcid:
                    continue

                # Synthesize a citation string from what we have.
                if title and year:
                    citation = f"{title} ({year})"
                elif title:
                    citation = title
                else:
                    citation = ""

                results.append({
                    "title": title,
                    "doi": doi,
                    "pmid": pmid,
                    "pmcid": pmcid,
                    "year": year,
                    "citation": citation,
                    "source": "orcid",
                })
            except Exception:  # noqa: BLE001 — skip malformed work, continue
                continue
    except Exception as exc:  # noqa: BLE001 — top-level parse failure
        # The response parsed as JSON but had an unexpected top-level shape:
        # treat as degraded, not as a genuinely empty record.
        return [], {"degraded": True, "reason": f"ORCID response shape error: {exc}"}

    # A successful fetch — even one with zero works — is NOT degraded.
    return results, {"degraded": False, "reason": None}
