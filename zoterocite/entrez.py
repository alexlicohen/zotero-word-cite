"""NCBI E-utilities + PMC ID-Converter client (network-resilient, stdlib HTTP).

Two jobs, both used by :mod:`zoterocite.biotailor` to enrich biosketch products
with real titles/abstracts so the citation-relevance ranker has text to score:

* :func:`pmcids_to_pmids` — map PMCID -> PMID via the NCBI PMC ID Converter.
* :func:`efetch_pubmed`   — fetch title/abstract/journal/year per PMID via EFetch.

Credentials come from the environment, never from code (mirrors
:mod:`zoterocite.zotero`):

============================  =================================================
``NCBI_API_KEY``              *optional* — raises the rate limit from 3 to 10
                              req/s.  Works fine without it.  Passed ONLY as a
                              query param to NCBI; never logged or returned.
``NCBI_EMAIL``               polite-pool contact; defaults to the project's
                              placeholder Crossref mailto (overridable via
                              ``ZOTERO_WORD_CITE_CONTACT_EMAIL`` / ``NCBI_EMAIL``).
============================  =================================================

Design contract: **these functions never raise to the caller.**  Every HTTP or
parse error is swallowed and a partial-or-empty result is returned, because the
ranker must degrade gracefully to citation-string-only ranking when the network
is unavailable.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request  # noqa: F401 — tests monkeypatch ``entrez.urllib.request.urlopen``
from typing import Optional

from . import _http

# E-utilities / ID-Converter / ESearch endpoints.
_IDCONV_URL = "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"
_EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
_ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"

_TOOL = "zotero-word-cite"

# NCBI caps EFetch ids at a few hundred; 100/request is comfortably polite.
_BATCH = 100
# Short courtesy pause between batches (NCBI asks <=3 req/s w/o key, <=10 w/key).
_SLEEP_NO_KEY = 0.34
_SLEEP_WITH_KEY = 0.11
_TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# Environment / config (no network, never leaks the api_key)
# ---------------------------------------------------------------------------

def _api_key() -> Optional[str]:
    key = os.environ.get("NCBI_API_KEY")
    return key.strip() if key and key.strip() else None


def _email() -> str:
    # NCBI_EMAIL keeps PRECEDENCE for NCBI calls (checked here first); when it is
    # unset we defer to the shared owner ``_http.contact_email()`` so the
    # ZOTERO_WORD_CITE_CONTACT_EMAIL override and the single fallback live in one place.
    # ``contact_email()`` itself also honours NCBI_EMAIL, so this composes safely.
    email = os.environ.get("NCBI_EMAIL")
    return email.strip() if email and email.strip() else _http.contact_email()


def _sleep_between_batches() -> None:
    time.sleep(_SLEEP_WITH_KEY if _api_key() else _SLEEP_NO_KEY)


def available() -> bool:
    """Whether network use *looks* configured/usable — does NOT make a call.

    An email is always available (defaulted), and the endpoints are public, so
    E-utilities is usable with or without an API key.  This returns ``True`` so
    long as a contact email resolves; it exists mainly so callers can short-
    circuit cleanly and so tests can monkeypatch it to force the offline path.
    """
    return bool(_email())


# ---------------------------------------------------------------------------
# Low-level HTTP (the ONLY functions that touch the network)
# ---------------------------------------------------------------------------

def _build_url(base: str, params: dict) -> str:
    """Assemble ``base?query``; appends the api_key only when present.

    The key is added here, last, so it lives only inside the URL string handed
    to urllib — it is never returned, logged, or placed in an exception we emit.
    """
    q = dict(params)
    key = _api_key()
    if key:
        q["api_key"] = key
    return f"{base}?{urllib.parse.urlencode(q)}"


def _http_get(url: str, timeout: float = _TIMEOUT) -> Optional[bytes]:
    """GET ``url`` and return the raw body, or ``None`` on ANY failure.

    Thin wrapper over :func:`zoterocite._http.http_get` (the one shared GET
    primitive).  Kept as a named function because in-module fetchers
    (:func:`pmcids_to_pmids`, :func:`efetch_pubmed`, :func:`esearch_pmids`) and
    several tests reference ``entrez._http_get`` directly — routing them all
    through this name keeps that seam intact while the GET logic lives once in
    ``_http``.  On failure we deliberately do NOT surface ``url`` (it may carry
    the api_key) — :func:`_http.http_get` returns ``None`` and never raises.
    """
    return _http.http_get(
        url,
        timeout=timeout,
        headers={"User-Agent": f"{_TOOL}/1.0 (mailto:{_email()})"},
    )


# ---------------------------------------------------------------------------
# PubMed ESearch  (the ONE esearch entrypoint — D4 consolidation)
# ---------------------------------------------------------------------------

def esearch_pmids(
    term: str,
    *,
    retmax: int = 20,
    sort: Optional[str] = None,
    timeout: float = _TIMEOUT,
) -> list[str]:
    """ESearch PubMed for ``term``; return up to ``retmax`` PMIDs (deduped).

    The single shared PubMed ESearch entrypoint for the toolkit.  Reuses
    :func:`_build_url` (so the api_key / polite-pool ``tool`` + ``email`` params
    are attached identically to EFetch / ID-Converter, and the api_key is never
    logged or surfaced in an exception) and :func:`_http_get` (the shared GET +
    never-raise contract).  Both :mod:`zoterocite.litsearch` and
    :mod:`zoterocite.biostage` call this instead of open-coding their own
    esearch URL + urlopen.

    Parameters
    ----------
    term:
        A PubMed query string — free text or fielded (e.g. ``"Cohen AL[Author]"``
        or ``"tuberous sclerosis AND autism"``).
    retmax:
        Maximum number of PMIDs to return.  Non-positive or unparseable values
        yield ``[]``.
    sort:
        Optional ESearch ``sort`` value (e.g. ``"relevance"``).  Omitted when
        ``None`` (PubMed's default, newest-first).
    timeout:
        Per-request socket timeout (seconds); defaults to the entrez timeout.

    Returns
    -------
    A de-duplicated, order-preserved list of bare digit-string PMIDs.  Any
    HTTP / JSON / parse error yields ``[]`` — this function never raises.
    """
    q = (term or "").strip()
    if not q:
        return []
    try:
        rm = int(retmax)
    except (TypeError, ValueError):
        return []
    if rm <= 0:
        return []

    params = {
        "db": "pubmed",
        "term": q,
        "retmax": str(rm),
        "retmode": "json",
        "tool": _TOOL,
        "email": _email(),
    }
    if sort:
        params["sort"] = sort

    out, _status = _esearch_pmids_impl(q, params, timeout)
    return out


def esearch_pmids_status(
    term: str,
    *,
    retmax: int = 20,
    sort: Optional[str] = None,
    timeout: float = _TIMEOUT,
) -> tuple[list[str], dict]:
    """Like :func:`esearch_pmids`, but also report whether the search was
    DEGRADED (F5).

    Returns ``(pmids, status)`` with ``status = {"degraded": bool, "reason":
    str | None}``.  ``degraded`` is ``True`` only when the empty result is due
    to a fetch/parse FAILURE, NOT when PubMed genuinely returned zero hits — so
    a caller (e.g. ``biostage``) can tell "no PubMed hits for this author" from
    "PubMed was unreachable".  Never raises.
    """
    q = (term or "").strip()
    if not q:
        return [], {"degraded": False, "reason": "empty query"}
    try:
        rm = int(retmax)
    except (TypeError, ValueError):
        return [], {"degraded": False, "reason": "invalid retmax"}
    if rm <= 0:
        return [], {"degraded": False, "reason": "non-positive retmax"}

    params = {
        "db": "pubmed",
        "term": q,
        "retmax": str(rm),
        "retmode": "json",
        "tool": _TOOL,
        "email": _email(),
    }
    if sort:
        params["sort"] = sort
    return _esearch_pmids_impl(q, params, timeout)


def _esearch_pmids_impl(q: str, params: dict, timeout: float) -> tuple[list[str], dict]:
    """Shared ESearch body: returns ``(pmids, status)`` where status carries a
    ``degraded`` flag distinguishing a fetch/parse failure from a true 0-hit."""
    url = _build_url(_ESEARCH_URL, params)
    body = _http_get(url, timeout=timeout)
    if body is None:
        return [], {"degraded": True, "reason": "ESearch fetch failed (network/HTTP)"}
    try:
        # body may be bytes (real path / most stubs) or str (some test stubs).
        text = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body)
        data = json.loads(text)
    except Exception:  # noqa: BLE001 — resilience: never propagate
        return [], {"degraded": True, "reason": "ESearch response was not valid JSON"}

    idlist = (((data or {}).get("esearchresult") or {}).get("idlist")) or []
    out: list[str] = []
    seen: set = set()
    for pid in idlist:
        s = str(pid).strip()
        if s and s.isdigit() and s not in seen:
            seen.add(s)
            out.append(s)
    # A successful fetch with zero hits is NOT degraded.
    return out, {"degraded": False, "reason": None}


# ---------------------------------------------------------------------------
# PMCID -> PMID  (NCBI PMC ID Converter)
# ---------------------------------------------------------------------------

def _normalize_pmcid(pmcid: str) -> Optional[str]:
    """Coerce a PMCID to the ``PMC#######`` form the converter expects.

    Accepts ``"PMC123"``, ``"pmc123"``, ``"123"``, or a versioned ``"PMC123.1"``
    (the version suffix is dropped).  Returns ``None`` if no digits are found.
    """
    if not pmcid:
        return None
    s = str(pmcid).strip()
    # Drop a leading PMC (any case) and any version suffix, then keep digits.
    s = s.upper()
    if s.startswith("PMC"):
        s = s[3:]
    digits = ""
    for ch in s:
        if ch.isdigit():
            digits += ch
        else:
            break  # stop at first non-digit (handles "123.1", "123 ", etc.)
    return f"PMC{digits}" if digits else None


def pmcids_to_pmids(pmcids: list[str]) -> dict[str, str]:
    """Map each input PMCID to its PMID via the NCBI PMC ID Converter.

    Returns ``{input_pmcid: pmid}``.  Keys are the caller's original strings
    (so callers can look results up by what they passed); values are bare PMID
    strings.  PMCIDs that do not resolve are simply absent from the result.

    Network-resilient: any HTTP/JSON error yields ``{}`` (or a partial map),
    never a raise.

    Back-compat wrapper over :func:`pmcids_to_pmids_status` (F5): a partial map
    (some batches failed) looks identical to "those PMCIDs don't resolve".  Use
    the status variant to learn whether the map is complete.
    """
    out, _status = pmcids_to_pmids_status(pmcids)
    return out


def pmcids_to_pmids_status(pmcids: list[str]) -> tuple[dict[str, str], dict]:
    """Like :func:`pmcids_to_pmids`, but also report batch-level failures (F5).

    Returns ``(out, status)`` with
    ``status = {"degraded": bool, "n_failed_batches": int, "n_batches": int}``.
    ``degraded`` is ``True`` if any batch fetch/parse failed.  Never raises.
    """
    if not pmcids:
        return {}, {"degraded": False, "n_failed_batches": 0, "n_batches": 0}

    # Map normalized -> list of original spellings (a pub may appear twice).
    norm_to_originals: dict[str, list[str]] = {}
    order: list[str] = []
    for original in pmcids:
        norm = _normalize_pmcid(original)
        if not norm:
            continue
        if norm not in norm_to_originals:
            norm_to_originals[norm] = []
            order.append(norm)
        norm_to_originals[norm].append(original)

    if not order:
        return {}, {"degraded": False, "n_failed_batches": 0, "n_batches": 0}

    out: dict[str, str] = {}
    n_batches = 0
    n_failed = 0
    for i in range(0, len(order), _BATCH):
        n_batches += 1
        batch = order[i:i + _BATCH]
        url = _build_url(
            _IDCONV_URL,
            {
                "ids": ",".join(batch),
                "format": "json",
                "tool": _TOOL,
                "email": _email(),
            },
        )
        body = _http_get(url)
        if body is None:
            n_failed += 1
            continue  # partial result; keep going with remaining batches
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:  # noqa: BLE001
            n_failed += 1
            continue
        if not isinstance(data, dict):
            n_failed += 1
            continue  # bare array, null, string, or other non-dict JSON
        for rec in (data.get("records") or []):
            if not isinstance(rec, dict):
                continue
            rec_pmcid = _normalize_pmcid(rec.get("pmcid", ""))
            pmid = rec.get("pmid")
            if not rec_pmcid or not pmid:
                continue  # records may carry an "errmsg" and no pmid
            for original in norm_to_originals.get(rec_pmcid, []):
                out[original] = str(pmid)
        if i + _BATCH < len(order):
            _sleep_between_batches()
    return out, {"degraded": n_failed > 0, "n_failed_batches": n_failed,
                 "n_batches": n_batches}


# ---------------------------------------------------------------------------
# PMID -> metadata  (EFetch PubMed XML, parsed with lxml)
# ---------------------------------------------------------------------------

def _text_or_empty(node) -> str:
    """Full text content of an lxml node, including tail-free child text."""
    if node is None:
        return ""
    return "".join(node.itertext()).strip()


def _parse_article(art) -> Optional[tuple[str, dict]]:
    """Parse one ``<PubmedArticle>`` element into ``(pmid, {...})``.

    Returns ``None`` if no PMID can be found (we key results by PMID).
    """
    # PMID — prefer the MedlineCitation/PMID (the article's own id).
    pmid_node = art.find(".//MedlineCitation/PMID")
    if pmid_node is None:
        pmid_node = art.find(".//PMID")
    pmid = _text_or_empty(pmid_node)
    if not pmid:
        return None

    title = _text_or_empty(art.find(".//Article/ArticleTitle"))

    # Abstract: one or more <AbstractText>, possibly labeled. Concatenate,
    # prefixing each labeled section with its label (e.g. "BACKGROUND: ...").
    abstract_parts: list[str] = []
    for at in art.findall(".//Abstract/AbstractText"):
        txt = _text_or_empty(at)
        if not txt:
            continue
        label = (at.get("Label") or "").strip()
        abstract_parts.append(f"{label}: {txt}" if label else txt)
    abstract = " ".join(abstract_parts).strip()

    # Journal title (full) with abbreviation fallback.
    journal = _text_or_empty(art.find(".//Journal/Title"))
    if not journal:
        journal = _text_or_empty(art.find(".//Journal/ISOAbbreviation"))

    # Year: PubDate/Year, else first 4 digits of MedlineDate.
    year = _text_or_empty(art.find(".//Journal/JournalIssue/PubDate/Year"))
    if not year:
        medline_date = _text_or_empty(
            art.find(".//Journal/JournalIssue/PubDate/MedlineDate")
        )
        for i in range(len(medline_date) - 3):
            chunk = medline_date[i:i + 4]
            if chunk.isdigit():
                year = chunk
                break

    # Authors: parse <AuthorList>/<Author> elements.
    #
    # Two shapes are produced from the SAME walk (one parse, one owner):
    #   * ``authors``            — bare "Lastname Initials" / collective-name
    #                              strings (the historical shape; existing
    #                              callers and test_entrez.py depend on it).
    #   * ``authors_structured`` — CSL-JSON-shaped ``{"family", "given"}`` dicts,
    #                              the structured form the citation-integrity
    #                              author cross-check feeds to
    #                              ``refresolve._author_family_compare``.  A
    #                              <CollectiveName> becomes ``{"family": <name>,
    #                              "given": ""}`` so ``_is_corporate_author``'s
    #                              org-keyword/no-given guard skips it rather than
    #                              false-mismatching it against a person.
    authors: list[str] = []
    authors_structured: list[dict] = []
    for author in art.findall(".//AuthorList/Author"):
        collective = _text_or_empty(author.find("CollectiveName"))
        if collective:
            authors.append(collective)
            # No forename for a collective -> the corporate guard catches it.
            authors_structured.append({"family": collective, "given": ""})
            continue
        last = _text_or_empty(author.find("LastName"))
        initials = _text_or_empty(author.find("Initials"))
        forename = _text_or_empty(author.find("ForeName"))
        if not initials:
            initials = forename
        if last:
            authors.append(f"{last} {initials}".strip())
            # Prefer the full ForeName for ``given`` (more discriminating than
            # bare initials); fall back to Initials.  Surname comparison in
            # ``_author_family_compare`` only keys on ``family``, so ``given`` is
            # advisory, but a real forename helps the corporate guard tell a
            # person from a collective.
            authors_structured.append(
                {"family": last, "given": (forename or initials).strip()}
            )

    # DOI: <ArticleId IdType="doi"> (PubmedData/ArticleIdList) or, failing that,
    # <ELocationID EIdType="doi"> (Article). Surfaced so DOI-keyed downstream
    # checks (library dedup, retraction screen) actually fire on efetch records.
    doi = ""
    for aid in art.findall(".//ArticleIdList/ArticleId"):
        if (aid.get("IdType") or "").lower() == "doi":
            doi = _text_or_empty(aid)
            if doi:
                break
    if not doi:
        for eloc in art.findall(".//ELocationID"):
            if (eloc.get("EIdType") or "").lower() == "doi":
                doi = _text_or_empty(eloc)
                if doi:
                    break

    return pmid, {
        "title": title,
        "abstract": abstract,
        "journal": journal,
        "year": year,
        "authors": authors,
        "authors_structured": authors_structured,
        "doi": doi,
    }


def efetch_pubmed(pmids: list[str]) -> dict[str, dict]:
    """Fetch ``{pmid: {title, abstract, journal, year}}`` for ``pmids``.

    Batches ids (<=100/request), sleeps briefly between batches, and is fully
    network-resilient: any HTTP/XML error yields a partial-or-empty dict, never
    a raise.  Multiple ``<AbstractText Label=...>`` sections are concatenated.

    Back-compat wrapper over :func:`efetch_pubmed_status` (F5): a partial result
    (some batches failed, or lxml is missing) is indistinguishable from "those
    PMIDs returned no article" in the bare dict.  Call
    :func:`efetch_pubmed_status` for a batch-failure count so a caller can
    record that an enrichment was incomplete rather than treat it as authoritative.
    """
    out, _status = efetch_pubmed_status(pmids)
    return out


def efetch_pubmed_status(pmids: list[str]) -> tuple[dict[str, dict], dict]:
    """Like :func:`efetch_pubmed`, but also report batch-level failures (F5).

    Returns ``(out, status)`` where ``status`` is::

        {"degraded": bool, "n_failed_batches": int, "n_batches": int}

    ``degraded`` is ``True`` if lxml is unavailable or ANY batch's fetch/parse
    failed — meaning metadata may be MISSING for PMIDs PubMed actually has, not
    just for genuinely-absent ones.  Never raises.
    """
    # De-dup while preserving order; coerce to clean digit strings.
    clean: list[str] = []
    seen: set = set()
    for p in pmids or []:
        s = str(p).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        clean.append(s)
    if not clean:
        return {}, {"degraded": False, "n_failed_batches": 0, "n_batches": 0}

    # Import lxml lazily so a missing parser can't break import of the module.
    try:
        from lxml import etree
    except Exception:  # noqa: BLE001
        # No parser → we could not enrich any batch: fully degraded.
        n_batches = (len(clean) + _BATCH - 1) // _BATCH
        return {}, {"degraded": True, "n_failed_batches": n_batches,
                    "n_batches": n_batches}

    out: dict[str, dict] = {}
    n_batches = 0
    n_failed = 0
    for i in range(0, len(clean), _BATCH):
        n_batches += 1
        batch = clean[i:i + _BATCH]
        url = _build_url(
            _EFETCH_URL,
            {
                "db": "pubmed",
                "id": ",".join(batch),
                "retmode": "xml",
                "rettype": "abstract",
                "tool": _TOOL,
                "email": _email(),
            },
        )
        body = _http_get(url)
        if body is not None:
            try:
                root = etree.fromstring(body)
            except Exception:  # noqa: BLE001
                root = None
            if root is not None:
                for art in root.findall(".//PubmedArticle"):
                    parsed = _parse_article(art)
                    if parsed is not None:
                        out[parsed[0]] = parsed[1]
            else:
                n_failed += 1  # fetched but unparseable XML
        else:
            n_failed += 1      # fetch failed (network/HTTP)
        if i + _BATCH < len(clean):
            _sleep_between_batches()
    return out, {"degraded": n_failed > 0, "n_failed_batches": n_failed,
                 "n_batches": n_batches}


def efetch_pubmed_authors(pmids: list[str]) -> dict[str, list[dict]]:
    """Fetch ``{pmid: [{"family", "given"}, ...]}`` — STRUCTURED PubMed authors.

    The single owner of structured (CSL-JSON-shaped) PubMed authors for the
    toolkit.  Reuses the same EFetch fetch + ``_parse_article`` parse as
    :func:`efetch_pubmed` (no second network path), then surfaces the
    ``authors_structured`` field that parse already produces.  Each entry is a
    ``{"family", "given"}`` dict; a ``<CollectiveName>`` group author becomes
    ``{"family": <collective>, "given": ""}`` so a consumer's corporate/
    collective guard (``refresolve._is_corporate_author``) skips it.

    This exists as the structured counterpart to :func:`efetch_pubmed` (whose
    ``authors`` is a list of bare "Lastname Initials" strings and is left
    unchanged for back-compat).  Callers that need to family-by-family compare a
    cited author list against the authoritative PubMed record use THIS function
    and feed the result straight to ``refresolve._author_family_compare``.

    Network-resilient: any HTTP/XML/parse error yields a partial-or-empty dict,
    never a raise (mirrors :func:`efetch_pubmed`).  PMIDs PubMed has no record
    for, or whose article carries no ``<AuthorList>``, map to ``[]``.
    """
    records = efetch_pubmed(pmids)
    return {
        pmid: (rec.get("authors_structured") or [])
        for pmid, rec in records.items()
    }
