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
import os
import re
import unicodedata
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
#
# ``_TIMEOUT`` is the default per-Crossref-call socket timeout (seconds).  A
# bounded timeout is the retry-saver: an unreachable Crossref host can otherwise
# hang a CLI run for minutes per reference.  Deployments can lower the ceiling
# via ``ZOTERO_WORD_CITE_HTTP_TIMEOUT`` (read at call time by :func:`resolve_timeout`,
# clamped to a sane floor); with no env var set the value is the historical 15 s
# so existing behaviour and the network-mock tests are byte-identical.
_TIMEOUT = 15.0
# Never allow a sub-second timeout to slip through (a typo'd env var) and turn
# every lookup into a guaranteed failure; clamp to at least this many seconds.
_MIN_TIMEOUT = 1.0


def resolve_timeout() -> float:
    """Per-Crossref-call socket timeout (seconds), read at CALL time.

    Honours ``ZOTERO_WORD_CITE_HTTP_TIMEOUT`` (a per-process override so an unreachable
    Crossref cannot hang the run for minutes), falling back to :data:`_TIMEOUT`.
    A malformed/absent value yields the default; a too-small value is clamped to
    :data:`_MIN_TIMEOUT`.
    """
    raw = os.environ.get("ZOTERO_WORD_CITE_HTTP_TIMEOUT")
    if not raw:
        return _TIMEOUT
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return _TIMEOUT
    if val <= 0:
        return _TIMEOUT
    return max(val, _MIN_TIMEOUT)

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
    elif doi and doi.startswith(_ARXIV_DOI_PREFIX):
        # arXiv mints DOIs as ``10.48550/arXiv.<id>`` (the id itself is what we
        # need for the zero-round-trip arXiv shortcut). The DOI was lowercased by
        # ``_normalise_doi`` above, so the suffix here is ``arxiv.<id>``.
        suffix = doi[len(_ARXIV_DOI_PREFIX):]
        if suffix.lower().startswith("arxiv."):
            candidate = suffix[len("arxiv."):].strip()
            if candidate:
                arxiv = candidate

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
    body = _http.http_get(url, timeout=resolve_timeout(), headers={"Accept": "application/json"})
    if body is None:
        return base_result

    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return base_result

    message = data.get("message") or {}

    # Crossref encodes the published-version link under ``relation``. The
    # canonical key is ``is-preprint-of`` but some records express the link as
    # ``is-version-of`` / ``has-version`` instead. Each is a LIST of relation
    # objects; the DOI we want is not guaranteed to be entry [0] (a record may
    # carry a non-DOI id — e.g. a PMID or URI — at [0] and the DOI at [1]), so
    # scan every entry across all three relation lists and take the first one
    # whose ``id-type`` is explicitly ``"doi"``.
    relation = message.get("relation") or {}
    published_doi: Optional[str] = None

    for rel_key in ("is-preprint-of", "is-version-of", "has-version"):
        entries = relation.get(rel_key) or []
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            # Only trust the id as a DOI when id-type is explicitly "doi".
            if entry.get("id-type") == "doi":
                candidate = (entry.get("id") or "").strip() or None
                if candidate:
                    # A preprint is NEVER the published version. ``has-version``
                    # (and occasionally the other relations) can point at a LATER
                    # PREPRINT revision rather than the published article — skip
                    # any candidate that is itself a preprint.
                    if is_preprint(candidate):
                        continue
                    published_doi = candidate
                    break
        if published_doi:
            break

    return {
        "is_preprint": is_pre,
        "published_doi": published_doi,
    }


# ---------------------------------------------------------------------------
# 2. crossref_bibliographic
# ---------------------------------------------------------------------------

# Author-block noise that depresses Crossref ``query.bibliographic`` recall for
# two real-world reference styles that are otherwise well-formed:
#
#   1. comma-initials with an ``et al.`` BEFORE the title:
#        ``Saeedi, M.T.S., et al., Title of the paper, Journal, Year``
#   2. hyphenated-initials with ``et al.`` before the title:
#        ``Chao H-T, Collins ..., et al. Title of the paper.``
#
# In the standard style (``Surname Initials. Title. Journal. Year;...``) the
# author initials are a single clean token (``MTS``) or space-separated
# (``M T S``); in the two failing styles they arrive DOTTED (``M.T.S.``) or
# HYPHENATED (``H-T``), so a whitespace/punctuation tokeniser splits the author
# surname away from its initials and the literal ``et al`` injects two junk
# query terms.  Normalising the query — dropping ``et al`` and collapsing a run
# of single-letter UPPERCASE initials joined by ``.``/``-`` into space-separated
# letters — makes all three styles present the SAME author-token shape to
# Crossref without touching the title, DOIs, page ranges, or hyphenated words.
#
# ``_INITIALS_RUN_RE`` is deliberately UPPERCASE-only and token-bounded so it
# cannot mangle a lowercase DOI fragment (``j.neuron``), a hyphenated title word
# (``anti-inflammatory``), or a page range (``100-110``): those are either
# lowercase or a single letter followed by a multi-letter run, neither of which
# matches a run of single uppercase letters.
_ETAL_RE = re.compile(r',?\s*\bet\s+al\.?\s*,?', re.IGNORECASE)
_INITIALS_RUN_RE = re.compile(r'\b([A-Z](?:[.\-][A-Z]){1,4})\.?(?=\b|$)')


def _normalize_bibliographic_query(query: str) -> str:
    """Strip ``et al`` and collapse dotted/hyphenated initial runs in *query*.

    Used only for the Crossref ``query.bibliographic`` blob; the resolver scores
    candidates against the ORIGINAL reference text, so this normalisation affects
    recall only and can never change the confidence-ladder signals.  Idempotent;
    a standard-style reference passes through unchanged.
    """
    s = _ETAL_RE.sub(' ', query)
    s = _INITIALS_RUN_RE.sub(lambda m: ' '.join(c for c in m.group(1) if c.isalpha()), s)
    return re.sub(r'\s{2,}', ' ', s).strip()


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

    # Normalise author-block noise (``et al``, dotted/hyphenated initials) so the
    # two comma-/hyphen-initial reference styles present the SAME author-token
    # shape to Crossref as the standard style — see _normalize_bibliographic_query.
    normalized = _normalize_bibliographic_query(query)
    if not normalized:
        return []

    params = urllib.parse.urlencode({
        "query.bibliographic": normalized,
        "rows": str(rows),
        "mailto": _http.contact_email(),
    })
    url = f"{_CROSSREF_WORKS}?{params}"
    body = _http.http_get(url, timeout=resolve_timeout(), headers={"Accept": "application/json"})
    if body is None:
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
    return textpatterns.jaccard(a, b)


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
    # next to the surname.  The separator between surname and initials, and
    # between the initials themselves, is tolerant so all the real-world byline
    # styles are credited: "An H" / "Wu JF" (standard), "Ng, M.T.S." (comma +
    # dotted initials), and "Wu H-T" (hyphenated initials) — the same author
    # shapes the Crossref query normaliser folds together.
    given = (first.get("given") or "").strip()
    initials = [g[0] for g in re.findall(r"[A-Za-z]+", given) if g]
    if not initials:
        return False
    # Optional comma after the surname; '.', '-', or whitespace between initials.
    init_sep = r"[.\-\s]*"
    init_pat = init_sep.join(re.escape(i) for i in initials)
    return bool(
        re.search(
            rf"\b{re.escape(family)}\s*,?\s*{init_pat}\b",
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


# ---------------------------------------------------------------------------
# 3b. Author / identity matching primitives
#
# This module is the SINGLE owner of normalised-surname comparison and the
# corporate/collective-author guard.  Citation-integrity checks (and any other
# consumer) import these rather than re-deriving name matching, so the
# false-positive guards stay in one place.  These primitives are
# brand-neutral (no project literals) so they port cleanly to the standalone
# Zotero-citation copy.
# ---------------------------------------------------------------------------

# Title-overlap floor for asserting a resolved record is the SAME paper as the
# cited record (Check A, DOI<->metadata identity).  Tuned so an UNRELATED paper
# trips it (a wrong-but-live DOI resolves to a title sharing almost no content
# tokens -> Jaccard near 0) while benign subtitle/formatting noise does not: a
# correct cite that drops or adds a subtitle clause still shares the great
# majority of its content tokens, keeping Jaccard well above this floor.  The
# resolve_reference confidence ladder uses 0.30 (_TITLE_OVERLAP_MEDIUM) merely
# to *promote* a candidate; here we are *accusing* a DOI of pointing at the
# wrong paper, so we keep the bar deliberately low to avoid false alarms and
# only fire when the titles genuinely diverge.
_IDENTITY_TITLE_OVERLAP_MIN = 0.30


def _normalize_surname(name: str) -> str:
    """Fold a surname to a comparable ASCII-lowercase key.

    Strips combining diacritics (NFKD), maps a handful of single-glyph
    digraphs/ligatures that NFKD does not decompose, drops non-letter
    punctuation, and collapses whitespace.  So "Çolakoğlu" and "Colakoglu"
    fold to the same key — without this, a diacritic-stripped source vs. a
    composed cited name would false-MISMATCH.
    """
    n = unicodedata.normalize("NFKD", name or "")
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = n.lower().strip()
    multi = {"ß": "ss", "þ": "th", "ł": "l", "đ": "d", "ı": "i",
             "ø": "o", "æ": "ae", "œ": "oe"}
    for k, v in multi.items():
        n = n.replace(k, v)
    n = re.sub(r"[^a-z\s\-]", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


# Collective/corporate author markers.  A name carrying any of these is an
# organisation, not a person, and must be SKIPPED from the family-by-family
# comparison (else it false-MISMATCHes against the person at that position in
# the authoritative list).
_ORG_AUTHOR_RE = re.compile(
    r"\b("
    r"Group|Committee|Society|Association|Collaborat\w+|Consortium|Network|"
    r"Panel|Initiative|Organi[sz]ation|Investigators|Trialists|Task\s+Force|"
    r"Working\s+Group|Study\s+Group|Foundation|Institute|Council|Federation|"
    r"College|WHO|EASL|AHA|ACC|ESC|NICE|AASLD"
    r")\b",
    re.IGNORECASE,
)


def _is_corporate_author(family: str, given: str = "") -> bool:
    """True when an author entry denotes an organisation, not a person.

    Signals: a CSL literal author still carrying brace delimiters ``{``/``}``;
    an org keyword present with NO given/forename (people have a forename, a
    collective name does not); or an org keyword in a single-field literal that
    lacks the ``family, given`` comma structure of a personal name.
    """
    fam = (family or "").strip()
    giv = (given or "").strip()
    blob = f"{fam} {giv}".strip()
    if not blob:
        # An empty entry is not a person to compare; treat as corporate/skip.
        return True
    # A surviving brace means a CSL literal (institutional) author.
    if "{" in blob or "}" in blob:
        return True
    has_org_kw = bool(_ORG_AUTHOR_RE.search(blob))
    if not has_org_kw:
        return False
    # Org keyword present.  A real person at this position would have a given
    # name; a bare collective (no given name) with an org keyword is corporate.
    if not giv:
        return True
    # Org keyword inside a single literal field with no comma structure -> the
    # whole thing is a collective rendered as one string.
    if "," not in blob:
        return True
    return False


def _author_family_compare(cited: list, actual: list) -> dict:
    """Family-by-family + count cross-check of two author lists.

    Both ``cited`` and ``actual`` are CSL-JSON-shaped lists of
    ``{"family": ..., "given": ...}`` (or anything exposing ``.get``).
    ``actual`` is the AUTHORITATIVE list (e.g. Crossref-by-DOI).

    Comparison rules (the corporate/collective guard is load-bearing — without
    it a collective author false-MISMATCHes a person):

    * Corporate/collective entries on EITHER side at a position are skipped
      (not compared) — they are organisations, not surnames.
    * For the overlapping positions ``[0, min(len(cited), len(actual)))`` we
      compare normalised surnames; any positional inequality is a mismatch.
    * Any CITED author at a position BEYOND the authoritative list length is a
      mismatch — a real list cannot have fewer authors than were cited, so an
      extra cited name cannot be intentional truncation; it is a fabricated /
      mis-pasted co-author.
    * Count handling: cited FEWER than actual is, by default, treated as the
      common CSL ``et al`` truncation and is NOT reported on its own.  Zotero
      itemData typically carries no truncation marker we could key on, so the
      safe default is to suppress a bare "cited has fewer authors" count
      mismatch (it is overwhelmingly intentional et-al shortening, not an
      error).  Cited MORE than actual is always reported via the
      beyond-length rule above.

    Returns::

        {
            "mismatch": bool,            # any positional or beyond-length issue
            "details": list[str],        # human-readable per-issue lines
            "cited_count": int,          # personal authors counted as cited
            "actual_count": int,         # personal authors in authoritative list
            "cited_total": int,          # raw cited length (incl. collectives)
            "actual_total": int,         # raw actual length (incl. collectives)
        }
    """
    cited = cited or []
    actual = actual or []
    details: list[str] = []
    mismatch = False

    # Build PERSON-ONLY views of each list FIRST (collectives dropped), carrying
    # each person's ORIGINAL 1-based position for human-readable messages.
    # Comparing person-by-person — rather than by raw index with collectives
    # skipped in place — is what makes the alignment correct when one side carries
    # a collective the other omits: a cited "[Consortium, Murray]" vs an
    # authoritative "[Murray]" must NOT report Murray as an extra/fabricated author
    # (the old raw-index walk did, because the leading collective shifted every
    # later position by one and pushed the last real author "beyond" the list).
    def _persons(lst: list) -> list:
        out = []
        for i, e in enumerate(lst):
            if not isinstance(e, dict):
                continue
            fam = (e.get("family") or "").strip()
            if _is_corporate_author(fam, e.get("given") or ""):
                continue
            out.append((i + 1, fam))      # (original 1-based position, surname)
        return out

    cited_p = _persons(cited)
    actual_p = _persons(actual)

    n = min(len(cited_p), len(actual_p))
    for k in range(n):
        pos, c_fam = cited_p[k]
        _, a_fam = actual_p[k]
        if _normalize_surname(c_fam) != _normalize_surname(a_fam):
            mismatch = True
            details.append(
                f"author #{pos} family {c_fam!r}(cited) != {a_fam!r}(source)"
            )

    # Cited PERSONS beyond the authoritative person count: a real author list
    # cannot be SHORTER than what was cited, so these cannot be et-al truncation
    # — they are fabricated / mis-pasted names.
    for k in range(len(actual_p), len(cited_p)):
        pos, c_fam = cited_p[k]
        mismatch = True
        details.append(
            f"author #{pos} {c_fam!r}(cited) has no counterpart in the "
            f"source author list (cannot be et-al truncation)"
        )

    return {
        "mismatch": mismatch,
        "details": details,
        "cited_count": len(cited_p),
        "actual_count": len(actual_p),
        "cited_total": len(cited),
        "actual_total": len(actual),
    }


def _first_author_family(authors: list) -> Optional[str]:
    """Return the first PERSONAL author's raw family name, or None.

    Skips leading corporate/collective entries so a consortium byline does not
    masquerade as the first author.
    """
    for a in authors or []:
        if not isinstance(a, dict):
            continue
        fam = (a.get("family") or "").strip()
        if _is_corporate_author(fam, a.get("given") or ""):
            continue
        if fam:
            return fam
    return None


def _metadata_from_candidate(c: dict) -> dict:
    """Strip 'score' to produce clean metadata."""
    return {k: v for k, v in c.items() if k != "score"}


def _crossref_doi_fetch(doi: str) -> Optional[dict]:
    """Fetch a single work by DOI from Crossref. Returns parsed item or None."""
    safe_doi = urllib.parse.quote(doi, safe="/")
    url = f"{_CROSSREF_WORKS}/{safe_doi}"
    body = _http.http_get(url, timeout=resolve_timeout(), headers={"Accept": "application/json"})
    if body is None:
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
        "doi": rec.get("doi") or None,
        "pmid": str(pmid),
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
            _meta = _metadata_from_candidate(item)
            # Carry the PMID forward when the input string also bore one, so
            # downstream dedup (unify/create_items) can match present-by-PMID.
            if identifiers.get("pmid"):
                _meta.setdefault("pmid", identifiers["pmid"])
            return {
                "input": text,
                "metadata": _meta,
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
