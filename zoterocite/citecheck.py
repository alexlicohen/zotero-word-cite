"""Citation integrity checks for grant documents with Zotero fields.

Public API
----------
reconcile_citations(path) -> dict
    Offline reconciliation of in-text cited keys vs. bibliography keys.

load_retraction_db(csv_path) -> dict
    Load the Retraction Watch CSV (Crossref-distributed) into a normalised
    DOI → record mapping.

check_retractions(dois, db) -> list[Finding]
    Flag cited DOIs found in the retraction database.

cite_check(path, *, rw_csv=None, check_existence=False) -> list[Finding]
    Orchestrates all checks; returns a flat list[Finding].

default_rw_path() -> Path
    ~/.claude/skills/zotero-word-cite/data/retraction_watch.csv

refresh_retraction_db(dest=None, *, url) -> Path
    Download the Retraction Watch CSV from ``url`` into ``dest``.
    No default URL is hard-coded because the Crossref Labs distribution URL
    is not stable enough to ship silently — callers must supply it.
"""
from __future__ import annotations

import csv
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Set, Union

from . import _http
from .findings import Finding
from .zoterofield import scan_citations, _field_codes
from .docxio import Docx, DOCUMENT

_RW_SOURCE = "Retraction Watch (Crossref-distributed)"
# Crossref Labs distributes the full Retraction Watch dataset as CSV; the
# polite-pool endpoint takes a mailto. (Verified 2026-06; see
# https://www.crossref.org/labs/retraction-watch/)
# The contact email is owned by :mod:`zoterocite._http` (single source); this
# default URL is built at import time, so with no env var set it is byte-
# identical to the historical value.  ``refresh_retraction_db`` re-resolves the
# email at CALL time (below) so a per-process override is still honoured.
RETRACTION_WATCH_URL = (
    f"https://api.labs.crossref.org/data/retractionwatch?{_http.contact_email()}"
)

# Severity ordering for the same DOI appearing multiple times with different
# natures: higher index = more severe; keep the most severe per DOI.
# Keys are LOWERCASE so lookups use .lower() — the real Crossref/RW feed emits
# "Expression of concern" (lowercase 'of concern'), not title-case.
_NATURE_SEVERITY = {
    "reinstatement": 0,
    "correction": 1,
    "expression of concern": 2,
    "retraction": 3,
}


# ---------------------------------------------------------------------------
# DOI normalisation
# ---------------------------------------------------------------------------

def _normalise_doi(doi: str) -> str:
    """Lowercase, strip whitespace, leading https://doi.org/ (or dx.doi.org/), and
    trailing prose punctuation.  A DOI never legitimately ends in . , ; : ) or ],
    so stripping them is always safe and avoids false mismatches when a DOI appears
    at the end of a sentence."""
    doi = (doi or "").strip()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
    doi = doi.strip().lower()
    doi = re.sub(r"[.,;:)\]]+$", "", doi)
    return doi


# ---------------------------------------------------------------------------
# 1. reconcile_citations
# ---------------------------------------------------------------------------

def _bibl_keys_from_doc(path) -> Optional[Set[str]]:
    """Extract item keys from the ZOTERO_BIBL field, or None if absent."""
    root = Docx(path).read_tree(DOCUMENT)
    from .zoterofield import _field_codes, _key_from_uri
    import re as _re

    for code in _field_codes(root):
        c = code.strip()
        if "ZOTERO_BIBL" not in c:
            continue
        # The ZOTERO_BIBL field code looks like:
        #   ADDIN ZOTERO_BIBL {"uncited":[],"omitted":[],"custom":[]} CSL_BIBLIOGRAPHY
        # The embedded JSON does NOT contain the item keys; those are only
        # visible after Zotero renders the bibliography. However, Zotero stores
        # the rendered bibliography runs as field *result* XML — which we can't
        # easily mine for keys after-the-fact.
        #
        # The reliable offline approach: the bibliography keys are exactly the
        # union of all item keys found in citation fields — that's how Zotero
        # builds the bib. But we need to signal that a BIBL field *exists* so
        # that the caller can distinguish "bib absent" from "bib present but
        # no keys parseable".
        #
        # We return an empty set (bib present) so the caller can detect absence
        # vs. presence, and fill the keys from the cited set if needed.
        return set()

    return None


def reconcile_citations(path) -> dict:
    """Reconcile in-text cited keys against the bibliography.

    Returns::

        {
            "cited_keys": list[str],       # keys appearing in citation fields
            "bib_keys": list[str] | None,  # keys listed in the bibliography
                                           #   None when no ZOTERO_BIBL field found
            "orphan_citations": list[str], # cited but absent from bibliography
            "uncited_references": list[str],# in bibliography but never cited
            "no_bibliography": bool,       # True when ZOTERO_BIBL field is absent
            "bib_keys_verified": bool,     # True only if true bibliography keys
                                           #   were read from the doc; always
                                           #   False offline (see Caveat)
        }

    Note
    ----
    The ZOTERO_BIBL field code itself does not embed item keys — those are only
    known to Zotero at render time. Grant-forge detects the *presence* of the
    bibliography field and treats the bibliography key-set as equal to the cited
    key-set (which is how Zotero populates the bibliography by default), unless
    the document has been manually customised (uncited/omitted/custom arrays in
    the field code). If those arrays are non-empty they are honoured.

    Caveat (why ``bib_keys_verified`` is always ``False`` here)
    ----------------------------------------------------------
    Because ``bib_keys`` is *derived* from the cited keys (``(cited ∪ custom) −
    omitted``) rather than read from the rendered bibliography, this reconcile is
    structurally limited offline: ``orphan_citations`` can only surface a cited
    key that the field's ``omitted`` array explicitly suppresses, and
    ``uncited_references`` can only surface a ``custom`` extra entry. On a normal
    Zotero document (empty omitted/custom) **both lists are necessarily empty** —
    a clean result here therefore does NOT mean the bibliography membership was
    verified, only that no manual omit/custom edit created a mismatch. True
    verification requires reading the rendered bibliography keys, which are not
    available in the field code offline. ``bib_keys_verified`` is exposed so
    callers never present a clean reconcile as an authoritative "all citations
    are in the bibliography" result.
    """
    citations = scan_citations(path)
    cited_keys: List[str] = []
    seen: Set[str] = set()
    for cit in citations:
        for item in cit.get("items", []):
            k = item.get("key")
            if k and k not in seen:
                cited_keys.append(k)
                seen.add(k)

    # Parse ZOTERO_BIBL field for uncited/omitted/custom arrays
    root = Docx(path).read_tree(DOCUMENT)
    bib_field_present = False
    bib_keys: Optional[Set[str]] = None
    extra_bib_keys: Set[str] = set()
    omitted_keys: Set[str] = set()

    for code in _field_codes(root):
        c = code.strip()
        if "ZOTERO_BIBL" not in c:
            continue
        bib_field_present = True
        # Try to parse any JSON payload
        idx = c.find("{")
        if idx != -1:
            try:
                payload = json.loads(c[idx: c.rindex("}") + 1])
                # "custom" entries add extra items to the bibliography
                for entry in payload.get("custom", []):
                    k = (entry.get("key") or "").strip()
                    if k:
                        extra_bib_keys.add(k)
                # "omitted" entries suppress items from the bibliography
                # entries may be dicts {"key": "..."} or bare strings
                for entry in payload.get("omitted", []):
                    if isinstance(entry, str):
                        k = entry.strip()
                    elif isinstance(entry, dict):
                        k = (entry.get("key") or "").strip()
                    else:
                        k = ""
                    if k:
                        omitted_keys.add(k)
            except (json.JSONDecodeError, ValueError):
                pass
        break  # only one ZOTERO_BIBL per doc

    no_bibliography = not bib_field_present

    if bib_field_present:
        # Bibliography = (cited_keys ∪ custom) − omitted
        bib_keys = (seen | extra_bib_keys) - omitted_keys
    else:
        bib_keys = None

    if bib_keys is not None:
        orphan_citations = [k for k in cited_keys if k not in bib_keys]
        uncited_references = [k for k in sorted(bib_keys) if k not in seen]
    else:
        orphan_citations = []
        uncited_references = []

    return {
        "cited_keys": cited_keys,
        "bib_keys": sorted(bib_keys) if bib_keys is not None else None,
        "orphan_citations": orphan_citations,
        "uncited_references": uncited_references,
        "no_bibliography": no_bibliography,
        # bib_keys is derived from cited keys, never read from the rendered
        # bibliography, so membership is not actually verifiable offline.
        "bib_keys_verified": False,
    }


# ---------------------------------------------------------------------------
# 2. Retraction Watch
# ---------------------------------------------------------------------------

def default_rw_path() -> Path:
    """Return the default local path for the Retraction Watch CSV.

    Package-relative: ``<skill root>/data/retraction_watch.csv`` (the skill root
    is the parent of this package directory), so the cache travels with the repo
    regardless of where it is installed.
    """
    return Path(__file__).resolve().parent.parent / "data" / "retraction_watch.csv"


# The Retraction Watch dataset is updated every working day; refreshing weekly
# balances freshness against a 60+ MB download. Auto-refresh kicks in when the
# cache is missing or older than this.
RW_MAX_AGE_DAYS = 7


def ensure_retraction_db(*, max_age_days: int = RW_MAX_AGE_DAYS,
                         allow_network: bool = True,
                         dest: Optional[Union[str, Path]] = None):
    """Return ``(path_or_None, note_or_None)`` — the usable Retraction Watch CSV,
    auto-refreshing it when missing or stale.

    Policy: if the cache is present and younger than ``max_age_days``, use it as
    is. Otherwise, if ``allow_network``, try to (re)download; on failure fall
    back to any existing (stale) cache. Never raises — a failed refresh degrades
    to the cached copy, or to ``(None, note)`` if there is none. The returned
    ``note`` (a :class:`Finding`, INFO) records what happened, for surfacing.
    """
    dest_path = Path(dest) if dest else default_rw_path()
    if dest_path.exists():
        age_days = (time.time() - dest_path.stat().st_mtime) / 86400.0
        if age_days <= max_age_days:
            return dest_path, None
    else:
        age_days = None

    if not allow_network:
        # No network permitted: use a stale cache if we have one, else nothing.
        # In either case surface a WARN so the caller (gate) knows the screen
        # may be incomplete; the standalone cite-check --refresh path bypasses
        # this branch (allow_network=True) and is unaffected.
        stale_warn = Finding(
            check="CITE-RW-STALE-GATE",
            severity="WARN",
            message=(
                "Retraction screen used a stale/absent cache"
                + ("" if age_days is None else f" ({age_days:.0f} days old)")
                + "; run `cite-check --refresh` to update."
            ),
            source=_RW_SOURCE,
        )
        return (dest_path, stale_warn) if dest_path.exists() else (None, stale_warn)

    try:
        refresh_retraction_db(dest_path)
        return dest_path, Finding(
            check="CITE-RW-REFRESH", severity="INFO",
            message="Refreshed the Retraction Watch database (cache was "
                    + ("missing" if age_days is None else f"{age_days:.0f} days old") + ").",
            source=_RW_SOURCE,
        )
    except Exception as exc:  # network down, Crossref moved, etc.
        if dest_path.exists():
            return dest_path, Finding(
                check="CITE-RW-STALE", severity="INFO",
                message=f"Using the cached Retraction Watch db; refresh failed ({exc}).",
                source=_RW_SOURCE,
            )
        return None, Finding(
            check="CITE-RW-UNAVAIL", severity="INFO",
            message=f"Retraction Watch checks skipped — database unavailable ({exc}). "
                    "Run `cite-check --refresh` when online to populate it.",
            source=_RW_SOURCE,
        )


def load_retraction_db(csv_path: Union[str, Path]) -> Dict[str, dict]:
    """Load the Retraction Watch CSV into a normalised DOI → record mapping.

    The CSV (Crossref-distributed) has at minimum the columns:
        OriginalPaperDOI, RetractionDOI, RetractionNature, Title, RetractionDate

    DOIs are normalised (lowercase, stripped of ``https://doi.org/`` prefix).
    When a DOI appears multiple times, the most severe ``RetractionNature`` wins
    (Retraction > Expression of Concern > Correction > Reinstatement); all
    natures for that DOI are collected in ``all_natures``.

    Returns a dict mapping normalised DOI → {
        "nature": str,         # most severe nature
        "all_natures": list[str],
        "date": str,
        "title": str,
        "retraction_doi": str,
    }
    """
    db: Dict[str, dict] = {}
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            doi = _normalise_doi(row.get("OriginalPaperDOI", "") or "")
            if not doi:
                continue
            nature = (row.get("RetractionNature") or "").strip()
            date = (row.get("RetractionDate") or "").strip()
            title = (row.get("Title") or "").strip()
            ret_doi = _normalise_doi(row.get("RetractionDOI", "") or "")

            if doi not in db:
                db[doi] = {
                    "nature": nature,
                    "all_natures": [nature] if nature else [],
                    "date": date,
                    "title": title,
                    "retraction_doi": ret_doi,
                }
            else:
                existing = db[doi]
                # Collect all natures
                if nature and nature not in existing["all_natures"]:
                    existing["all_natures"].append(nature)
                # Keep the most severe nature (compare case-insensitively)
                cur_sev = _NATURE_SEVERITY.get(existing["nature"].lower(), -1)
                new_sev = _NATURE_SEVERITY.get(nature.lower(), -1)
                if new_sev > cur_sev:
                    existing["nature"] = nature
                    existing["date"] = date
                    existing["retraction_doi"] = ret_doi
    return db


def check_retractions(
    dois: List[str], db: Dict[str, dict]
) -> List[Finding]:
    """Flag cited DOIs found in the Retraction Watch database.

    Severity mapping:
        Retraction              → ERROR
        Expression of Concern   → WARN  (paper is usually still citable; advisory)
        Correction              → WARN  (paper is usually still citable; advisory)
        Reinstatement           → INFO  (ignored; paper reinstated)

    Each Finding carries ``source="Retraction Watch (Crossref-distributed)"``.
    """
    findings: List[Finding] = []
    for doi in dois:
        norm = _normalise_doi(doi)
        if not norm:
            continue
        record = db.get(norm)
        if not record:
            continue
        nature = record.get("nature", "")
        title = record.get("title", "")
        date = record.get("date", "")
        label = f"{title!r}" if title else f"DOI {doi}"
        date_str = f" ({date})" if date else ""

        nature_lower = nature.lower()
        if nature_lower == "retraction":
            findings.append(Finding(
                check="CITE-RETRACTED",
                severity="ERROR",
                message=(
                    f"Cited paper has been RETRACTED{date_str}: {label}. "
                    f"DOI: {doi}"
                ),
                source=_RW_SOURCE,
            ))
        elif nature_lower in ("expression of concern", "correction"):
            advisory = (
                "This paper is usually still citable, but verify the concern "
                "has been addressed before including it in a grant."
            )
            findings.append(Finding(
                check="CITE-CONCERN",
                severity="WARN",
                message=(
                    f"Cited paper has a {nature!r} notice{date_str}: {label}. "
                    f"DOI: {doi}. {advisory}"
                ),
                source=_RW_SOURCE,
            ))
        # Reinstatement → INFO / ignore (paper is back)
    return findings


def refresh_retraction_db(dest: Optional[Union[str, Path]] = None, *,
                          url: Optional[str] = None) -> Path:
    """Download the Retraction Watch CSV from ``url`` and save to ``dest``.

    Defaults to the Crossref Labs polite-pool endpoint — built at CALL time from
    :func:`zoterocite._http.contact_email` so a per-process contact-email
    override (``ZOTERO_WORD_CITE_CONTACT_EMAIL`` / ``NCBI_EMAIL``) is reflected in the
    polite-pool mailto.  Passing ``url=None`` (the default) resolves that
    endpoint; passing an explicit empty ``url`` is still rejected; if Crossref
    moves the distribution, pass an explicit non-empty ``url`` (see
    https://www.crossref.org/labs/retraction-watch/).

    ``dest`` defaults to ``default_rw_path()``; its parent directory is
    created if absent. Raises RuntimeError on download failure.
    """
    if url is None:
        # Resolve the polite-pool endpoint now, honouring any call-time env
        # override of the contact email (RETRACTION_WATCH_URL is frozen at
        # import; this re-derives it).
        url = (
            "https://api.labs.crossref.org/data/retractionwatch?"
            f"{_http.contact_email()}"
        )
    if not url:
        raise ValueError(
            "url must be supplied — check "
            "https://www.crossref.org/labs/retraction-watch/ for the current link."
        )
    dest_path = Path(dest) if dest else default_rw_path()
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": _http.user_agent()},
    )
    tmp_path = dest_path.with_suffix(".tmp")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
            data = resp.read()
    except (urllib.error.URLError, http.client.IncompleteRead, OSError) as exc:
        raise RuntimeError(
            f"Failed to download Retraction Watch CSV from {url!r}: {exc}"
        ) from exc
    except Exception as exc:
        raise RuntimeError(
            f"Unexpected error downloading Retraction Watch CSV from {url!r}: {exc}"
        ) from exc
    # Sanity-check: the bytes should look like the expected CSV header
    sample = data[:2048]
    try:
        sample_text = sample.decode("utf-8-sig", errors="replace")
    except Exception:
        sample_text = ""
    if "RetractionNature" not in sample_text or "OriginalPaperDOI" not in sample_text:
        raise RuntimeError(
            f"Downloaded content from {url!r} does not look like the Retraction Watch CSV "
            f"(expected headers 'RetractionNature' and 'OriginalPaperDOI' in first 2 KB). "
            f"Not replacing the cached file."
        )
    # Atomic write: write to tmp, then rename over the destination
    try:
        tmp_path.write_bytes(data)
        os.replace(tmp_path, dest_path)
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"Failed to write Retraction Watch CSV to {dest_path!r}: {exc}"
        ) from exc
    return dest_path


# ---------------------------------------------------------------------------
# 3. DOI extraction from citation fields
# ---------------------------------------------------------------------------

def _iter_cited_itemdata(path):
    """Yield ``(key, itemData)`` for every cited item embedded in ZOTERO_ITEM
    fields.

    This is the SINGLE owner of "what the document cited" (structured): it walks
    the raw field codes, parses the CSL-JSON payload, and surfaces each
    ``citationItems[].itemData`` dict together with its item key.  Every
    consumer that needs cited bibliographic data — DOIs, titles, authors —
    iterates this generator instead of re-implementing the field/JSON parse.

    ``scan_citations`` does not expose ``itemData``, so we re-read the field
    codes here.  The item key is taken from ``itemData.id`` (Zotero stores
    ``<libraryID>/<key>``; we return the trailing key) and falls back to the
    raw ``id``.  Never raises on a malformed payload — a bad field code is
    skipped.
    """
    import json as _json
    root = Docx(path).read_tree(DOCUMENT)
    for code in _field_codes(root):
        c = code.strip()
        if "ZOTERO_ITEM CSL_CITATION" not in c or "{" not in c:
            continue
        try:
            data = _json.loads(c[c.index("{"):c.rindex("}") + 1])
        except _json.JSONDecodeError:
            continue
        for ci in data.get("citationItems", []):
            idata = ci.get("itemData") or {}
            raw_id = str(idata.get("id") or ci.get("id") or "").strip()
            key = raw_id.rsplit("/", 1)[-1] if raw_id else ""
            yield key, idata


def _extract_cited_dois(path) -> List[str]:
    """Collect DOIs from the CSL-JSON itemData embedded in ZOTERO_ITEM fields."""
    dois: List[str] = []
    seen: Set[str] = set()
    for _key, idata in _iter_cited_itemdata(path):
        doi = (idata.get("DOI") or idata.get("doi") or "").strip()
        if doi:
            norm_key = _normalise_doi(doi)
            if norm_key not in seen:
                dois.append(doi)
                seen.add(norm_key)
    return dois


# A PMID is a bare run of digits.  Zotero exposes it either as a first-class
# CSL field ``PMID`` (rare in exported CSL-JSON) or — the common convention —
# stuffed into the ``extra`` / ``note`` field as a ``PMID: 12345678`` line
# (Zotero's "Extra" field round-trips arbitrary ``key: value`` lines).  Match
# the label case-insensitively with optional colon/whitespace, anchored on a
# word boundary so "PMID" inside a larger token (e.g. a URL) does not trigger.
_PMID_LABEL_RE = re.compile(r"\bPMID\s*:?\s*(\d{1,9})\b", re.IGNORECASE)


def _extract_pmid(idata: dict) -> str:
    """Pull a PMID out of one CSL-JSON ``itemData`` dict, or ``""``.

    Priority:
      1. A first-class ``PMID`` field (``itemData["PMID"]``), digits only.
      2. A ``PMID: <digits>`` line in the Zotero ``extra``/``note`` field —
         the common convention for carrying a PMID through CSL-JSON, parsed
         with :data:`_PMID_LABEL_RE` so surrounding ``key: value`` lines are
         ignored.

    Returns a bare digit string (no ``PMID`` prefix) or ``""`` when none is
    present / parseable.  Never raises.
    """
    # 1. First-class CSL field.  Zotero usually omits it from CSL-JSON, but
    # honour it when present (some exporters / manual itemData include it).
    raw = idata.get("PMID")
    if raw is None:
        raw = idata.get("pmid")
    if raw is not None:
        s = re.sub(r"\D", "", str(raw))
        if s:
            return s

    # 2. The "PMID: 12345678" convention in extra / note.
    for field in ("extra", "note"):
        blob = idata.get(field)
        if not blob:
            continue
        m = _PMID_LABEL_RE.search(str(blob))
        if m:
            return m.group(1)

    return ""


def _extract_cited_references(path) -> List[dict]:
    """Collect structured cited references for identity checks.

    Returns one dict per cited item::

        {"key": str, "doi": str, "pmid": str, "title": str, "authors": list[dict]}

    where ``authors`` is the CSL-JSON ``itemData.author`` list (each
    ``{"family": ..., "given": ...}``) and ``pmid`` is a bare digit string (or
    ``""``) sourced from :func:`_extract_pmid` (the ``PMID`` field, else a
    ``PMID: …`` line in ``extra``/``note``).  Shares the same field-code/
    CSL-JSON parse as :func:`_extract_cited_dois` via
    :func:`_iter_cited_itemdata`, so there is one owner of cited-reference
    extraction.  Items are de-duplicated on item key when present (the same
    source cited twice yields one entry); keyless items are kept as-is.
    """
    refs: List[dict] = []
    seen_keys: Set[str] = set()
    for key, idata in _iter_cited_itemdata(path):
        if key and key in seen_keys:
            continue
        if key:
            seen_keys.add(key)
        doi = (idata.get("DOI") or idata.get("doi") or "").strip()
        pmid = _extract_pmid(idata)
        title = (idata.get("title") or "").strip()
        authors = idata.get("author") or []
        if not isinstance(authors, list):
            authors = []
        refs.append({"key": key, "doi": doi, "pmid": pmid,
                     "title": title, "authors": authors})
    return refs


# ---------------------------------------------------------------------------
# 4. Crossref existence check
# ---------------------------------------------------------------------------

def _check_doi_exists(doi: str) -> bool:
    """Return True if Crossref knows about this DOI."""
    norm = _normalise_doi(doi)
    if not norm:
        return False
    url = f"https://api.crossref.org/works/{urllib.parse.quote(norm, safe='/')}"
    # NOTE (W9-2, DEFERRED): this keeps its OWN urllib transport rather than
    # routing through ``_http.http_get`` on purpose.  The shared primitive is
    # never-raise and returns ``None`` on ANY failure, with no way to tell a 404
    # (DOI genuinely absent → False) from a transport failure (could-not-check →
    # must raise).  Collapsing both to ``None`` would make a real DOI look
    # nonexistent whenever offline — the exact false-negative the caller at
    # ``cite_check`` step 4 guards against (it only emits CITE-NOT-FOUND on a
    # definitive False, and wraps a raise as CITE-EXISTENCE-UNAVAIL).  So we
    # preserve the three-way contract here and only adopt the shared polite-pool
    # UA (W9-1).
    req = urllib.request.Request(
        url,
        headers={"User-Agent": _http.user_agent()},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            return resp.status == 200
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise
    except urllib.error.URLError:
        raise


import urllib.parse  # noqa: E402 — needed for _check_doi_exists


# ---------------------------------------------------------------------------
# 4b. DOI<->metadata identity + author cross-check (network, opt-in)
#
# These two checks assert that a cited record actually MATCHES the authoritative
# record behind its identifier — a live DOI is NOT verification.  They reuse the
# single Crossref-by-DOI path (refresolve._crossref_doi_fetch) and the identity
# primitives owned by refresolve; they add no second metadata fetch or name
# matcher.  Both emit WARN (advisory): a mismatch is a strong signal worth human
# review, not a hard failure of the gate.
# ---------------------------------------------------------------------------

def _check_doi_metadata_identity(refs: List[dict]) -> List[Finding]:
    """Check A — assert each cited DOI resolves to the cited paper.

    For every cited item carrying BOTH a DOI and a title, fetch the
    authoritative record by DOI (the single Crossref-by-DOI path) and compare
    the resolved title against the cited title via normalised token overlap.
    Below the identity floor -> the DOI points at a different paper
    ("DOI-misdirection": a wrong/fabricated DOI that resolves to a real but
    unrelated paper; or a "mashup" of stitched-together fields).  A
    first-author family mismatch is folded into the SAME finding to reinforce
    the signal rather than emitting a second one.

    Silently skips an item with no DOI, no cited title, or a fetch that returns
    nothing (offline-degrade — never raises, mirroring the rest of this module).
    """
    from . import refresolve as _rr

    findings: List[Finding] = []
    for ref in refs:
        doi = (ref.get("doi") or "").strip()
        cited_title = (ref.get("title") or "").strip()
        if not doi or not cited_title:
            continue
        try:
            resolved = _rr._crossref_doi_fetch(_normalise_doi(doi))
        except Exception:  # noqa: BLE001 — offline-degrade like the rest of citecheck
            resolved = None
        if not resolved:
            continue
        resolved_title = (resolved.get("title") or "").strip()
        if not resolved_title:
            continue

        overlap = _rr._title_overlap(resolved_title, cited_title)
        if overlap >= _rr._IDENTITY_TITLE_OVERLAP_MIN:
            continue  # titles agree — same paper

        key = ref.get("key") or doi
        msg = (
            f"citation {key!r}: DOI {doi} resolves to a different paper. "
            f"Cited title {cited_title!r} but DOI {doi} is "
            f"{resolved_title!r} (title overlap {overlap:.2f} < "
            f"{_rr._IDENTITY_TITLE_OVERLAP_MIN:.2f})."
        )

        # Fold a first-author family mismatch into the same message.
        cited_fam = _rr._first_author_family(ref.get("authors") or [])
        resolved_fam = _rr._first_author_family(resolved.get("authors") or [])
        if cited_fam and resolved_fam and \
                _rr._normalize_surname(cited_fam) != _rr._normalize_surname(resolved_fam):
            msg += (
                f" First author also differs: {cited_fam!r}(cited) vs "
                f"{resolved_fam!r}(DOI record)."
            )

        findings.append(Finding(
            check="CITE-DOI-MISMATCH",
            severity="WARN",
            message=msg,
            source="Crossref",
        ))
    return findings


def _pubmed_authors(pmid: str) -> List[dict]:
    """Structured PubMed authors for one PMID, or ``[]`` (offline-degrade).

    Thin wrapper over :func:`entrez.efetch_pubmed_authors` — the single owner of
    structured PubMed authors — so this module never re-implements the EFetch
    parse.  Returns the ``[{"family", "given"}]`` list (collectives flagged as
    ``{"family": <name>, "given": ""}`` so the corporate guard skips them) or
    ``[]`` on any failure.  Never raises.
    """
    p = (pmid or "").strip()
    if not p:
        return []
    try:
        from . import entrez as _entrez
        result = _entrez.efetch_pubmed_authors([p])
    except Exception:  # noqa: BLE001 — offline-degrade like the rest of citecheck
        return []
    authors = result.get(p) or []
    return authors if isinstance(authors, list) else []


def _check_author_families(refs: List[dict]) -> List[Finding]:
    """Check B — family-by-family cross-check of cited authors vs. the
    authoritative author list.

    Authoritative-source priority (the comparison itself is identical — the same
    :func:`refresolve._author_family_compare`; only the source list differs):

    * **DOI -> Crossref authors** (primary): the single Crossref-by-DOI path,
      which yields structured ``{family, given}``.  Used whenever the cited item
      carries a DOI and that fetch returns a non-empty author list.
    * **PMID -> PubMed authors** (fallback): consulted ONLY when the item has no
      DOI, or the DOI is present but Crossref returned nothing / no authors, AND
      the item carries a PMID.  Structured authors come from
      :func:`entrez.efetch_pubmed_authors` (the single owner of structured
      PubMed authors); collectives arrive flagged so the corporate guard skips
      them, exactly as on the Crossref side.

    Skips silently when an item has neither a usable DOI-author list nor a PMID,
    or every fetch returns nothing / no authors (offline-degrade — never raises,
    mirroring the rest of this module).  Emits the same WARN ``CITE-AUTHOR-
    MISMATCH``; the message names which authoritative source was used.
    """
    from . import refresolve as _rr

    findings: List[Finding] = []
    for ref in refs:
        doi = (ref.get("doi") or "").strip()
        pmid = (ref.get("pmid") or "").strip()
        cited_authors = ref.get("authors") or []
        if not cited_authors:
            continue

        # ---- Resolve the ONE authoritative author list, DOI-primary. --------
        actual_authors: List[dict] = []
        source_label = ""   # human-readable, embedded in the message
        source_name = ""     # Finding.source attribution

        if doi:
            try:
                resolved = _rr._crossref_doi_fetch(_normalise_doi(doi))
            except Exception:  # noqa: BLE001 — offline-degrade
                resolved = None
            if resolved:
                crossref_authors = resolved.get("authors") or []
                if crossref_authors:
                    actual_authors = crossref_authors
                    source_label = f"the DOI record ({doi})"
                    source_name = "Crossref"

        # PMID fallback: only when DOI gave us no usable author list above.
        if not actual_authors and pmid:
            pubmed_authors = _pubmed_authors(pmid)
            if pubmed_authors:
                actual_authors = pubmed_authors
                source_label = f"the PubMed record (PMID {pmid})"
                source_name = "PubMed"

        if not actual_authors:
            continue  # nothing authoritative to compare against

        result = _rr._author_family_compare(cited_authors, actual_authors)
        if not result["mismatch"]:
            continue

        key = ref.get("key") or doi or (f"PMID {pmid}" if pmid else "")
        detail = "; ".join(result["details"])
        msg = (
            f"citation {key!r}: author mismatch vs {source_label}: "
            f"{detail}; count cited={result['cited_count']} vs "
            f"source={result['actual_count']}."
        )
        findings.append(Finding(
            check="CITE-AUTHOR-MISMATCH",
            severity="WARN",
            message=msg,
            source=source_name,
        ))
    return findings


# ---------------------------------------------------------------------------
# 5. cite_check — orchestrator
# ---------------------------------------------------------------------------

def cite_check(
    path,
    *,
    rw_csv: Optional[Union[str, Path]] = None,
    check_existence: bool = False,
    auto_refresh: bool = True,
    rw_max_age_days: int = RW_MAX_AGE_DAYS,
) -> List[Finding]:
    """Run all citation integrity checks on ``path``.

    Steps
    -----
    1. Verify the document has Zotero fields at all; if not, return a single
       INFO finding.
    2. ``reconcile_citations`` → INFO for each orphan citation (cited but
       absent from bibliography) and each uncited reference (in bib but never
       cited).  These are INFO, not WARN, because offline the bibliography
       key-set is *derived* from the cited keys rather than read from the
       rendered bibliography, so the reconcile can only catch manual
       omit/custom mismatches — a clean result is not a verified one (see
       :func:`reconcile_citations`).  If there is no bibliography, emit a single
       INFO note; when the bibliography is present but unverifiable offline,
       emit a single INFO caveat so a clean run is not mistaken for verified.
    3. Extract cited DOIs from embedded CSL-JSON and, if a Retraction Watch
       CSV is available (``rw_csv`` or ``default_rw_path()``), run
       ``check_retractions``.
    4. If ``check_existence=True``, check each cited DOI against Crossref;
       wrap network failures as a single INFO finding rather than crashing.

    Parameters
    ----------
    path:
        Path to the .docx document.
    rw_csv:
        Optional explicit path to the Retraction Watch CSV. Falls back to
        ``default_rw_path()`` if it exists.
    check_existence:
        If True, verify each cited DOI via the Crossref API (network).

    Returns
    -------
    list[Finding]
    """
    findings: List[Finding] = []

    # Step 0: classify citation sources (foreign-manager mix). Surfaced on every
    # run so a plain cite-check reports EndNote/Mendeley/Word/manual citations
    # that should be converted to Zotero. Non-fatal if classification errors.
    foreign_findings: List[Finding] = []
    try:
        from .citeconvert import classify_citation_sources, classification_findings
        classification = classify_citation_sources(path)
        cnts = classification["counts"]
        # Also surface findings when a b:Sources store failed to parse: that
        # failure can leave counts["word"] == 0 (the affected cites lose their
        # metadata and present as unmatched), so it would otherwise be silent.
        if any(cnts[m] for m in ("mendeley", "endnote", "word", "manual")) or \
                classification.get("source_errors"):
            foreign_findings = classification_findings(classification)
    except Exception:
        classification = None

    # Step 1: check for Zotero fields
    citations = scan_citations(path)
    if not citations:
        # Check whether there are *any* field codes at all
        root = Docx(path).read_tree(DOCUMENT)
        codes = _field_codes(root)
        has_zotero = any(
            "ZOTERO_ITEM" in c or "ZOTERO_BIBL" in c for c in codes
        )
        if not has_zotero:
            # No Zotero fields. If foreign managers/manual refs were detected,
            # surface those instead of the generic "no zotero" note.
            if foreign_findings:
                return foreign_findings
            findings.append(Finding(
                check="CITE-NO-ZOTERO",
                severity="INFO",
                message=(
                    "No Zotero citation fields found in this document. "
                    "Citation integrity checks require live ZOTERO_ITEM fields."
                ),
                source="citecheck",
            ))
            return findings

    # Surface the foreign-manager mix alongside the Zotero checks.
    findings.extend(foreign_findings)

    # Step 2: reconcile citations
    rec = reconcile_citations(path)

    if rec["no_bibliography"]:
        findings.append(Finding(
            check="CITE-NO-BIB",
            severity="INFO",
            message=(
                "No ZOTERO_BIBL bibliography field found. "
                "Add a bibliography field so Zotero can verify all citations are listed."
            ),
            source="citecheck",
        ))

    for key in rec["orphan_citations"]:
        findings.append(Finding(
            check="CITE-ORPHAN",
            severity="INFO",
            message=(
                f"Citation key {key!r} is cited in text but suppressed from the "
                f"bibliography by a Zotero 'omitted' edit."
            ),
            source="citecheck",
        ))

    for key in rec["uncited_references"]:
        findings.append(Finding(
            check="CITE-UNCITED",
            severity="INFO",
            message=(
                f"Bibliography key {key!r} is a Zotero 'custom' entry added to "
                f"the bibliography but never cited in text."
            ),
            source="citecheck",
        ))

    # Honest-result caveat: the bibliography is present but its true key-set is
    # not readable offline, so a clean reconcile is not a verified one.
    if (
        not rec["no_bibliography"]
        and not rec.get("bib_keys_verified", False)
        and not rec["orphan_citations"]
        and not rec["uncited_references"]
    ):
        findings.append(Finding(
            check="CITE-BIB-UNVERIFIED",
            severity="INFO",
            message=(
                "In-text/bibliography reconcile found no mismatch, but the "
                "bibliography key-set is derived from the cited keys (not read "
                "from the rendered bibliography), so membership cannot be "
                "verified offline. Open the document in Zotero to confirm every "
                "citation is listed."
            ),
            source="citecheck",
        ))

    # Step 3: retraction check
    cited_dois = _extract_cited_dois(path)

    rw_path: Optional[Path] = None
    if rw_csv:
        rw_path = Path(rw_csv)
    elif cited_dois:
        # Auto-refresh the cached DB when missing/stale (network-resilient).
        rw_path, rw_note = ensure_retraction_db(
            max_age_days=rw_max_age_days, allow_network=auto_refresh,
        )
        if rw_note is not None:
            findings.append(rw_note)

    if rw_path and rw_path.exists() and cited_dois:
        try:
            db = load_retraction_db(rw_path)
            findings.extend(check_retractions(cited_dois, db))
        except Exception:
            findings.append(Finding(
                check="CITE-RW-UNREADABLE",
                severity="INFO",
                message=(
                    "Retraction Watch DB unreadable; skipping retraction checks. "
                    "Re-run `cite-check --refresh` to replace the corrupted cache."
                ),
                source=_RW_SOURCE,
            ))

    # Step 4: existence check (optional, network)
    if check_existence and cited_dois:
        unavailable_note_added = False
        for doi in cited_dois:
            try:
                exists = _check_doi_exists(doi)
                if not exists:
                    findings.append(Finding(
                        check="CITE-NOT-FOUND",
                        severity="WARN",
                        message=f"DOI {doi!r} could not be found in Crossref.",
                        source="Crossref",
                    ))
            except Exception:
                if not unavailable_note_added:
                    findings.append(Finding(
                        check="CITE-EXISTENCE-UNAVAIL",
                        severity="INFO",
                        message="DOI existence check via Crossref is currently unavailable.",
                        source="Crossref",
                    ))
                    unavailable_note_added = True

    # Step 5: DOI<->metadata identity + author cross-check (network, opt-in).
    # Behind the same check_existence gate as step 4 because both fetch Crossref
    # by DOI.  Both emit WARN only (advisory): a mismatch flags a likely
    # DOI-misdirection / fabricated co-author for human review but must not
    # hard-fail the (non-strict) validate gate — the gate fails only on ERROR
    # (or on WARN under --strict), so WARN keeps these advisory by construction.
    if check_existence:
        cited_refs = _extract_cited_references(path)
        if cited_refs:
            findings.extend(_check_doi_metadata_identity(cited_refs))
            findings.extend(_check_author_families(cited_refs))

    return findings
