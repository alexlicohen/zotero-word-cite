"""Reference resolver — turn a raw reference string into canonical bibliographic metadata.

Public API
----------
extract_identifier(text) -> dict
    Regex-extract DOI, PMID, arXiv, ISBN from a reference string.

crossref_bibliographic(query, *, rows=5) -> list[dict]
    Query Crossref ``works?query.bibliographic=...`` and return parsed candidates.

resolve_reference(text, *, fetch=True) -> dict
    Main entry: identifier-first, then bibliographic search; returns metadata +
    confidence level ("high" | "medium" | "low" | "none").

is_preprint(identifier_or_doi) -> bool
    Return True if the DOI/identifier belongs to a known preprint server
    (bioRxiv/medRxiv ``10.1101/``, Research Square ``10.21203/``, arXiv, SSRN).

check_preprint_status(doi, *, fetch=True) -> dict
    For a preprint DOI, query Crossref ``relation.is-preprint-of`` to find any
    published version.  Returns ``{"is_preprint": bool, "published_doi": str|None}``.
    ``published_doi`` is only populated when the relation entry carries
    ``"id-type": "doi"``; the published venue name is omitted (callers should
    resolve ``published_doi`` separately to get the journal name).
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from .citecheck import _normalise_doi
from . import _http
from . import entrez as _entrez
from . import textpatterns

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CROSSREF_WORKS = "https://api.crossref.org/works"
# The polite-pool User-Agent + contact email (the ``mailto`` query param) are
# owned by :mod:`zoterocite._http`; call ``_http.user_agent()`` /
# ``_http.contact_email()`` at request time instead of carrying our own copy.
_TIMEOUT = 15.0

# DOI prefixes for known preprint servers.
_PREPRINT_DOI_PREFIXES = (
    "10.1101/",   # bioRxiv and medRxiv
    "10.21203/",  # Research Square
    "10.2139/",   # SSRN (Social Science Research Network)
)

# arXiv DOI prefix when assigned via CrossRef (arxiv.org issues DOIs under 10.48550/).
_ARXIV_DOI_PREFIX = "10.48550/"

# Minimum normalised token-overlap fraction to consider a title a strong match.
_TITLE_OVERLAP_HIGH = 0.55
_TITLE_OVERLAP_MEDIUM = 0.30

# Score lead fraction over second candidate required for a "clear lead".
_SCORE_LEAD_FRAC = 0.10


# ---------------------------------------------------------------------------
# 1. extract_identifier
# ---------------------------------------------------------------------------

_DOI_RE = re.compile(
    r'10\.\d{4,9}/[^\s"<>]+',
    re.IGNORECASE,
)
_TRAILING_PUNCT = re.compile(r'[.,);:]+$')

_PMID_RE = re.compile(r'PMID:?\s*(\d+)', re.IGNORECASE)
_ARXIV_RE = re.compile(r'arxiv:\s*(\d{4}\.\d{4,5})', re.IGNORECASE)

# ISBN: 10 or 13 digits, possibly hyphenated/spaced.
# Strategy: after the ISBN label, grab a loose run of digits, hyphens and spaces,
# then verify the digit count is 10 or 13 (the check digit may be X for ISBN-10).
_ISBN_RE = re.compile(
    r'ISBN[-: ]*([\dX][\d\- X]{8,17})',
    re.IGNORECASE,
)


def extract_identifier(text: str) -> dict:
    """Extract bibliographic identifiers from a reference string.

    Returns::

        {
            "doi":   str | None,
            "pmid":  str | None,
            "arxiv": str | None,
            "isbn":  str | None,
        }

    The DOI is extracted via :func:`textpatterns.extract_dois` (which strips any
    ``doi:`` / URL prefix and trailing prose punctuation including ``]``) and then
    normalised through :func:`citecheck._normalise_doi`, so the returned DOI is the
    canonical lookup form (lowercased, prefix-stripped, no trailing ``. , ) ; : ]``).
    """
    doi: Optional[str] = None
    found = textpatterns.extract_dois(text)
    if found:
        doi = _normalise_doi(found[0]) or None

    pmid: Optional[str] = None
    m = _PMID_RE.search(text)
    if m:
        pmid = m.group(1)

    arxiv: Optional[str] = None
    m = _ARXIV_RE.search(text)
    if m:
        arxiv = m.group(1)

    isbn: Optional[str] = None
    m = _ISBN_RE.search(text)
    if m:
        digits = re.sub(r'[^0-9Xx]', '', m.group(1))
        if len(digits) in (10, 13):
            isbn = digits.upper()

    return {"doi": doi, "pmid": pmid, "arxiv": arxiv, "isbn": isbn}


# ---------------------------------------------------------------------------
# 1b. is_preprint / check_preprint_status
# ---------------------------------------------------------------------------

def is_preprint(identifier_or_doi: str) -> bool:
    """Return ``True`` if *identifier_or_doi* indicates a preprint.

    Recognised preprint signals:

    * DOI prefix ``10.1101/``  — bioRxiv / medRxiv
    * DOI prefix ``10.21203/`` — Research Square
    * DOI prefix ``10.2139/``  — SSRN
    * DOI prefix ``10.48550/`` — arXiv (when a formal DOI is assigned)
    * ``arXiv:NNNN.NNNNN`` — arXiv id with explicit ``arXiv:`` prefix

    The check is case-insensitive and works on full reference strings as well as
    bare DOIs.

    When the input contains multiple DOIs (e.g. a full reference string that
    mentions a preprint URL in passing), the reference is only classified as a
    preprint if **all** extracted DOIs have preprint prefixes.  A single
    non-preprint DOI anywhere in the string means the reference itself is not a
    preprint.
    """
    if not identifier_or_doi:
        return False

    text = identifier_or_doi.strip()

    # Collect ALL DOI tokens from the string.
    dois = [_TRAILING_PUNCT.sub("", m.group(0)).lower()
            for m in _DOI_RE.finditer(text)]

    if dois:
        preprint_dois = []
        non_preprint_dois = []
        for doi in dois:
            is_pre = any(doi.startswith(p) for p in _PREPRINT_DOI_PREFIXES) or doi.startswith(_ARXIV_DOI_PREFIX)
            if is_pre:
                preprint_dois.append(doi)
            else:
                non_preprint_dois.append(doi)
        # If any DOI is non-preprint, the reference itself is not a preprint.
        if non_preprint_dois:
            return False
        # All DOIs are preprint DOIs.
        if preprint_dois:
            return True

    # arXiv id with explicit "arXiv:" prefix (e.g. "arXiv:2301.01234").
    # Bare NNNN.NNNNN patterns are NOT accepted — they match page ranges.
    if _ARXIV_RE.search(text):
        return True

    return False


def check_preprint_status(doi: str, *, fetch: bool = True) -> dict:
    """Query Crossref for the published version of a preprint DOI.

    Parameters
    ----------
    doi:
        A DOI string (bare or as part of a reference string).
    fetch:
        If ``False``, skip the network call and return structural defaults.

    Returns
    -------
    ``{"is_preprint": bool, "published_doi": str | None}``

    ``published_doi`` is ``None`` when no published version is recorded in
    Crossref ``relation.is-preprint-of``, or when the relation entry does not
    carry ``"id-type": "doi"``.

    ``published_in`` (the published venue name) is **not** returned because the
    preprint stub's ``container-title`` reflects the preprint server, not the
    journal.  Callers that need the journal name should resolve ``published_doi``
    with a second :func:`~zoterocite.refresolve.resolve_reference` call.

    Never raises; network errors degrade gracefully to ``published_doi=None``.
    """
    # Normalise: pull the first DOI token if the caller passed a full reference.
    doi_clean: Optional[str] = None
    m = _DOI_RE.search(doi)
    if m:
        doi_clean = _TRAILING_PUNCT.sub("", m.group(0))
    else:
        doi_clean = doi.strip() or None

    is_pre = is_preprint(doi_clean or doi)

    base_result: dict = {
        "is_preprint": is_pre,
        "published_doi": None,
    }

    if not doi_clean or not fetch:
        return base_result

    # Fetch the work record from Crossref (reuses _crossref_doi_fetch path but
    # we need the raw ``relation`` field not surfaced by _parse_crossref_item).
    safe_doi = urllib.parse.quote(doi_clean, safe="/")
    url = f"{_CROSSREF_WORKS}/{safe_doi}"
    req = urllib.request.Request(url, headers={"User-Agent": _http.user_agent()})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310
            body = resp.read()
    except Exception:  # noqa: BLE001
        return base_result

    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return base_result

    message = data.get("message") or {}

    # Crossref encodes the published-version link under
    # ``relation.is-preprint-of`` (a list of relation objects).
    relation = message.get("relation") or {}
    is_preprint_of = relation.get("is-preprint-of") or []

    published_doi: Optional[str] = None

    if is_preprint_of and isinstance(is_preprint_of, list):
        first = is_preprint_of[0]
        if isinstance(first, dict):
            # Only trust the id as a DOI when id-type is explicitly "doi".
            if first.get("id-type") == "doi":
                published_doi = (first.get("id") or "").strip() or None

    return {
        "is_preprint": is_pre,
        "published_doi": published_doi,
    }


# ---------------------------------------------------------------------------
# 2. crossref_bibliographic
# ---------------------------------------------------------------------------

def _parse_crossref_item(item: dict) -> dict:
    """Extract the fields we care about from one Crossref ``works`` item."""
    doi = (item.get("DOI") or "").strip() or None

    # Title — Crossref returns a list; take the first.
    titles = item.get("title") or []
    title = titles[0].strip() if titles else None

    # Authors
    authors = []
    for a in (item.get("author") or []):
        if not isinstance(a, dict):
            continue
        family = (a.get("family") or "").strip()
        given = (a.get("given") or "").strip()
        authors.append({"family": family, "given": given})

    # Year — prefer published-print, then published-online, then issued.
    year = None
    for date_key in ("published-print", "published-online", "issued"):
        dp = item.get(date_key)
        if dp and isinstance(dp, dict):
            dp_parts = dp.get("date-parts")
            if dp_parts and dp_parts[0]:
                y = dp_parts[0][0]
                if y:
                    year = str(y)
                    break

    # Journal / container
    container = item.get("container-title") or []
    journal = container[0].strip() if container else None

    item_type = (item.get("type") or "").strip() or None
    score = item.get("score")

    # Preprint flag — derive from DOI prefix; also catches posted-content type.
    preprint = is_preprint(doi or "") or item_type in ("posted-content",)

    return {
        "doi": doi,
        "title": title,
        "authors": authors,
        "year": year,
        "journal": journal,
        "type": item_type,
        "score": score,
        "preprint": preprint,
    }


def crossref_bibliographic(query: str, *, rows: int = 5) -> list[dict]:
    """Query Crossref ``works?query.bibliographic=...`` and parse candidates.

    Returns a list of up to *rows* dicts, each with keys::

        doi, title, authors, year, journal, type, score

    Returns ``[]`` on any network error or empty result; never raises.
    """
    if not query or not query.strip():
        return []

    params = urllib.parse.urlencode({
        "query.bibliographic": query.strip(),
        "rows": str(rows),
        "mailto": _http.contact_email(),
    })
    url = f"{_CROSSREF_WORKS}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": _http.user_agent()})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310
            body = resp.read()
    except Exception:  # noqa: BLE001
        return []

    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return []

    items = (data.get("message") or {}).get("items") or []
    return [_parse_crossref_item(item) for item in items]


# ---------------------------------------------------------------------------
# 3. resolve_reference — helpers
# ---------------------------------------------------------------------------

_YEAR_RE = re.compile(r'\b(?:19|20)\d{2}\b')
_STOP_WORDS = frozenset(
    "a an the and or of in on at to for with by from is are was were be been "
    "being have has had do does did will would could should may might shall "
    "that this these those it its their our your his her".split()
)


def _title_tokens(text: str) -> set[str]:
    """Lower-case alphabetic tokens from *text*, minus stop words."""
    tokens = re.findall(r'[a-z]+', text.lower())
    return {t for t in tokens if len(t) > 2 and t not in _STOP_WORDS}


def _title_overlap(candidate_title: Optional[str], input_text: str) -> float:
    """Symmetric (Jaccard) token overlap: |A ∩ B| / |A ∪ B|.

    Jaccard, not |A∩B|/|A|, so a SHORT generic candidate title can no longer
    auto-clear the threshold against a long input: a 3-token candidate whose
    tokens all happen to appear in the input previously scored 1.0 under the
    candidate-normalised form even when it shared almost nothing of the input's
    content.  Normalising by the union penalises that asymmetry.
    """
    if not candidate_title:
        return 0.0
    a = _title_tokens(candidate_title)
    b = _title_tokens(input_text)
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _first_author_surname_in_text(candidate: dict, text: str) -> bool:
    """True if the candidate's first-author surname appears in *text*.

    Word-boundary matched (``\\b{family}\\b``) so a short surname like 'An' no
    longer matches *inside* an unrelated word ('analysis').  A surname shorter
    than 3 characters is only credited when corroborated by the author's
    initials/given-name appearing as a separate token nearby — a bare 2-letter
    surname substring is too weak to count on its own.
    """
    authors = candidate.get("authors") or []
    if not authors:
        return False
    first = authors[0]
    family = (first.get("family") or "").strip()
    if not family:
        return False

    family_hit = bool(
        re.search(rf"\b{re.escape(family)}\b", text, re.IGNORECASE)
    )
    if not family_hit:
        return False

    if len(family) >= 3:
        return True

    # Short surname (< 3 chars, e.g. 'An', 'Wu', 'Xu', 'Li'): require the
    # author's initials to corroborate so we are matching a real author token,
    # not a coincidental short word.  Initial(s) from the given name must appear
    # as a standalone token (typical "An H" / "Wu JF" reference forms).
    given = (first.get("given") or "").strip()
    initials = [g[0] for g in re.findall(r"[A-Za-z]+", given) if g]
    if not initials:
        return False
    init_pat = "".join(initials)  # e.g. "H" or "JF"
    return bool(
        re.search(
            rf"\b{re.escape(family)}\s+{re.escape(init_pat)}\b",
            text,
            re.IGNORECASE,
        )
    )


def _year_matches(candidate: dict, text: str) -> bool:
    candidate_year = candidate.get("year")
    if not candidate_year:
        return False
    text_years = set(_YEAR_RE.findall(text))
    return candidate_year in text_years


def _metadata_from_candidate(c: dict) -> dict:
    """Strip 'score' to produce clean metadata."""
    return {k: v for k, v in c.items() if k != "score"}


def _crossref_doi_fetch(doi: str) -> Optional[dict]:
    """Fetch a single work by DOI from Crossref. Returns parsed item or None."""
    safe_doi = urllib.parse.quote(doi, safe="/")
    url = f"{_CROSSREF_WORKS}/{safe_doi}"
    req = urllib.request.Request(url, headers={"User-Agent": _http.user_agent()})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310
            body = resp.read()
    except Exception:  # noqa: BLE001
        return None
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    item = (data.get("message") or {})
    if not item:
        return None
    return _parse_crossref_item(item)


def _pubmed_fetch(pmid: str) -> Optional[dict]:
    """Fetch a single PMID via entrez.efetch_pubmed. Returns metadata dict or None."""
    results = _entrez.efetch_pubmed([pmid])
    rec = results.get(pmid)
    if rec is None:
        return None
    return {
        "doi": None,
        "title": rec.get("title"),
        "authors": rec.get("authors") or [],
        "year": rec.get("year"),
        "journal": rec.get("journal"),
        "type": "journal-article",
    }


# ---------------------------------------------------------------------------
# 3. resolve_reference — main entry
# ---------------------------------------------------------------------------

def resolve_reference(text: str, *, fetch: bool = True) -> dict:
    """Resolve a raw reference string to canonical bibliographic metadata.

    Parameters
    ----------
    text:
        The raw reference string or placeholder (e.g. ``[find a ref on tubers and ASD]``).
    fetch:
        If ``False``, only identifier extraction is performed (no network).
        Confidence is derived solely from identifier presence.

    Returns
    -------
    dict with keys:

    ``input``
        The original *text*.
    ``metadata``
        ``{doi, title, authors, year, journal, type}`` or ``None``.
    ``confidence``
        ``"high"`` | ``"medium"`` | ``"low"`` | ``"none"``.
    ``source``
        ``"doi"`` | ``"pmid"`` | ``"crossref"`` | ``"pubmed"`` | ``None``.
    ``candidates``
        Up to 5 candidate dicts (useful when confidence is "medium"/"low").
    ``identifiers``
        The result of ``extract_identifier(text)``.
    """
    identifiers = extract_identifier(text)

    # ---- fetch=False path --------------------------------------------------
    if not fetch:
        if identifiers["doi"] or identifiers["pmid"]:
            confidence = "high"
        elif identifiers["arxiv"] or identifiers["isbn"]:
            confidence = "medium"
        else:
            confidence = "none"
        return {
            "input": text,
            "metadata": None,
            "confidence": confidence,
            "source": None,
            "candidates": [],
            "identifiers": identifiers,
        }

    # ---- empty input -------------------------------------------------------
    if not text or not text.strip():
        return {
            "input": text,
            "metadata": None,
            "confidence": "none",
            "source": None,
            "candidates": [],
            "identifiers": identifiers,
        }

    # ---- identifier-first: DOI --------------------------------------------
    if identifiers["doi"]:
        item = _crossref_doi_fetch(identifiers["doi"])
        if item:
            return {
                "input": text,
                "metadata": _metadata_from_candidate(item),
                "confidence": "high",
                "source": "doi",
                "candidates": [item],
                "identifiers": identifiers,
            }
        # DOI fetch failed — fall through to bibliographic
        # (rare: maybe a preprint DOI not in Crossref)

    # ---- identifier-first: PMID -------------------------------------------
    if identifiers["pmid"]:
        meta = _pubmed_fetch(identifiers["pmid"])
        if meta:
            return {
                "input": text,
                "metadata": meta,
                "confidence": "high",
                "source": "pmid",
                "candidates": [],
                "identifiers": identifiers,
            }

    # ---- bibliographic search via Crossref ---------------------------------
    candidates = crossref_bibliographic(text, rows=5)

    if not candidates:
        return {
            "input": text,
            "metadata": None,
            "confidence": "none",
            "source": None,
            "candidates": [],
            "identifiers": identifiers,
        }

    top = candidates[0]
    overlap = _title_overlap(top.get("title"), text)
    author_ok = _first_author_surname_in_text(top, text)
    year_ok = _year_matches(top, text)

    # Clear score lead over candidate #2?
    top_score = top.get("score") or 0.0
    second_score = candidates[1].get("score") or 0.0 if len(candidates) > 1 else 0.0
    if top_score > 0:
        score_lead = (top_score - second_score) / top_score
    else:
        score_lead = 0.0
    clear_lead = score_lead >= _SCORE_LEAD_FRAC or len(candidates) == 1

    # Confidence assignment.
    #
    # The three corroborating signals are: title overlap ≥ MEDIUM, first-author
    # surname present, and the candidate year present in the text.  'medium'
    # requires EITHER:
    #   * a single STRONG title signal on its own — overlap ≥ HIGH.  A
    #     near-perfect Jaccard title overlap is, by itself, a reliable identity
    #     match: a correct title-only / year-less reference (perfect title, no
    #     author token parsed, no year in the input) must resolve to 'medium' and
    #     return its metadata rather than dropping to 'low' (metadata=None) and
    #     silently losing a correct resolution downstream.  The Jaccard
    #     normalisation + HIGH threshold guard against a short generic candidate
    #     title auto-clearing here.
    #   * OR at least TWO of the three signals.  A single WEAK signal is NOT
    #     enough — any one of {overlap ≥ MEDIUM only, a coincidental surname
    #     substring, a wrong same-year hit} previously promoted a WRONG paper to
    #     'medium' and returned its metadata.
    #
    # 'high' is unchanged: strong overlap AND author AND year AND a clear score
    # lead.
    overlap_ok = overlap >= _TITLE_OVERLAP_MEDIUM
    overlap_strong = overlap >= _TITLE_OVERLAP_HIGH
    signal_count = sum((overlap_ok, author_ok, year_ok))

    if overlap_strong and author_ok and year_ok and clear_lead:
        confidence = "high"
    elif overlap_strong or signal_count >= 2:
        confidence = "medium"
    elif candidates:
        confidence = "low"
    else:
        confidence = "none"

    metadata = _metadata_from_candidate(top) if confidence in ("high", "medium") else None

    return {
        "input": text,
        "metadata": metadata,
        "confidence": confidence,
        "source": "crossref",
        "candidates": candidates,
        "identifiers": identifiers,
    }
