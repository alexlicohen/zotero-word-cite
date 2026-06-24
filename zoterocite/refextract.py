"""Reference extractor for grant .docx drafts.

Inventories every citation-like object beyond the field-based citations already
detected by :func:`~zoterocite.citeconvert.classify_citation_sources`:

* **reflist** — entries in a References / Bibliography section.
* **intext** — ad-hoc author-year ``(Smith et al., 2020)`` or numeric ``[12]``
  citations in body paragraphs.
* **placeholders** — bracket stubs ``[CITE]`` / ``[ref?]`` / ``[Smith 2020]``,
  and citation-ish text buried in Word comments.
* **fields** — the structured bucket from :func:`classify_citation_sources`.

Public API
----------
.. code-block:: python

    from zoterocite.refextract import extract_references

    result = extract_references("draft.docx")
    # result["reflist"]      -> list of reference-list entries
    # result["intext"]       -> list of in-text ad-hoc citations
    # result["placeholders"] -> bracket stubs + comment citations
    # result["fields"]       -> classify_citation_sources() result
    # result["counts"]       -> summary counts

Detection runs on the **accepted view** of each paragraph (tracked deletions
excluded), so edits with tracked changes are handled correctly.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional

from lxml import etree

from . import textpatterns
from .textpatterns import (
    ALLCAPS_TOKEN,
    AUTHOR_INITIALS,
    AUTHOR_PARTICLE,
    CAP_WORD,
    REF_SECTION_LABEL,
)
from .citeconvert import classify_citation_sources
from .docxio import DOCUMENT, Docx
from .ooxml import NS, qn
from .paras import iter_paragraphs, paragraph_text
from .sections import REFERENCES_HEADING_RE, is_heading_by_pstyle

# ---------------------------------------------------------------------------
# Namespace shorthands
# ---------------------------------------------------------------------------
W = NS["w"]

# ---------------------------------------------------------------------------
# Heading detector — the shared standalone-References heading pattern.  This was
# the broadest local copy (it already carried the "citations"/"cited literature"
# synonyms); the shared pattern reproduces every heading it matched and adds
# \s+ multi-word-space tolerance, so the switch is a strict recall-superset.
# ---------------------------------------------------------------------------
_REF_HEADING_RE = REFERENCES_HEADING_RE

# Whitelisted next-section headings that legitimately END a References region
# when they appear as a standalone paragraph (whole-line match).  We terminate
# the references region ONLY on a TRUE structural heading (Word Heading pStyle /
# outlineLvl, via sections.is_heading_by_pstyle) or on one of these whitelisted
# next-section names — NEVER on a content-shaped fragment ('Nature Publishing
# Group', 'Smith JA', 'In Press') that merely looks heading-ish.  Previously a
# heuristic (≤6-word, year-less, capital-initial paragraph) silently truncated
# the reflist, dropping every reference after such a fragment with no flag.
_NEXT_SECTION_HEADING_RE = re.compile(
    r"^\s*("
    r"acknowledg(?:e?ments?|ements?)"
    r"|fund(?:ing)?(?:\s+(?:sources?|statement|information))?"
    r"|financial\s+disclosures?"
    r"|conflicts?\s+of\s+interest"
    r"|(?:competing|declaration\s+of)\s+interests?"
    r"|author\s+contributions?"
    r"|author\s+information"
    r"|contributions?"
    r"|disclosures?"
    r"|(?:additional|supporting)\s+information"
    r"|competing\s+(?:financial\s+)?interests?"
    r"|online\s+methods?"
    r"|extended\s+data"
    r"|correspondence"
    r"|footnotes?"
    r"|endnotes?"
    r"|supplement(?:al|ary)(?:\s+(?:material|materials|information|data))?"
    r"|appendix(?:\s+[A-Z\d]+)?"
    r"|appendices"
    r"|tables?"
    r"|figures?(?:\s+legends?)?"
    r"|figure\s+legends?"
    r"|abbreviations?"
    r"|data\s+availability(?:\s+statement)?"
    r"|ethics(?:\s+(?:statement|approval))?"
    r"|notes?"
    r")\s*:?\s*$",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Reference-list numbering patterns
# ---------------------------------------------------------------------------
# Boolean match: uses the shared textpatterns.NUMBERED_REF_RE (recall-superset of
# the old local copy — adds the "(1)" and spaced-bracket forms; verified
# behaviour-preserving for reflist detection by the test suite).  The EXTRACT
# pattern below stays local: it relies on a CAPTURE group (group(1) = numbering
# token) that the shared non-capturing pattern does not expose.
_NUMBERING_RE = textpatterns.NUMBERED_REF_RE
_NUMBERING_EXTRACT_RE = re.compile(r"^\s*(\[\d+\]|\(?\d+\)\.?|\d+\.)\s+")

# ---------------------------------------------------------------------------
# Heading-less run detection patterns
# ---------------------------------------------------------------------------

# Minimum number of consecutive reference-shaped paragraphs to form a "run"
_RUN_MIN_LEN = 3

# 4-digit year in bibliographic range
_YEAR_RE = re.compile(r"\b(1[89]\d{2}|20\d{2})\b")

# Leading enumerator: [1], (1), 1. or 1) — shared textpatterns.NUMBERED_REF_RE
# (recall-superset of the old local copy; verified behaviour-preserving here).
_ENUM_RE = textpatterns.NUMBERED_REF_RE

# Author-byline building blocks.  The genuinely-identical fragments
# (``AUTHOR_PARTICLE`` / ``AUTHOR_INITIALS`` / ``REF_SECTION_LABEL`` / ``CAP_WORD``
# / ``ALLCAPS_TOKEN``) now live in the shared ``textpatterns`` module as the
# single owner; ``sections`` imports the same fragments so the two author-start
# recognisers cannot drift.  The COMPILED head patterns stay LOCAL because their
# shapes differ deliberately (here: initials OPTIONAL, leading ``\s*``).
#
# ``_PARTICLE``: a capital-initial surname optionally preceded by up to three
# lowercase nobiliary particles (van / von / de / der / del / di / da / la / le /
# dos / bin / al / mac / mc ...), e.g. "van der Berg", "de la Cruz".  (The shared
# fragment de-duplicates the historical ``ten|ten`` — inert, behaviour-preserving.)
#
# ``_SURNAME`` is kept LOCAL (not shared): unlike ``sections._SURNAME`` it does
# NOT include the curly apostrophe ``’``.  That one character is NOT inert — it
# would flip ``_is_reference_shaped("O’Brien AL. … 2022.")`` from False to True —
# so the surname class is deliberately divergent and stays module-local.
_PARTICLE = AUTHOR_PARTICLE
_SURNAME = r"[A-Z][A-Za-z'\-]+"  # local: does NOT include curly apostrophe ’ (see above)
_INITIALS = AUTHOR_INITIALS  # "JA" / "J.A." / "J A" / "J"

# Author-year list pattern: starts with "Surname AB," or "Surname AB and"
# Captures multi-author formats like "Smith AB, Jones CD, ..." / "Smith A, Jones
# B." / "van der Berg H, ...".  Lowercase nobiliary particles are now permitted
# in the leading surname so "van der Berg H, ..." is recognised.  Shape: initials
# OPTIONAL, leading ``\s*`` allowed (contrast ``sections._AUTHOR_HEAD_RE``, which
# REQUIRES initials and anchors ``^`` with no leading ``\s*``).
_AUTHOR_LIST_START_RE = re.compile(
    rf"^\s*{_PARTICLE}{_SURNAME}(?:\s+{_INITIALS})?(?:\s*,|\s+and\b|\s+&)"
)

# Section labels that are sentence-case prose, NOT author fields.  An author
# manuscript / PDF→docx commonly opens a section with a 'Label.' lead-in
# ("Methods. Patients were recruited ...", "Data Availability. Data generated
# in 2022 ...").  The old organizational branch matched any "Capitalized words."
# lead-in, so such prose was wrongly classed as reference-shaped whenever a year
# happened to appear later in the line.  This negative-lookahead set excludes the
# common labels so they can never satisfy the organizational author shape.  Now
# the shared ``REF_SECTION_LABEL`` (single owner; sections imports it too).
_SECTION_LABEL_RE = REF_SECTION_LABEL

# Single-author / organizational bibliographic-entry start — the broader bar for
# heading-less reflists that have NO comma/and/& after the first surname:
#   * single author + initials + terminating period:  "Smith JA. ..."
#   * lowercase-particle single author:               "van der Berg H. ..."
#   * organizational / ALLCAPS author + period:       "ENIGMA Consortium. ...",
#                                                      "World Health Organization. ..."
# The trailing period (before the title) is what marks the end of the author
# field for these no-delimiter entries.
#
# The organizational alternative now demands a genuinely bibliographic author
# shape rather than any "Capitalized words." lead-in:
#   (a) a section label ('Methods.', 'Data Availability.') is excluded up front
#       via the negative lookahead, AND
#   (b) the author field must carry an ALLCAPS token ('ENIGMA', 'WHO') OR be at
#       least TWO capitalized words ('World Health Organization', 'International
#       League Against Epilepsy') — a lone 'Capitalizedword.' (sentence-case
#       prose like 'Background.' / 'Patients.') no longer qualifies.
_CAP_WORD = CAP_WORD          # a capitalized word
_ALLCAPS_TOKEN = ALLCAPS_TOKEN  # an acronym / ALLCAPS token (≥2 chars)
_REF_AUTHOR_START_RE = re.compile(
    rf"^\s*(?:"
    rf"{_PARTICLE}{_SURNAME}\s+{_INITIALS}\."          # Smith JA.  /  van der Berg H.
    rf"|(?!{_SECTION_LABEL_RE}[.\s])(?:"               # exclude section labels first
    rf"(?:{_CAP_WORD}\s+){{0,5}}{_ALLCAPS_TOKEN}(?:\s+{_CAP_WORD}){{0,5}}\s*\."  # has an ALLCAPS token
    rf"|{_CAP_WORD}(?:\s+{_CAP_WORD}){{1,5}}\s*\."     # ≥2 capitalized words
    rf")"
    rf")"
)

# Precision guard: paragraphs beginning with these tokens are NOT references
_NON_REF_PREFIXES_RE = re.compile(
    r"^\s*(Adapted\s+from|Reprinted\s+from|Modified\s+from|Figure\s+\d|"
    r"Fig\.\s*\d|Table\s+\d)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Contiguous year-less-entry recovery (the "#51 gap" fix)
# ---------------------------------------------------------------------------
# A numbered reference list can contain a year-less entry — an in-press / "in
# preparation" paper with no published year and no DOI/PMID
# ("51. Miller G, Steeby C. ... Imaging Neuroscience;").  The year requirement
# in :func:`_is_reference_shaped` is a PRECISION guard for AMBIGUOUS lines, but a
# numbered entry whose printed number is CONTIGUOUS with an accepted neighbour
# inside a confirmed reference list is NOT ambiguous — dropping it leaves a
# phantom gap (printed #51 missing between #50 and #52) so any in-text cite to it
# can't resolve.
#
# These helpers gate the recovery narrowly: only an enumerator-led, reference-
# LENGTH, author-shaped line whose number == (last accepted number + 1) is
# admitted.  A standalone year-less numbered body line has no accepted #N-1
# predecessor, so it never qualifies; a numbered instruction list
# ("1. Run the script.") fails the length / author-shape bar.

# Minimum length for a year-less filler to be reference-LENGTH (matches the
# author-led bar in _is_reference_shaped).
_FILLER_MIN_LEN = 40


def _enum_number(txt: str) -> Optional[int]:
    """Return the integer value of a line's leading enumerator, or ``None``.

    Handles every enumerator shape the extractor recognises: ``51.``, ``[51]``,
    ``(51)``, ``51)``, ``(51).``  Returns ``None`` for non-enumerator-led lines.
    """
    m = _NUMBERING_EXTRACT_RE.match(txt)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else None


def _is_contiguous_filler(txt: str, last_accepted_num: Optional[int]) -> bool:
    """Return True if *txt* is a year-less reference entry that fills a numeric
    hole immediately after *last_accepted_num* in a confirmed reference list.

    Narrow recovery — ALL must hold:

    * the line is enumerator-led and its number == ``last_accepted_num + 1``
      (contiguous with the previous ACCEPTED entry — never admits an entry with
      no accepted predecessor at N-1);
    * it survives the figure/table/"Adapted from" precision guard;
    * it is reference-LENGTH (> 40 chars);
    * it is author-shaped: with its leading enumerator stripped, the remainder
      matches one of the existing author-byline recognisers
      (:data:`_AUTHOR_LIST_START_RE` — "Miller G, Steeby C, ..." — or
      :data:`_REF_AUTHOR_START_RE` — "Miller G. ..."), which require a surname
      FOLLOWED BY INITIALS.  This is what separates a real byline from a numbered
      instruction sentence ("3. Submit the final report ..."): the instruction's
      first word is followed by lowercase prose, not author initials, so neither
      recogniser matches.

    A year-bearing line is handled by :func:`_is_reference_shaped` directly and
    never reaches here; this only rescues the year-LESS contiguous case.
    """
    if last_accepted_num is None:
        return False
    stripped = txt.strip()
    if not stripped:
        return False
    if _NON_REF_PREFIXES_RE.match(stripped):
        return False
    num = _enum_number(stripped)
    if num is None or num != last_accepted_num + 1:
        return False
    if len(stripped) <= _FILLER_MIN_LEN:
        return False
    # Strip the leading enumerator and re-test the remainder against the existing
    # author-byline recognisers (which require surname + INITIALS — the feature
    # that distinguishes a real byline from a numbered instruction sentence).
    # Use the local CAPTURE pattern for the offset, NOT the shared boolean
    # _ENUM_RE: the latter is a recall-superset that greedily consumes the first
    # content character, which would corrupt the surname.
    m = _NUMBERING_EXTRACT_RE.match(stripped)
    if m is None:
        return False
    rest = stripped[m.end():]
    return bool(
        _AUTHOR_LIST_START_RE.match(rest) or _REF_AUTHOR_START_RE.match(rest)
    )

# ---------------------------------------------------------------------------
# In-text citation patterns
# ---------------------------------------------------------------------------

# Author-year: (Smith et al., 2020) or (Smith & Jones, 2020a; Brown, 2019)
# Also catches: Author (2020) — the bare form with the year in parens
_AUTHOR_YEAR_PAREN_RE = re.compile(
    r"\(([A-Z][A-Za-z'\-]+(?:\s+et\s+al\.?)?(?:\s*(?:&|and)\s*[A-Z][A-Za-z'\-]+)?,"
    r"?\s+\d{4}[a-z]?(?:\s*;\s*[^)]{0,60})?)\)",
)
# Bare "Author (2020)" form — author immediately followed by year in parens
_AUTHOR_YEAR_BARE_RE = re.compile(
    r"([A-Z][A-Za-z'\-]+(?:\s+et\s+al\.?)?)\s+\((\d{4}[a-z]?)\)",
)

# Numeric citations: [12], [3,4], [5-7], (12), (3,4)
# Must look like citation markers, not measurements (no units after them).
_NUMERIC_CITE_RE = re.compile(
    r"(?<!\d)[\[\(](\d+(?:\s*[,\-–]\s*\d+)*)[\]\)](?!\s*(?:mg|ml|kg|cm|mm|µm|nm|%|°|g\b|s\b|h\b|d\b|yr\b|min\b|sec\b|kDa\b))",
)
# Require at least one purely numeric token — filter out things like (p<0.05)
_NUMERIC_DIGITS_ONLY_RE = re.compile(r"^\d+(?:\s*[,\-–]\s*\d+)*$")

# Max number to be treated as a citation (avoids flagging years, large measurements)
_MAX_CITE_NUM = 999

# ---------------------------------------------------------------------------
# Placeholder detection
# ---------------------------------------------------------------------------
_PLACEHOLDER_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_PLACEHOLDER_BRACKET_RE = re.compile(r"\[([^\]]{1,80})\]")

# Patterns that identify definitely-citation brackets (override other filters).
# The keyword alternation is word-bounded so an ordinary bracket whose content
# merely CONTAINS the letters "ref"/"cite" — "[preferred]", "[referral pathway]",
# "[cross-referenced data]" — is NOT misread as a citation placeholder (which
# would demand a spurious confirm/resolution in the unify plan). The cite/ref
# families allow a plural 's' and a trailing reference number ("refs", "ref12",
# "citations", "references") so those genuine stubs are still detected; a bare
# "?" matches anywhere (an explicit uncertainty marker).
_DEFINITE_PLACEHOLDER_RE = re.compile(
    r"(?i:^\s*(CITE|CITATION|REF|REF\?|CITE\?|ref\?|cite\?)\s*$)"
    r"|(?i:\b(?:cites?|references?|citations?|refs?|todo|et\s+al)\b|\bref\.?\s*\d|\?)",
)

# DOI pattern for comment scanning — shared bare-DOI body (byte-identical to the
# old local copy; perfect single-source).  See textpatterns.DOI_BARE_RE.
# (Currently unreferenced in this module, but kept for parity / external import.)
_DOI_RE = textpatterns.DOI_BARE_RE

# Author-ish token for comment scanning: capitalised word ≥ 3 chars
_COMMENT_CITE_RE = re.compile(
    r"(?:[A-Z][A-Za-z'\-]{2,}.*\b(?:19|20)\d{2}\b)"     # Author ... year
    r"|(?:10\.\d{4,}/)"                                    # DOI
    r"|(?i:cite|ref(?:erence)?)\b",                        # cite/ref keyword
)


# ===========================================================================
# Internal helpers
# ===========================================================================

def _para_accepted_text(p: etree._Element) -> str:
    """Return accepted-view text for a paragraph element (deletions excluded)."""
    return paragraph_text(p)


def _is_reference_shaped(txt: str) -> bool:
    """Return True if *txt* looks like a bibliographic reference entry.

    Acceptance paths (all require a 4-digit (19|20) year and survive the
    precision guard):
    1. Leading enumerator ([1], (1), 1., 1)) — any year.
    2. Author-led entry > 40 chars whose leading author field is one of:
       * a multi-author list ("Smith AB, Jones CD ..." / "van der Berg H, ..."),
       * a single author + initials + period ("Smith JA. ..."),
       * an organizational / ALLCAPS author + period ("ENIGMA Consortium. ...",
         "World Health Organization. ...").
       This is the lowered bar: capital-initial start, contains a year, > 40
       chars, not a guard prefix — so heading-less single-author, lowercase-
       particle, and organizational reflists no longer fall through to an empty
       inventory.

    Precision guards applied first: figure captions, table notes, and
    "Adapted from" / "Reprinted from" prefixes are excluded.
    """
    stripped = txt.strip()
    if not stripped:
        return False
    # Precision guard — bail early on figure/table/adaptation prefixes
    if _NON_REF_PREFIXES_RE.match(stripped):
        return False
    has_year = bool(_YEAR_RE.search(stripped))
    if not has_year:
        return False
    # Path 1: enumerator-led
    if _ENUM_RE.match(stripped):
        return True
    # Path 2: author-led entry (multi-author list OR single-author / org start),
    # long enough to be bibliographic.
    if len(stripped) > 40 and (
        _AUTHOR_LIST_START_RE.match(stripped)
        or _REF_AUTHOR_START_RE.match(stripped)
    ):
        return True
    return False


def _find_headingless_run(paras, exclude_indices: set) -> Optional[range]:
    """Find the LAST run of ≥ _RUN_MIN_LEN consecutive reference-shaped paragraphs.

    Parameters
    ----------
    paras:
        Full paragraph list (lxml elements).
    exclude_indices:
        Paragraph indices already captured by the heading path; skip them.

    Returns
    -------
    A ``range`` covering the best (last) qualifying run, or ``None``.
    """
    best_run: Optional[range] = None
    run_start: Optional[int] = None
    run_len = 0
    # Enumerator number of the last ACCEPTED entry in the current run, used to
    # bridge a contiguous year-less filler ("#51 gap" fix).  None when the run is
    # author-year style (no enumerators), so a year-less filler is never bridged
    # in an unnumbered run — there is no number to be contiguous with.
    last_accepted_num: Optional[int] = None

    for pi, p in enumerate(paras):
        if pi in exclude_indices:
            # Reset any current run if we hit the heading region
            run_start = None
            run_len = 0
            last_accepted_num = None
            continue

        txt = _para_accepted_text(p).strip()
        if not txt:
            # Blank paragraphs break the run
            if run_len >= _RUN_MIN_LEN and run_start is not None:
                best_run = range(run_start, run_start + run_len)
            run_start = None
            run_len = 0
            last_accepted_num = None
            continue

        # A year-less enumerator-led entry contiguous with the last accepted
        # number is part of the run (in-press paper inside a numbered list); it
        # only bridges WITHIN an already-open run, never starts one.
        is_filler = (
            run_start is not None
            and not _is_reference_shaped(txt)
            and _is_contiguous_filler(txt, last_accepted_num)
        )

        if _is_reference_shaped(txt) or is_filler:
            if run_start is None:
                run_start = pi
            run_len += 1
            n = _enum_number(txt)
            if n is not None:
                last_accepted_num = n
        else:
            # Non-ref paragraph — close current run if long enough
            if run_len >= _RUN_MIN_LEN and run_start is not None:
                best_run = range(run_start, run_start + run_len)
            run_start = None
            run_len = 0
            last_accepted_num = None

    # Handle a run that extends to the end of the document
    if run_len >= _RUN_MIN_LEN and run_start is not None:
        best_run = range(run_start, run_start + run_len)

    return best_run


def _extract_numbering(txt: str) -> Optional[str]:
    """Return the leading numbering token from a reference-list line, or None."""
    m = _NUMBERING_EXTRACT_RE.match(txt)
    if m:
        return m.group(1).strip()
    return None


def _is_numeric_cite(inner: str) -> bool:
    """Return True if *inner* (content between brackets) looks like a citation number."""
    inner = inner.strip()
    if not _NUMERIC_DIGITS_ONLY_RE.match(inner):
        return False
    # Parse all numbers; reject if any individual number exceeds max or is 0
    nums = [int(n) for n in re.split(r"[,\-–]", inner) if n.strip().isdigit()]
    if not nums:
        return False
    if any(n < 1 or n > _MAX_CITE_NUM for n in nums):
        return False
    return True


def _enumerator_run_indices(matches: list, txt: str) -> set:
    """Indices (into *matches*) of single-number PARENTHETICAL markers that form a
    numbered-list / clause enumerator run — e.g.
    ``"(1) severe …, (2) moderate …, and (3) preserved …"`` — which are prose, not
    citations.  Distinguished from GENUINE sequential Vancouver/NIH citations
    (which also surface as ``(1),(2),(3)…`` for the first-cited refs) by
    SENTENCE SCOPE + POSITION rather than by ascending value alone.

    Only single-number parentheticals are candidates: bracketed ``[n]`` and
    multi-number ``(1,2)``/``(3-5)`` are citation lists/ranges, never enumerators.

    A run starts at the first candidate with ``value == 1`` whose marker is
    immediately followed by lowercase prose (a list item).  It extends through
    each subsequent candidate ONLY while ALL hold: the value is the next
    consecutive integer (2, 3, …); the marker is followed by lowercase prose; the
    marker is preceded by a clause/list separator (``:`` ``;`` ``,`` or
    ``and``/``or`` — list position); and no sentence-ending punctuation
    (``.`` ``!`` ``?``) separates it from the previous run marker (single-sentence
    scope).  The value-1 marker need NOT satisfy the boundary condition, so a
    verb-introduced list ("The patient had (1) seizures, (2) …, and (3) …") is
    still caught.  Markers ``2..k`` DO require the boundary.

    Returns the set of run-member ``i`` indices when the run length is >= 2, else
    an empty set — so sequential citations across sentences ("A (1). … B (2). …"),
    claim-trailing citations ("robust (1) and confirmed (2)"), and isolated cites
    ("(5)") are left alone as citations."""
    candidates = [(i, int(m.group(1).strip()), m)
                  for i, m in enumerate(matches)
                  if m.group(0).startswith("(") and m.group(1).strip().isdigit()]

    def _right_is_item(m) -> bool:
        right = txt[m.end():].lstrip()
        return bool(right) and right[0].isalpha() and right[0].islower()

    def _left_is_boundary(m) -> bool:
        left = txt[:m.start()].rstrip()
        return (
            left == ""
            or left[-1] in ":;,"
            or re.search(r"\b(?:and|or)$", left) is not None
        )

    # Find ONE run beginning at the first value-1, lowercase-followed candidate.
    start_pos = None
    for pos, (_, value, m) in enumerate(candidates):
        if value == 1 and _right_is_item(m):
            start_pos = pos
            break
    if start_pos is None:
        return set()

    run = [candidates[start_pos]]            # each entry is (i, value, m)
    expected_next = 2
    prev_m = candidates[start_pos][2]
    for cand in candidates[start_pos + 1:]:
        _, value, m = cand
        if value != expected_next:
            break
        if not _right_is_item(m):
            break
        if not _left_is_boundary(m):
            break
        if re.search(r"[.!?]", txt[prev_m.end():m.start()]):
            break
        run.append(cand)
        expected_next += 1
        prev_m = m

    if len(run) >= 2:
        return {i for i, _, _ in run}
    return set()


def _is_placeholder_bracket(inner: str) -> bool:
    """Return True if bracketed content looks like a citation placeholder."""
    if _DEFINITE_PLACEHOLDER_RE.search(inner):
        return True
    if _PLACEHOLDER_YEAR_RE.search(inner):
        return True
    return False


# ===========================================================================
# Public API
# ===========================================================================

def extract_references(path) -> dict:
    """Inventory every citation-like thing in *path* (.docx).

    Parameters
    ----------
    path:
        Path to a .docx file (str or Path).

    Returns
    -------
    dict with keys:

    ``fields``
        Result of :func:`~zoterocite.citeconvert.classify_citation_sources` —
        the structured field-based citation buckets (zotero/mendeley/endnote/
        word/manual).

    ``reflist``
        ``[{"index": int, "text": str, "numbering": str|None,
           "detected_by": "heading"|"run"}]`` — entries in a References/
        Bibliography section (by heading) or a heading-less run of ≥3
        consecutive reference-shaped paragraphs.

    ``intext``
        ``[{"index": int, "text": str, "kind": "author_year"|"numeric"}]`` —
        ad-hoc in-text citations in body paragraphs (not the reflist region).
        Field-based citations are NOT counted here.

    ``placeholders``
        ``[{"location": str, "text": str, "kind": "bracket"|"comment",
           "context": str}]`` — bracket stubs and comment-embedded references.

    ``counts``
        Summary counts for each bucket.
    """
    path = Path(path)

    # -----------------------------------------------------------------------
    # 1. Field-based citations (reuse existing logic)
    # -----------------------------------------------------------------------
    fields_result = classify_citation_sources(path)

    # -----------------------------------------------------------------------
    # 2. Load document paragraphs
    # -----------------------------------------------------------------------
    doc = Docx(path)
    root = doc.read_tree(DOCUMENT)
    paras = iter_paragraphs(root)

    # -----------------------------------------------------------------------
    # 3. Identify the reflist region
    # -----------------------------------------------------------------------
    # Find the first heading that matches the References pattern; everything
    # after it (until a clearly non-reference heading or end-of-doc) is the
    # reflist region.
    ref_heading_idx: Optional[int] = None
    ref_end_idx: Optional[int] = None  # exclusive; None = end of document

    for pi, p in enumerate(paras):
        txt = _para_accepted_text(p).strip()
        if ref_heading_idx is None:
            if _REF_HEADING_RE.match(txt):
                ref_heading_idx = pi
        else:
            # We're inside the reflist region.  Terminate ONLY on a real next
            # section: a TRUE structural heading (Word Heading pStyle / explicit
            # outline level) or a whitelisted next-section name as a standalone
            # line.  Never end on a content-shaped fragment ('Nature Publishing
            # Group', 'Smith JA', 'In Press') that merely looks heading-ish —
            # those are reference content and must stay in the region.
            if not txt:
                continue
            # The References heading itself never ends its own region.
            if _REF_HEADING_RE.match(txt) is not None:
                continue
            if is_heading_by_pstyle(p) or _NEXT_SECTION_HEADING_RE.match(txt):
                ref_end_idx = pi
                break

    reflist_para_indices: set = set()
    if ref_heading_idx is not None:
        end = ref_end_idx if ref_end_idx is not None else len(paras)
        reflist_para_indices = set(range(ref_heading_idx, end))

    # -----------------------------------------------------------------------
    # 4. Build reflist entries
    # -----------------------------------------------------------------------
    reflist: List[dict] = []
    reflist_counter = 0
    if ref_heading_idx is not None:
        end = ref_end_idx if ref_end_idx is not None else len(paras)
        last_accepted_num: Optional[int] = None
        for pi in range(ref_heading_idx + 1, end):
            txt = _para_accepted_text(paras[pi]).strip()
            if not txt:
                continue  # skip blank lines
            # Re-gate: even inside the References region, only append paragraphs
            # that are actually reference-shaped.  This is the backstop for E5
            # (a non-ref content fragment such as 'In Press.' or a wrapped
            # 'Nature Publishing Group' line is skipped, not captured, while the
            # real numbered/author-led entries around it are kept) AND for the
            # case where termination misses a non-Heading-styled, non-whitelisted
            # next section (e.g. 'Disclosures' / 'Author Information' in an author
            # manuscript) — its prose is NOT reference-shaped, so it can never
            # pollute the inventory even though the region ran on past it.
            #
            # EXCEPTION (the "#51 gap" fix): a year-LESS enumerator-led entry
            # whose number is contiguous with the last accepted entry
            # (in-press / "in preparation" papers with no year, no DOI/PMID) is
            # NOT ambiguous inside a confirmed reference list — admit it so its
            # printed number isn't a phantom hole that breaks in-text resolution.
            if not _is_reference_shaped(txt):
                if not _is_contiguous_filler(txt, last_accepted_num):
                    continue
            numbering = _extract_numbering(txt)
            reflist.append({
                "index": reflist_counter,
                "text": txt,
                "numbering": numbering,
                "detected_by": "heading",
            })
            reflist_counter += 1
            n = _enum_number(txt)
            if n is not None:
                last_accepted_num = n

    # -----------------------------------------------------------------------
    # 4b. Heading-less run detection
    # -----------------------------------------------------------------------
    # Only run if the heading path did not already find a reflist, OR if we
    # want to find additional unlabelled lists. We only add entries for
    # paragraphs NOT already in reflist_para_indices.
    if ref_heading_idx is None:
        run = _find_headingless_run(paras, reflist_para_indices)
        if run is not None:
            for pi in run:
                txt = _para_accepted_text(paras[pi]).strip()
                if not txt:
                    continue
                numbering = _extract_numbering(txt)
                reflist.append({
                    "index": reflist_counter,
                    "text": txt,
                    "numbering": numbering,
                    "detected_by": "run",
                })
                reflist_counter += 1
                reflist_para_indices.add(pi)

    # -----------------------------------------------------------------------
    # 5. Collect field-bearing paragraph indices (to avoid double-counting)
    # -----------------------------------------------------------------------
    # Field items include para_index for manual items, and "location" for
    # complex/fldSimple/sdt. We use a simple heuristic: any para whose
    # accepted text contains a Zotero/field marker is "field-bearing".
    # For in-text filtering, we will skip intext matches that overlap with
    # field-managed citations by checking against the fields_result.
    # However, since field instructions are invisible in accepted text, any
    # (Author, Year) pattern in field-bearing paragraphs is likely genuine
    # in-text. We DON'T filter field paras from intext — we only filter
    # manual references (which are already surfaced in fields_result).
    manual_para_indices: set = set()
    for item in fields_result.get("items", []):
        if item.get("manager") == "manual":
            loc = item.get("location", "")
            m = re.match(r"para (\d+)", loc)
            if m:
                manual_para_indices.add(int(m.group(1)))

    # -----------------------------------------------------------------------
    # 6. Scan body paragraphs for in-text citations
    # -----------------------------------------------------------------------
    intext: List[dict] = []
    intext_counter = 0

    for pi, p in enumerate(paras):
        # Skip the heading paragraph and the reflist region
        if pi in reflist_para_indices:
            continue
        # NOTE: manual_para_indices (body-level hand-typed inline citations) are
        # NOT skipped here. Prior to the GF-5 fix, _detect_manual_refs only found
        # bibliography entries (which legitimately don't need intext scanning). Now
        # it finds body-level parenthetical cites, which ARE intext signals we want
        # to report. Skipping them here was wrong and is removed.

        txt = _para_accepted_text(p)
        if not txt.strip():
            continue

        # Author-year: parenthetical form (Smith et al., 2020)
        for m in _AUTHOR_YEAR_PAREN_RE.finditer(txt):
            intext.append({
                "index": intext_counter,
                "text": m.group(0),
                "kind": "author_year",
            })
            intext_counter += 1

        # Author-year: bare form  Smith (2020)
        for m in _AUTHOR_YEAR_BARE_RE.finditer(txt):
            # Avoid double-counting if the bare form is inside a parenthetical
            # already captured above. Check if the match start is just before
            # a paren group already captured.
            intext.append({
                "index": intext_counter,
                "text": m.group(0),
                "kind": "author_year",
            })
            intext_counter += 1

        # Numeric: [12], [3,4], (1,2) — but EXCLUDE list enumerators "(1) … (2) … (3)"
        # (single-number parentheticals running 1..k); those are prose, not citations.
        # The open/close bracket character class is independent in the pattern, so
        # reject MISMATCHED pairs ("[12)", "(12]") here — a malformed marker would
        # otherwise become a broken anchor that fails to locate at apply time.
        numeric_matches = [m for m in _NUMERIC_CITE_RE.finditer(txt)
                           if _is_numeric_cite(m.group(1))
                           and (m.group(0)[0], m.group(0)[-1]) in (("[", "]"), ("(", ")"))]
        enum_idxs = _enumerator_run_indices(numeric_matches, txt)
        for j, m in enumerate(numeric_matches):
            if j in enum_idxs:
                continue
            intext.append({
                "index": intext_counter,
                "text": m.group(0),
                "kind": "numeric",
            })
            intext_counter += 1

    # -----------------------------------------------------------------------
    # 7. Scan for bracket placeholders in body paragraphs
    # -----------------------------------------------------------------------
    placeholders: List[dict] = []

    # Collect numeric cite texts so we don't flag them as placeholders
    numeric_cite_texts = {e["text"] for e in intext if e["kind"] == "numeric"}

    for pi, p in enumerate(paras):
        if pi in reflist_para_indices:
            continue

        txt = _para_accepted_text(p)
        if not txt.strip():
            continue

        for m in _PLACEHOLDER_BRACKET_RE.finditer(txt):
            bracket_text = m.group(0)   # full "[...]"
            inner = m.group(1)           # content between brackets

            # Skip purely numeric brackets already caught as numeric cites
            if bracket_text in numeric_cite_texts:
                continue
            if _is_numeric_cite(inner):
                continue

            if _is_placeholder_bracket(inner):
                # Context: up to 60 chars before the match
                start = max(0, m.start() - 60)
                context = txt[start:m.end()].strip()
                placeholders.append({
                    "location": f"para {pi}",
                    "text": bracket_text,
                    "kind": "bracket",
                    "context": context,
                })

    # -----------------------------------------------------------------------
    # 8. Scan Word comments for citation-ish content
    # -----------------------------------------------------------------------
    if doc.has("word/comments.xml"):
        comments_root = doc.read_tree("word/comments.xml")
        for comment_el in comments_root.findall(qn("w:comment")):
            cid = comment_el.get(qn("w:id"), "")
            # Extract comment text (all w:t descendants)
            comment_text = "".join(
                t.text or "" for t in comment_el.iter(qn("w:t"))
            )
            if not comment_text.strip():
                continue

            if _COMMENT_CITE_RE.search(comment_text):
                # Try to find the anchor paragraph: look for commentRangeStart
                # with matching id in the document body.
                context = _comment_anchor_context(root, cid)
                placeholders.append({
                    "location": f"comment {cid}",
                    "text": comment_text.strip(),
                    "kind": "comment",
                    "context": context,
                })

    # -----------------------------------------------------------------------
    # 9. Assemble result
    # -----------------------------------------------------------------------
    counts = {
        "reflist": len(reflist),
        "intext": len(intext),
        "placeholders": len(placeholders),
        "fields_zotero": fields_result["counts"].get("zotero", 0),
        "fields_endnote": fields_result["counts"].get("endnote", 0),
        "fields_mendeley": fields_result["counts"].get("mendeley", 0),
        "fields_word": fields_result["counts"].get("word", 0),
        "fields_manual": fields_result["counts"].get("manual", 0),
    }

    return {
        "fields": fields_result,
        "reflist": reflist,
        "intext": intext,
        "placeholders": placeholders,
        "counts": counts,
    }


def _comment_anchor_context(doc_root: etree._Element, comment_id: str) -> str:
    """Return the accepted text of the paragraph anchoring a comment, best-effort."""
    # Find commentRangeStart with matching w:id
    crs_tag = qn("w:commentRangeStart")
    for el in doc_root.iter(crs_tag):
        if el.get(qn("w:id")) == comment_id:
            # Walk up to find the containing paragraph
            anc = el.getparent()
            while anc is not None and anc.tag != qn("w:p"):
                anc = anc.getparent()
            if anc is not None:
                return paragraph_text(anc).strip()[:120]
    return ""
