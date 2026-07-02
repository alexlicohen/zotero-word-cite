"""EndNote-library migration — move a downloaded EndNote export into a validated
Zotero **group-library** collection and re-cite the document that uses it.

A collaborator hands over a document whose citations are EndNote ``EN.CITE``
fields plus a separate EndNote **library export** (the ``.xml`` / ``.ris`` file
the citations were built from). This module:

1. **Parses** that exported library into normalized records
   (:func:`parse_endnote_library`) — supporting EndNote-XML and RIS.
2. **Plans** a migration (:func:`plan_endnote_migration`) — a *read-only* dry
   run that resolves/validates every record (canonical DOI + confidence tier via
   :mod:`refresolve`), retraction-screens them (:mod:`citecheck`), checks which
   are already in the group library (DOI→PMID→title via
   :func:`zotero.library_index` / :func:`zotero.lookup_index_key`), and
   counts the document's EndNote citation fields and how many map to a library
   record (:mod:`citeconvert`). NOTHING is written; the document is untouched.
3. **Applies** the migration (:func:`apply_endnote_migration`) — the *gated*
   write: create a named Zotero collection + add the resolved, non-retracted,
   not-already-present records (tagged ``added-by:zotero-word-cite``), then re-cite
   the document via :func:`citeconvert.convert_to_zotero` (the records are now in
   the library, so its ``EN.CITE`` → Zotero matching resolves them).

Governance (mirrors :mod:`unify`)
---------------------------------
The shared group library is a shared resource, so the WRITE is high-stakes:

* The default is a **dry-run plan** — no library writes, no document change.
* The actual write happens only under an explicit ``apply=True`` AND a
  write-enabled key (:func:`zotero.key_can_write`); :func:`apply_endnote_migration`
  refuses otherwise (same gate as :func:`unify.apply_unification`).
* A reference MATCH below ``high`` confidence is never auto-asserted: the record
  is still migrated, but using the EndNote export's OWN parsed metadata rather
  than an uncertain (possibly wrong) resolved match, so a mis-resolved work's
  title/DOI is never silently written into the shared group.
* **Retracted** references are flagged and never auto-imported.

The only network mutation is the confirm-gated, dedup-hard, tagged Zotero write.
Tests monkeypatch every network/Zotero call; the parsing path never touches the
network.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from lxml import etree

from . import refresolve
from . import citecheck
from . import zotero
from . import citeconvert
from .findings import Finding

# The dedicated collection + tag every imported item gets (mirrors unify.py).
IMPORTED_COLLECTION = "Imported — review"
ADDED_TAG = "added-by:zotero-word-cite"

# How a refresolve confidence level maps to a tier bucket (mirrors unify.py).
_TIER_BY_CONFIDENCE = {
    "high": "high",
    "medium": "medium",
    "low": "low",
    "none": "low",
}


# ===========================================================================
# 1. parse_endnote_library — EndNote-XML + RIS → normalized records
# ===========================================================================

# Normalized record shape (every key always present):
#   {authors: [str], title: str|None, journal: str|None, year: str|None,
#    volume: str|None, pages: str|None, doi: str|None, pmid: str|None,
#    ref_type: str|None}
_RECORD_KEYS = (
    "authors", "title", "journal", "year",
    "volume", "pages", "doi", "pmid", "ref_type",
)


def _empty_record() -> dict:
    rec = {k: None for k in _RECORD_KEYS}
    rec["authors"] = []
    return rec


def _itext(el: Optional[etree._Element]) -> str:
    """Concatenated text of *el* including descendant elements.

    CRITICAL for EndNote XML: field text is wrapped in nested ``<style>``
    elements, so ``el.text`` only captures the leading fragment. ``itertext``
    walks the subtree and gathers every text node. Returns ``""`` for ``None``.
    """
    if el is None:
        return ""
    return "".join(el.itertext()).strip()


def _clean_doi(raw: str) -> Optional[str]:
    """Strip a ``doi:`` prefix and surrounding whitespace; ``None`` if empty."""
    doi = (raw or "").strip()
    if doi.lower().startswith("doi:"):
        doi = doi[4:].strip()
    return doi or None


def _looks_like_pmid(raw: str) -> Optional[str]:
    """An EndNote ``<accession-num>`` is often a bare PMID (all digits).

    Routes through the shared :data:`textpatterns.PMID_RE` so that forms like
    ``"PMID 12345678"`` and ``"PubMed PMID: 12345678"`` are also recognized, in
    addition to the legacy ``"pmid:12345678"`` and bare-digit forms.
    """
    from .textpatterns import PMID_RE
    s = (raw or "").strip()
    # 1. Try the shared extractor first (handles all prefixed forms).
    m = PMID_RE.match(s)
    if m:
        return m.group(1)
    # 2. Fall back: bare digit string (legacy behaviour preserved).
    return s if s.isdigit() else None


def _normalize_year(raw: str) -> Optional[str]:
    """Pull a 4-digit year out of a date-ish string; ``None`` if none found."""
    s = (raw or "").strip()
    if not s:
        return None
    for i in range(len(s) - 3):
        chunk = s[i:i + 4]
        if chunk.isdigit():
            return chunk
    return s or None


def _parse_endnote_xml(path: Path) -> List[dict]:
    """Parse an EndNote ``File → Export → XML`` library.

    Structure: ``<xml><records><record>…``. Field text is wrapped in nested
    ``<style>`` elements, so every field is read with :func:`_itext`
    (``itertext``), never ``.text``. A malformed individual ``<record>`` is
    skipped, not raised on.
    """
    try:
        tree = etree.parse(str(path))
    except (etree.XMLSyntaxError, OSError):
        return []
    root = tree.getroot()

    records: List[dict] = []
    for rec_el in root.iter("record"):
        try:
            rec = _empty_record()

            # Authors — <contributors><authors><author>…
            authors: List[str] = []
            for a in rec_el.findall("contributors/authors/author"):
                name = _itext(a)
                if name:
                    authors.append(name)
            rec["authors"] = authors

            # Title — <titles><title>; journal — periodical/full-title or
            # <titles><secondary-title>.
            rec["title"] = _itext(rec_el.find("titles/title")) or None
            journal = (
                _itext(rec_el.find("periodical/full-title"))
                or _itext(rec_el.find("titles/secondary-title"))
            )
            rec["journal"] = journal or None

            rec["year"] = _normalize_year(_itext(rec_el.find("dates/year")))
            rec["volume"] = _itext(rec_el.find("volume")) or None
            rec["pages"] = _itext(rec_el.find("pages")) or None

            rec["doi"] = _clean_doi(_itext(rec_el.find("electronic-resource-num")))

            # accession-num is *often* a PMID; only treat it as one when it is
            # all-digits (otherwise it may be an EndNote internal id / WoS id).
            rec["pmid"] = _looks_like_pmid(_itext(rec_el.find("accession-num")))

            # ref-type: name attribute is the human label ("Journal Article").
            rt = rec_el.find("ref-type")
            if rt is not None:
                rec["ref_type"] = (rt.get("name") or _itext(rt) or "").strip() or None

            records.append(rec)
        except Exception:  # noqa: BLE001 — never fail the whole library on one bad record
            continue
    return records


# RIS tag → which normalized field it feeds.
_RIS_AUTHOR_TAGS = ("AU", "A1", "A2", "A3")
_RIS_TITLE_TAGS = ("TI", "T1")
_RIS_JOURNAL_TAGS = ("JO", "JF", "T2", "JA")
_RIS_YEAR_TAGS = ("PY", "Y1")


def _parse_ris(text: str) -> List[dict]:
    """Parse RIS text into normalized records.

    Line shape ``XX  - value``; ``AU`` repeats; ``ER  -`` ends a record. We are
    tolerant: a line without the ``  - `` separator is ignored, fields outside a
    record are ignored, and a record is emitted at ``ER`` (and at EOF if the
    final ``ER`` is missing).
    """
    records: List[dict] = []
    cur: Optional[dict] = None
    started = False

    def _flush():
        nonlocal cur, started
        if cur is not None and started:
            records.append(cur)
        cur = None
        started = False

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r\n")
        if len(line) < 2:
            continue
        tag = line[:2].strip().upper()
        # The canonical separator is "  - " at columns 2..5, but exports vary;
        # accept any "<tag><spaces>-<space>value".
        sep = line[2:].lstrip()
        if not sep.startswith("-"):
            # Continuation / non-tag line — ignore (tolerant).
            continue
        value = sep[1:].strip()

        if tag == "TY":
            _flush()
            cur = _empty_record()
            started = True
            cur["ref_type"] = value or None
            continue
        if tag == "ER":
            _flush()
            continue
        if cur is None:
            # Field before any TY — start an implicit record (tolerant).
            cur = _empty_record()
            started = True

        try:
            if tag in _RIS_AUTHOR_TAGS:
                if value:
                    cur["authors"].append(value)
            elif tag in _RIS_TITLE_TAGS:
                if value and not cur["title"]:
                    cur["title"] = value
            elif tag in _RIS_JOURNAL_TAGS:
                if value and not cur["journal"]:
                    cur["journal"] = value
            elif tag in _RIS_YEAR_TAGS:
                if not cur["year"]:
                    cur["year"] = _normalize_year(value)
            elif tag == "VL":
                cur["volume"] = value or cur["volume"]
            elif tag == "SP":
                cur["pages"] = value if not cur["pages"] else f"{value}-{cur['pages']}"
            elif tag == "EP":
                cur["pages"] = (f"{cur['pages']}-{value}" if cur["pages"] else value)
            elif tag == "DO":
                cur["doi"] = _clean_doi(value) or cur["doi"]
            elif tag in ("ID",) and value.isdigit() and not cur["pmid"]:
                cur["pmid"] = value
            elif tag == "C2" and value.isdigit() and not cur["pmid"]:
                # PubMed Central / PMID often stored in C2 by some exporters.
                cur["pmid"] = value
        except Exception:  # noqa: BLE001 — never fail the whole file on one bad line
            continue

    _flush()  # emit a trailing record if ER was missing
    return records


def parse_endnote_library(path) -> List[dict]:
    """Parse an EndNote library export into normalized records.

    Two formats are supported, detected by extension then by content:

    * **EndNote XML** (``File → Export → XML``) — ``<xml><records><record>``
      with nested ``<style>`` field wrappers (handled via ``itertext``).
    * **RIS** (``.ris``) — line-based ``TY``…``ER`` records.

    Each record is normalized to::

        {"authors": [str], "title": str|None, "journal": str|None,
         "year": str|None, "volume": str|None, "pages": str|None,
         "doi": str|None, "pmid": str|None, "ref_type": str|None}

    Tolerant by design: missing fields → ``None`` / ``[]``; a malformed entry is
    skipped (never raises); an unreadable / unrecognised file → ``[]``.
    """
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError:
        return []
    if not raw.strip():
        return []

    suffix = p.suffix.lower()

    # Decode once for content sniffing / RIS parsing (XML path re-reads via lxml).
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("latin-1")
        except Exception:  # noqa: BLE001
            return []

    head = text.lstrip()[:512].lower()
    looks_xml = head.startswith("<?xml") or "<records>" in head or "<record>" in head or "<xml>" in head
    looks_ris = bool(_first_ris_tag(text))

    if suffix in (".xml",) or (looks_xml and not suffix == ".ris"):
        recs = _parse_endnote_xml(p)
        if recs or not looks_ris:
            return recs
        # XML parse produced nothing but content looks RIS — fall through.

    if suffix == ".ris" or looks_ris:
        return _parse_ris(text)

    # Unknown extension — best-effort by content.
    if looks_xml:
        return _parse_endnote_xml(p)
    if looks_ris:
        return _parse_ris(text)
    return []


def _first_ris_tag(text: str) -> bool:
    """True if the text has at least one ``TY  - `` line (a RIS record start)."""
    for line in text.splitlines():
        if len(line) >= 4 and line[:2].upper() == "TY" and line[2:].lstrip().startswith("-"):
            return True
    return False


# ===========================================================================
# 4. validate_folder — minimum-field / resolvable-id / not-retracted check
# ===========================================================================

def validate_folder(records: List[dict]) -> List[Finding]:
    """Check each parsed record for the minimum key fields, a resolvable
    identifier, and retraction — returning a list of :class:`Finding`.

    A record is flagged (WARN) when it is missing any of authors / title / year,
    flagged (WARN) when it carries no resolvable identifier (DOI or PMID) — such
    a record can only be matched by fuzzy bibliographic search — and flagged
    (ERROR) when its DOI is in the Retraction Watch database. Records that pass
    every check produce no finding.

    The retraction screen is offline-resilient: if the Retraction Watch DB is
    unavailable, retraction findings are simply omitted (never raises).
    """
    findings: List[Finding] = []

    rw_db = _load_retraction_db()

    for i, rec in enumerate(records):
        loc = f"record {i}"
        ident = (rec.get("title") or "").strip()[:60] or f"#{i}"

        missing = [
            f for f in ("authors", "title", "year")
            if not rec.get(f)
        ]
        if missing:
            findings.append(Finding(
                check="ENDNOTE-INCOMPLETE",
                severity="WARN",
                message=(
                    f"Record {ident!r} is missing {', '.join(missing)} — "
                    "incomplete metadata may resolve to the wrong work."
                ),
                location=loc,
                source="endnote",
            ))

        if not (rec.get("doi") or rec.get("pmid")):
            findings.append(Finding(
                check="ENDNOTE-NO-ID",
                severity="WARN",
                message=(
                    f"Record {ident!r} has no DOI or PMID — it can only be matched "
                    "by fuzzy bibliographic search (lower confidence)."
                ),
                location=loc,
                source="endnote",
            ))

        if _retracted(rec.get("doi"), rw_db):
            findings.append(Finding(
                check="ENDNOTE-RETRACTED",
                severity="ERROR",
                message=(
                    f"Record {ident!r} (DOI {rec.get('doi')}) is RETRACTED — it "
                    "will NOT be imported; remove it from the library or cite a "
                    "correction."
                ),
                location=loc,
                source="endnote",
            ))

    return findings


# ===========================================================================
# Shared retraction helpers (mirror unify.py's offline-resilient pattern)
# ===========================================================================

def _load_retraction_db() -> Dict[str, dict]:
    """Load the Retraction Watch DB, degrading to ``{}`` on any failure.

    Routes through the single owner :func:`citecheck.load_retraction_map` with
    ``allow_network=True`` (explicit) — preserving this caller's always-allow-
    refresh policy: a missing/stale cache is auto-refreshed when online.
    """
    return citecheck.load_retraction_map(allow_network=True)


def _retracted(doi: Optional[str], rw_db: Dict[str, dict]) -> bool:
    """True iff ``doi`` is a retraction — the single owner's per-DOI verdict."""
    return citecheck.is_retraction(doi or "", rw_db)


# ===========================================================================
# Resolution — canonicalize one parsed record via refresolve
# ===========================================================================

def _record_query(rec: dict) -> str:
    """Build a reference-string query from a parsed record for refresolve.

    refresolve is identifier-first, so we lead with the DOI / PMID (which makes
    it resolve at ``high`` confidence) and append title/authors/year so a
    no-identifier record can still resolve via bibliographic search.
    """
    parts: List[str] = []
    if rec.get("doi"):
        parts.append(rec["doi"])
    if rec.get("pmid"):
        parts.append(f"PMID: {rec['pmid']}")
    authors = rec.get("authors") or []
    if authors:
        parts.append(", ".join(authors[:3]))
    if rec.get("title"):
        parts.append(rec["title"])
    if rec.get("journal"):
        parts.append(rec["journal"])
    if rec.get("year"):
        parts.append(str(rec["year"]))
    return ". ".join(p for p in parts if p)


def _meta_to_zotero_meta(meta: Optional[dict], rec: dict) -> dict:
    """Build the CSL-JSON-ish metadata dict :func:`zotero.create_items` accepts.

    Prefer the canonical resolved ``meta`` (from Crossref/PubMed); fall back to
    the parsed record's own fields so a record that didn't resolve online can
    still be imported with what the library gave us.
    """
    meta = meta or {}
    out: dict = {}
    out["title"] = (meta.get("title") or rec.get("title") or "").strip()
    out["doi"] = (meta.get("doi") or rec.get("doi") or "").strip()
    out["journal"] = (meta.get("journal") or rec.get("journal") or "").strip()
    year = meta.get("year") or rec.get("year")
    if year:
        out["year"] = str(year)
    out["type"] = meta.get("type") or "article-journal"
    out["volume"] = (meta.get("volume") or rec.get("volume") or "")
    out["pages"] = (meta.get("pages") or rec.get("pages") or "")

    # Authors: resolved meta carries {family, given}; the parsed record carries
    # flat "Last, First" strings — normalize both to {family, given}.
    authors: List[dict] = []
    meta_authors = meta.get("authors") or []
    if meta_authors:
        for a in meta_authors:
            if isinstance(a, dict):
                authors.append({
                    "family": (a.get("family") or "").strip(),
                    "given": (a.get("given") or "").strip(),
                })
            elif isinstance(a, str):
                fam, _, giv = a.partition(",")
                authors.append({"family": fam.strip(), "given": giv.strip()})
    else:
        for a in rec.get("authors") or []:
            fam, _, giv = a.partition(",")
            authors.append({"family": fam.strip(), "given": giv.strip()})
    if authors:
        out["authors"] = authors
    return out


def _resolve_record(rec: dict, *, fetch: bool,
                    lib_index: Optional[Dict[str, Dict[str, str]]],
                    rw_db: Dict[str, dict]) -> dict:
    """Resolve + screen one parsed record. NO writes. Returns a plan entry."""
    query = _record_query(rec)
    try:
        resolved = refresolve.resolve_reference(query, fetch=fetch)
    except Exception:  # noqa: BLE001 — degrade to a partial plan, never crash
        resolved = {"metadata": None, "confidence": "none", "source": None,
                    "candidates": []}

    meta = resolved.get("metadata")
    confidence = resolved.get("confidence") or "none"
    tier = _TIER_BY_CONFIDENCE.get(confidence, "low")

    # Canonical DOI: the resolved one wins, else the record's own.
    canonical_doi = None
    if meta and meta.get("doi"):
        canonical_doi = meta["doi"]
    elif rec.get("doi"):
        canonical_doi = rec["doi"]

    # Presence by DOI → PMID → normalized-title via the SINGLE owner of that
    # precedence (:func:`zotero.lookup_index_key`). A DOI-only test false-flagged
    # a present-but-DOI-less group item as "missing" — under --apply that
    # re-created it and DUPLICATED the shared group (the bug 59c4d64 fixed
    # everywhere else). The resolved metadata wins for each identifier, else the
    # record's own parsed value.
    pmid = None
    if meta and meta.get("pmid"):
        pmid = meta["pmid"]
    elif rec.get("pmid"):
        pmid = rec["pmid"]
    title = (meta.get("title") if meta else None) or rec.get("title")

    existing_key = zotero.lookup_index_key(
        lib_index, doi=canonical_doi, pmid=pmid, title=title
    )
    in_library = existing_key is not None

    retracted = _retracted(canonical_doi, rw_db)

    return {
        "record": rec,
        "query": query,
        "resolution": meta,
        "confidence": confidence,
        "tier": tier,
        "source": resolved.get("source"),
        "candidates": resolved.get("candidates") or [],
        "canonical_doi": canonical_doi,
        "in_library": in_library,
        "existing_key": existing_key,
        "retracted": retracted,
    }


# ===========================================================================
# 2. plan_endnote_migration — DRY RUN (no writes, no doc change)
# ===========================================================================

def plan_endnote_migration(
    doc_path,
    library_path,
    *,
    collection: Optional[str] = None,
    fetch: bool = True,
    check_retractions: bool = True,
) -> dict:
    """Plan a migration — **read-only**: no library writes, no document change.

    Parses *library_path*, resolves/validates every record via :mod:`refresolve`
    (canonical DOI + confidence tier), retraction-screens them, checks presence
    in the group library by DOI→PMID→title (:func:`zotero.library_index` +
    :func:`zotero.lookup_index_key`), and inspects
    *doc_path*'s EndNote citation fields via
    :func:`citeconvert.classify_citation_sources` (counting how many map to a
    parsed library record by DOI/title).

    Parameters
    ----------
    doc_path:
        The .docx whose EndNote citations will be re-cited (read-only here).
    library_path:
        The EndNote export (``.xml`` / ``.ris``) to migrate.
    collection:
        Target Zotero collection name. Defaults to :data:`IMPORTED_COLLECTION`.
    fetch:
        Passed to :func:`refresolve.resolve_reference` (``False`` =
        identifier-only, no network).
    check_retractions:
        When ``True``, load the Retraction Watch DB and flag retracted DOIs.

    Returns
    -------
    dict
        ``{"records": [{record, resolution, confidence, tier, in_library,
        retracted, ...}], "to_create", "to_match", "doc_citations",
        "unmatched", "collection_name", "summary", "validation"}``.

        ``to_create`` — count of resolved, non-retracted, not-in-library records
        that the apply pass would create. ``to_match`` — records already present
        in the library. ``doc_citations`` — number of EndNote citation fields in
        the document. ``unmatched`` — EndNote citation fields whose DOI/title is
        not present among the parsed library records.

    Network-resilient: a Crossref / library / Zotero failure degrades to a
    partial plan (records resolve to ``confidence="none"``, ``doc_citations``/
    library lookups fall back to empty), never a crash.
    """
    collection_name = (collection or IMPORTED_COLLECTION).strip() or IMPORTED_COLLECTION

    records = parse_endnote_library(library_path)

    # Library index (DOI+PMID+title). READ/coverage path: strict=False so a
    # DEGRADED combined read falls back to the library_doi_index disk cache
    # (DOI-only coverage) instead of going fully empty — a ref whose DOI is in
    # that cache still reads in_library rather than false-"missing". A DEGRADED
    # READ (LibraryUnavailableError) is NOT the same as an empty library: with an
    # empty index every record looks "missing" and the apply pass would create
    # DUPLICATES in the shared group. The read-only plan still degrades
    # gracefully (records resolve, just unmatched), but we record
    # ``library_available=False`` so the GATED apply refuses to create. Other
    # (non-read) failures fall back to empty maps as before. (Mirror of
    # :func:`unify.plan_unification`.)
    library_available = True
    try:
        lib_index, _ = zotero.library_index_status(strict=False)
    except zotero.LibraryUnavailableError:
        lib_index = {"doi": {}, "pmid": {}, "title": {}}
        library_available = False
    except Exception:  # noqa: BLE001
        lib_index = {"doi": {}, "pmid": {}, "title": {}}

    rw_db = _load_retraction_db() if check_retractions else {}

    resolved_records: List[dict] = []
    to_create = 0
    to_match = 0
    n_retracted = 0
    tier_counts = {"high": 0, "medium": 0, "low": 0}
    # DOI / normalized-title set of parsed library records, for doc matching.
    lib_dois: set = set()
    lib_titles: set = set()

    for rec in records:
        entry = _resolve_record(rec, fetch=fetch, lib_index=lib_index, rw_db=rw_db)
        resolved_records.append(entry)
        tier_counts[entry["tier"]] = tier_counts.get(entry["tier"], 0) + 1
        if entry["retracted"]:
            n_retracted += 1
        if entry["in_library"]:
            to_match += 1
        elif not entry["retracted"]:
            # Would be created by apply (resolved or not, non-retracted, missing).
            to_create += 1

        doi = entry["canonical_doi"]
        if doi:
            lib_dois.add(citecheck._normalise_doi(doi))
        title = (rec.get("title") or "").strip()
        if title:
            lib_titles.add(citeconvert._normalize_title(title))

    # ---- document side: count EndNote citation fields + match-ability --------
    doc_citations = 0
    unmatched: List[dict] = []
    try:
        classification = citeconvert.classify_citation_sources(doc_path)
    except Exception:  # noqa: BLE001 — degrade to a partial plan
        classification = {"counts": {}, "items": []}

    for item in classification.get("items", []):
        if item.get("manager") != "endnote":
            continue
        doc_citations += 1
        ex = item.get("extracted") or {}
        ex_doi = citecheck._normalise_doi(ex.get("doi", "")) if ex.get("doi") else ""
        ex_title = citeconvert._normalize_title(ex.get("title", "")) if ex.get("title") else ""
        matched = (ex_doi and ex_doi in lib_dois) or (ex_title and ex_title in lib_titles)
        if not matched:
            unmatched.append({
                "location": item.get("location"),
                "extracted": ex,
                "reason": "EndNote citation does not match any parsed library record",
            })

    summary = {
        "n_records": len(records),
        "tiers": dict(tier_counts),
        "to_create": to_create,
        "to_match": to_match,
        "n_retracted": n_retracted,
        "doc_citations": doc_citations,
        "n_unmatched": len(unmatched),
        "fetch": fetch,
        "library_available": library_available,
    }

    return {
        "records": resolved_records,
        "to_create": to_create,
        "to_match": to_match,
        "doc_citations": doc_citations,
        "unmatched": unmatched,
        "collection_name": collection_name,
        "library_available": library_available,
        "validation": validate_folder(records),
        "summary": summary,
    }


# ===========================================================================
# 3. apply_endnote_migration — the GATED write
# ===========================================================================

class WriteRefusedError(RuntimeError):
    """Raised when the apply pass is invoked without a write-enabled key."""


def apply_endnote_migration(
    doc_path,
    library_path,
    *,
    collection: Optional[str] = None,
    out=None,
    fetch: bool = True,
    track: bool = True,
    attach_pdfs: bool = False,
) -> dict:
    """Apply a migration — the **gated write** (create items + re-cite the doc).

    1. Plan the migration (read-only) to know which records to create.
    2. Create the named Zotero collection + add the resolved, **non-retracted**,
       not-already-present records via :func:`zotero.create_items` (deduped by
       DOI/title inside that call, tagged ``added-by:zotero-word-cite`` + the source
       label, placed in the collection).
    3. Re-cite *doc_path* by calling :func:`citeconvert.convert_to_zotero` — the
       new records are now in the library, so its ``EN.CITE`` → Zotero matching
       resolves them — writing tracked changes to *out*.

    Parameters
    ----------
    doc_path:
        The .docx to re-cite.
    library_path:
        The EndNote export to migrate.
    collection:
        Target collection name. Defaults to :data:`IMPORTED_COLLECTION`.
    out:
        Destination for the re-cited document. Defaults to ``<doc>.zotero.docx``.
    fetch:
        Passed through to resolution.
    track:
        Insert citation swaps as tracked changes (default ``True``).
    attach_pdfs:
        OPT-IN (default ``False``). Pass through to :func:`zotero.create_items`
        so each newly-created item gets a best-effort open-access PDF attached.
        Off by default — existing migration behaviour is unchanged.

    Returns
    -------
    dict
        ``{"created", "matched", "unmatched", "collection", "doc_out",
        "validation", "skipped_retracted"}``.

    Refuses the write (raises :class:`WriteRefusedError`) when the configured
    Zotero key is not write-enabled (:func:`zotero.key_can_write`) — same gate as
    :func:`unify.apply_unification`. Retracted records are never imported.
    """
    # Hard write gate — identical posture to unify.apply_unification.  Fail
    # closed on BOTH a definitive "no access" and an unverifiable result, but
    # tell the truth about which so the user retries on a transient failure
    # rather than being told (wrongly) the key lacks write access.
    _write_status = zotero.key_can_write_status()
    if not _write_status:
        if _write_status is zotero.WRITE_ACCESS_UNKNOWN:
            raise WriteRefusedError(
                "Could not reach Zotero to verify the API key's write access. "
                "The migration is a shared-library write and is refused "
                "(fail-closed). Re-run when Zotero is reachable."
            )
        raise WriteRefusedError(
            "Zotero API key has no write access to the group library. "
            "The migration is a shared-library write and is refused. Use a "
            "write-enabled key, or import the items manually and re-run with "
            "--apply for the re-cite step only."
        )

    collection_name = (collection or IMPORTED_COLLECTION).strip() or IMPORTED_COLLECTION
    source_label = Path(library_path).name

    plan = plan_endnote_migration(
        doc_path, library_path, collection=collection_name, fetch=fetch,
    )

    # Degraded library read: the plan could not enumerate the group library, so
    # every record looks "missing" and creating them would risk DUPLICATES in
    # the shared group. Refuse the write — fail closed on uncertain reads, same
    # posture as the no-write-key gate above.
    if not plan.get("library_available", True):
        raise WriteRefusedError(
            "Zotero library could not be read (degraded/unavailable), so "
            "existing items cannot be distinguished from new ones. Refusing the "
            "migration write to avoid duplicate entries in the shared group. "
            "Re-run when the library is reachable."
        )

    # Collect metadata for records to create: resolved (or parsed fallback),
    # NON-RETRACTED, not already present in the library.
    to_create_metas: List[dict] = []
    matched: List[dict] = []
    skipped_retracted: List[dict] = []

    for entry in plan["records"]:
        rec = entry["record"]
        if entry["retracted"]:
            skipped_retracted.append({
                "title": (rec.get("title") or "")[:80],
                "doi": entry["canonical_doi"],
            })
            continue
        if entry["in_library"]:
            matched.append({
                "title": (rec.get("title") or "")[:80],
                "key": entry["existing_key"],
                "doi": entry["canonical_doi"],
            })
            continue
        # A resolved MATCH is asserted into the shared library only when it is
        # HIGH confidence. Below that the match may be the WRONG work, so import
        # the EndNote export's OWN parsed metadata instead of an uncertain
        # resolution — never silently write a mis-resolved work's title/DOI into
        # the group (governance: below-high matches are not auto-asserted).
        resolution = entry["resolution"] if entry.get("tier") == "high" else None
        to_create_metas.append(_meta_to_zotero_meta(resolution, rec))

    # ---- the WRITE: create items (deduped, tagged, in-collection) ------------
    # Re-fetch the combined library index (DOI+PMID+title) and thread its DOI AND
    # PMID maps into create_items so its per-item dedup answers from the index
    # (O(1)/item) instead of a full-library fetch_all scan per DOI (the F3 O(N·M)
    # blow-up) — and so a ref present only by PMID is deduped (defense-in-depth),
    # not re-created. WRITE path: strict=True (fail-closed). A stale DOI-only
    # fallback is DELIBERATELY refused — it could miss recently-added items and
    # mass-duplicate the shared group; a degraded read MUST refuse rather than
    # create blind. (Mirror of :func:`unify.apply_unification`.)
    try:
        lib_index = zotero.library_index(strict=True)
    except zotero.LibraryUnavailableError:
        # Became unreachable between plan and write — refuse rather than create blind.
        raise WriteRefusedError(
            "Zotero library became unreadable before the migration write; "
            "refusing to create to avoid duplicate entries in the shared group."
        )
    doi_index = lib_index.get("doi") or {}
    pmid_index = lib_index.get("pmid") or {}

    created_report = {"created": [], "skipped_existing": [], "failed": [],
                      "skipped_degraded_read": []}
    if to_create_metas:
        created_report = zotero.create_items(
            to_create_metas,
            collection=collection_name,
            tags=[ADDED_TAG, source_label],
            dedup=True,
            doi_index=doi_index,
            pmid_index=pmid_index,
            attach_pdfs=attach_pdfs,
        )
        # Items create_items found already present count as matched, not created.
        for s in created_report.get("skipped_existing", []):
            matched.append({
                "title": s.get("title", ""),
                "key": s.get("existing_key", ""),
                "doi": None,
            })

    # ---- re-cite the document (records now in the library) -------------------
    out_path = Path(out) if out else Path(doc_path).with_suffix(".zotero.docx")
    convert_result = citeconvert.convert_to_zotero(
        doc_path, out=out_path, managers=("endnote",), track=track,
    )

    return {
        "created": created_report.get("created", []),
        "matched": matched,
        "unmatched": convert_result.get("unmatched", []),
        "skipped_retracted": skipped_retracted,
        "collection": collection_name,
        "doc_out": convert_result.get("out") or str(out_path),
        "validation": plan["validation"],
        "create_report": created_report,
        "convert": {
            "converted": len(convert_result.get("converted", [])),
            "deduped": convert_result.get("deduped", 0),
        },
    }
