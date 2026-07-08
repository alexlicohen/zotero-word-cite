"""Shared section / heading locator — the single source of truth for section
and heading detection across zotero-word-cite.

Background
----------
Section/heading detection was independently reimplemented 4-5x across the
codebase (``structure.py``, ``conform.py``, ``journalprofile.py``).  This module
is the *foundational* locator those consumers will migrate to later.  It is
designed to **reproduce the exact union** of every legacy behaviour so a
migration is behaviour-preserving (no result changes).  Nothing here imports
those consumers; they will import *this* module when they migrate.

What it centralises
-------------------
* ``SECTION_CUES`` / ``MANUSCRIPT_SECTION_CUES`` / ``MANUSCRIPT_SECTION_WORD_CUES``
  — the merged cue dictionaries (grant Significance/Innovation/Approach, and the
  IMRaD + Abstract + References manuscript cues, plus a References heading cue).
* ``find_section_idx(paras, section, *, para_elems=None, mode=...)`` — one locator
  with two modes:
    - ``"strict"`` : anchored ``.search`` anywhere in the paragraph
      (subsumes ``structure._find_section_idx``).
    - ``"loose"``  : 60-char-prefix + heading-likeness + word-cue +
      Word-style/outline-level union, using ``para_elems`` when given
      (subsumes ``structure._find_manuscript_section_idx_union`` and
      ``structure._find_section_header_idx``).
* ``section_span`` / ``section_text`` — "text from this heading to the next
  detected section" helpers.
* ``is_heading_by_pstyle(p)`` — ONE copy of the Word pStyle/outlineLvl heading
  predicate (with a ``None`` guard) to replace BOTH ``structure._is_heading_by_style``
  and ``conform._is_heading_by_pstyle``.
* ``get_paragraphs_ooxml`` / ``get_paragraph_elements`` (raw-OOXML path, matching
  ``structure._get_paragraphs`` / ``_get_paragraph_elements``) AND
  ``get_paragraphs_accepted`` (the ``read_views`` accepted-view path matching
  ``journalprofile._paragraphs``).  Both are centralised deliberately; picking a
  winner is a later, verification-gated migration decision and is NOT made here.

Migration map (legacy -> sections.py)
-------------------------------------
* ``structure._SECTION_CUES``                    -> ``SECTION_CUES``
* ``structure._MANUSCRIPT_SECTION_CUES``         -> ``MANUSCRIPT_SECTION_CUES``
* ``structure._MANUSCRIPT_SECTION_WORD_CUES``    -> ``MANUSCRIPT_SECTION_WORD_CUES``
* ``structure._is_likely_heading``               -> ``is_likely_heading``
* ``structure._find_section_idx(paras, pat)``    -> ``find_section_idx(paras, pat, mode="strict")``
* ``structure._find_section_header_idx(paras, pat)``
      -> ``find_section_idx(paras, pat, mode="loose", require_heading_for_prefix=False)``
         (leading-label match wins on m.start()<60 alone; no word-cue/style path)
* ``structure._find_manuscript_section_idx(paras, pat)``
      -> ``find_section_idx(paras, pat, mode="loose")``
         (default require_heading_for_prefix=True: whole-line .match + heading-like 60-char fallback)
* ``structure._find_manuscript_section_idx_union(paras, key, para_elems)``
      -> ``find_section_idx(paras, key, para_elems=para_elems, mode="loose")``
* ``structure._is_heading_by_style``             -> ``is_heading_by_pstyle``
* ``structure._section_text``                    -> ``section_text(paras, idx, to_next_section=False)``
* ``structure._get_paragraphs``                  -> ``get_paragraphs_ooxml``
* ``structure._get_paragraph_elements``          -> ``get_paragraph_elements``
* ``conform._is_heading_by_pstyle``              -> ``is_heading_by_pstyle``
* ``journalprofile._ABSTRACT_HEADING_RE``        -> ``ABSTRACT_HEADING_RE``
* ``journalprofile._REFERENCES_HEADING_RE``      -> ``REFERENCES_HEADING_RE`` (also in MANUSCRIPT_SECTION_CUES["references"])
* ``journalprofile._paragraphs``                 -> ``get_paragraphs_accepted``
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional, Pattern, Sequence, Union

from .docxio import DOCUMENT, Docx
from .ooxml import qn
from .paras import iter_paragraphs, paragraph_text
from .textpatterns import (
    ALLCAPS_TOKEN,
    AUTHOR_INITIALS,
    AUTHOR_PARTICLE,
    CAP_WORD,
    NUMBERED_REF_RE,
    REF_SECTION_LABEL,
)
from .views import read_views

# Heading-prefix window: a cue appearing within this many chars of the
# paragraph start is treated as a label/heading rather than body text.  Matches
# the legacy ``_HEADER_PREFIX_LEN`` in both ``_find_section_header_idx`` and
# ``_find_manuscript_section_idx``.
HEADER_PREFIX_LEN: int = 60

# ---------------------------------------------------------------------------
# Cue dictionaries (merged union of the legacy modules)
# ---------------------------------------------------------------------------

# Grant Research-Strategy section cues (structure._SECTION_CUES).  Each is an
# un-anchored \bword\b pattern intended for strict ``.search`` (the keyword may
# appear anywhere in a heading line such as "A. Significance").
SECTION_CUES: dict = {
    "significance": re.compile(r"\bsignificance\b", re.IGNORECASE),
    "innovation": re.compile(r"\binnovation\b", re.IGNORECASE),
    "approach": re.compile(r"\bapproach\b", re.IGNORECASE),
}

# A standalone Abstract heading (journalprofile._ABSTRACT_HEADING_RE).  Note the
# optional trailing colon, which the manuscript-cue "abstract" (whole-line, no
# colon) does NOT allow — both forms are preserved here.
ABSTRACT_HEADING_RE: Pattern = re.compile(r"^\s*abstract\s*:?\s*$", re.IGNORECASE)

# A standalone References / Bibliography heading.  Union of every legacy copy:
#   * journalprofile._REFERENCES_HEADING_RE  (references / reference list /
#     bibliography / works cited / literature cited)
#   * citeconvert._REF_HEADING_RE            (same set, literal single spaces)
#   * refextract._REF_HEADING_RE             (adds "citations" / "cited literature")
# Folding "citations" and "cited literature" in makes this a strict recall-
# superset of all three (the multi-word synonyms keep ``\s+`` so a double-space
# or tab variant — which the literal-space citeconvert/refextract copies missed —
# also matches).  "References Cited" is additionally folded in (the NIH
# Research-Strategy reference-list heading; absent from all three legacy copies
# yet unambiguously a reference heading) — purely additive recall.  citeconvert
# and refextract import THIS shared pattern.
REFERENCES_HEADING_RE: Pattern = re.compile(
    r"^\s*(references|reference\s+list|references\s+cited|bibliography"
    r"|works\s+cited|literature\s+cited|citations|cited\s+literature)\s*:?\s*$",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Citation-line predicate (consolidated from biosketch._is_citation_line)
# ---------------------------------------------------------------------------
# "Does this paragraph look like a bibliography citation rather than narrative?"
# This was open-coded in ``biosketch`` (separating prose from citation lines in
# OLD-format A/C blocks).  It is the shared heuristic that
# :func:`references_idx` uses (alongside ``NUMBERED_REF_RE``) to recognise a
# heading-less numbered reference list.  ``biosketch`` now imports it back as
# ``_is_citation_line`` (mirroring how ``journalprofile`` re-exports
# ``REFERENCES_HEADING_RE`` as ``_REFERENCES_HEADING_RE``); there is ONE copy.

# A bibliography identifier — PMCID / PMID / DOI in any common surface form.
_CIT_ID_RE: Pattern = re.compile(
    r"PMC\d+|PMID:\s*\d+|doi\.org/|10\.\d{4,9}/", re.IGNORECASE
)

# A WHOLE inline-identifier token (URL / DOI / PMID / PMCID incl. its suffix) —
# used to blank out identifiers before looking for a publication YEAR, so a
# year-shaped run buried inside a DOI ("10.1016/j.neuroimage.2019.5678" -> "2019")
# is NOT mistaken for a bibliographic date.  Unlike ``_CIT_ID_RE`` (which anchors
# only the identifier PREFIX), this consumes the trailing ``\S+`` of the token.
_IDENTIFIER_TOKEN_RE: Pattern = re.compile(
    r"https?://\S+|(?:doi\.org/)?10\.\d{4,9}/\S+|PMC\d+|PMID:\s*\d+",
    re.IGNORECASE,
)

# --- Author-head building blocks (aligned with refextract.py) --------------
# The genuinely-identical fragments (``AUTHOR_PARTICLE`` / ``AUTHOR_INITIALS`` /
# ``REF_SECTION_LABEL`` / ``CAP_WORD`` / ``ALLCAPS_TOKEN``) now live in the shared
# ``textpatterns`` module as the single owner; both ``sections`` and
# ``refextract`` import them so the two author-start recognisers cannot drift.
# refextract owns the richer reference *extraction*; ``sections`` owns the
# body/reference *boundary* predicate.  The COMPILED head patterns stay LOCAL to
# each module because their shapes differ deliberately (here: initials REQUIRED,
# no leading ``\s*``, anchored ``^``).
#
# ``_SURNAME`` is kept LOCAL (not shared): ``sections`` includes the curly
# apostrophe ``’`` ("O’Brien") whereas ``refextract`` does not, and that one
# character is NOT inert — it changes the match decision for a curly-apostrophe
# surname.  Sharing it would silently alter refextract's behaviour, so each
# module keeps its own.  (If you change one, decide deliberately whether the
# other should follow — they diverge on purpose.)
_PARTICLE = AUTHOR_PARTICLE
_SURNAME = r"[A-Z][A-Za-z'’\-]+"  # local: INCLUDES curly apostrophe ’ (see above)
_INITIALS = AUTHOR_INITIALS  # "JA" / "J.A." / "J A" / "J"

# A *multi-author* byline at line start: "Surname AB," / "Surname AB and" /
# "Surname AB & ..." — initials followed by a comma / "and" / "&".  This is the
# ORIGINAL, comma-requiring head; kept (broadened only to allow nobiliary
# particles + "and"/"&") for the multi-author Vancouver/NIH form.  Shape: initials
# REQUIRED, anchored ``^`` with NO leading ``\s*`` (contrast
# ``refextract._AUTHOR_LIST_START_RE``, which makes initials optional and allows a
# leading ``\s*``).
_AUTHOR_HEAD_RE: Pattern = re.compile(
    rf"^{_PARTICLE}{_SURNAME}\s+{_INITIALS}\s*(?:,|and\b|&)"
)

# A *single-author* / organisation byline at line start — the dominant single-
# author Vancouver/NIH form "Surname Init. Title. Journal. Year." has NO comma:
# the author field is terminated by a PERIOD after the initials.  Two shapes:
#   * single author + initials + terminating period:  "Cohen AL." / "van der Berg H."
#   * organisation / consortium author + period:      "ENIGMA Consortium." /
#                                                      "World Health Organization."
# Aligned with ``refextract._REF_AUTHOR_START_RE`` (single-author + org branches),
# INCLUDING its section-label negative-lookahead: a sentence-case 'Label.' lead-in
# common in author manuscripts / PDF->docx ("Methods.", "Data Availability.",
# "Author Contributions.") must never satisfy the organisation-author shape just
# because two capitalised words and a year happen to appear.  Mirrors
# the shared ``REF_SECTION_LABEL`` (the single owner; refextract imports it too).
# (The trailing 4-digit *year* requirement in :func:`is_citation_line`, the
# length floor below, and the >=3-entry run requirement in
# :func:`references_idx` add further precision on top.)
_SECTION_LABEL_RE = REF_SECTION_LABEL
_CAP_WORD = CAP_WORD          # a capitalised word
_ALLCAPS_TOKEN = ALLCAPS_TOKEN  # an acronym / ALLCAPS token (>=2 chars)
_AUTHOR_HEAD_SINGLE_RE: Pattern = re.compile(
    rf"^(?:"
    rf"{_PARTICLE}{_SURNAME}\s+{_INITIALS}\."           # Cohen AL.  /  van der Berg H.
    rf"|(?!{_SECTION_LABEL_RE}[.\s])(?:"                # exclude section labels first
    rf"(?:{_CAP_WORD}\s+){{0,5}}{_ALLCAPS_TOKEN}(?:\s+{_CAP_WORD}){{0,5}}\s*\."  # ENIGMA Consortium.
    rf"|{_CAP_WORD}(?:\s+{_CAP_WORD}){{1,5}}\s*\."      # World Health Organization.
    rf")"
    rf")"
)

# A reference entry is bibliographic, not a stray short capitalised sentence:
# require a minimum length for the comma-less single-author/org path so a short
# prose lead-in ("Cohen AL. Yes.") cannot masquerade as a citation line.  Mirrors
# refextract's ``len(stripped) > 40`` discipline.  (The multi-author and
# identifier paths already carry strong enough structure to skip this floor.)
_MIN_SINGLE_AUTHOR_CIT_LEN: int = 40

# A 4-digit publication year.
_YEAR_RE: Pattern = re.compile(r"\b(19|20)\d{2}\b")


def is_citation_line(p: str) -> bool:
    """True if a paragraph looks like a bibliography citation (not narrative).

    Independent signals (any one is sufficient):

    * it carries a citation identifier (PMCID / PMID / DOI), or
    * it opens with a *multi-author* byline (``Surname AB, ...``) *and* contains a
      4-digit year, or
    * it opens with a *single-author* / organisation byline whose author field is
      terminated by a period (``Cohen AL.`` / ``ENIGMA Consortium.``), is long
      enough to be bibliographic, *and* contains a 4-digit year.

    The author-byline tests are anchored at the start of ``p``: when classifying a
    *numbered* reference entry, strip the leading enumerator first (see
    :func:`references_idx`), so an entry like ``"1.\\tSmith J, Doe A. ... 2020"``
    is tested against ``"Smith J, Doe A. ... 2020"``.

    Precision: a narrative sentence ("Cohen et al. found that ...") is NOT a
    citation line — "et al." is not an initials field, and a comma-less prose
    sentence shorter than the length floor (or lacking a year) is rejected.
    """
    p = p.strip()
    if _CIT_ID_RE.search(p):
        return True
    if not _YEAR_RE.search(p):
        return False
    if _AUTHOR_HEAD_RE.match(p):
        return True
    if len(p) >= _MIN_SINGLE_AUTHOR_CIT_LEN and _AUTHOR_HEAD_SINGLE_RE.match(p):
        return True
    return False


# Strips a leading numbered enumerator ("1.", "1)", "[1]", "(1)") plus the
# trailing whitespace (space or tab) before the entry body, so the anchored
# author-byline test in :func:`is_citation_line` sees the citation proper.  Built
# from the same alternation as the shared ``NUMBERED_REF_RE`` (minus its
# trailing ``\S`` lookahead-of-body) to stay in lock-step with what counts as an
# enumerator.
_REF_ENUMERATOR_RE: Pattern = re.compile(
    r"^\s*(?:\[\s*\d+\s*\]|\(\s*\d+\s*\)|\(?\d+\)?[.)]|\d+[.)])\s+"
)

# Manuscript (journal research-article) whole-line section cues.  Union of
# structure._MANUSCRIPT_SECTION_CUES (IMRaD) plus an "abstract" and "references"
# entry so the dict covers Abstract + IMRaD + References in one place.
#
# Each cue matches a *standalone heading* whose entire (stripped) text is the
# section name or a common synonym / combined form.  A combined "Results and
# Discussion" heading satisfies BOTH "results" and "discussion".
# An OPTIONAL leading decimal section number that real journals prepend to IMRaD
# headings ("2 Methods", "2.1 Methods", "3 Results", "3. Discussion").  Allows a
# dotted hierarchy ("2.1") and one trailing dot ("3."), then REQUIRES at least
# one whitespace before the section word.  Because every cue keeps its trailing
# ``$`` anchor and matches the section word ALONE, a numbered prose line
# ("3 patients were enrolled.") still does NOT match — the body word after the
# number is not a bare section title.  Applied only to the numberable IMRaD body
# cues (introduction / methods / results / discussion) and the combined
# Results-and-Discussion cue; the shared "abstract" and "references" cues are
# left as-is (they are cross-module-shared objects equal to ABSTRACT_HEADING_RE
# / identical to REFERENCES_HEADING_RE, and are not section-numbered in practice).
_SECTION_NUM_PREFIX = r"(?:\d+(?:\.\d+)*\.?\s+)?"

# An OPTIONAL leading qualifier on the methods cue, so combined/qualified methods
# headings match: "Subjects/Materials and Methods", "Subjects and Methods",
# "Patients and Methods", "Participants and Methods", "Online Methods".  The
# qualifier is zero-or-more {subjects|patients|participants|materials|online}
# tokens, each followed by a REQUIRED connector (``/ , &`` optionally spaced, OR
# " and ", OR bare whitespace).  It is OPTIONAL and the cue still REQUIRES a core
# methods word at the end, so a bare "Subjects" (no methods word) does NOT match.
# ("online" is included so the Nature-style "Online Methods" heading matches —
# strictly additive, anchored whole-line.)
#
# Each qualifier unit ends in a MANDATORY connector (never a fully-optional one)
# and the repetition is bounded ``{0,4}``: this is deliberate to keep the regex
# LINEAR-time.  An earlier draft used ``(?:token\s*[/,&]?\s*(?:and\s+)?)*`` whose
# all-optional connectors overlapped with the core ``materials\s+and\s+methods``
# phrase, producing catastrophic exponential backtracking on a long methods-word
# heavy near-miss line (~500 chars -> >2s).  Do NOT reintroduce optional
# connectors here.
_METHODS_QUALIFIER = (
    r"(?:(?:subjects?|patients?|participants?|materials|online)"
    r"(?:\s*[/,&]\s*|\s+and\s+|\s+)){0,4}"
)

MANUSCRIPT_SECTION_CUES: dict = {
    # Each whole-line cue tolerates an OPTIONAL trailing colon (``\s*:?\s*$``):
    # real accepted papers use colon-terminated headings ("Abstract:",
    # "Introduction:", "Methods:") and a colon-terminated standalone heading is
    # unambiguous, so this is a strict recall-widening.  This brings the
    # manuscript cues in line with ``ABSTRACT_HEADING_RE`` (which already
    # tolerated the colon) and ``REFERENCES_HEADING_RE`` (likewise).
    #
    # The numberable body cues additionally tolerate an OPTIONAL leading decimal
    # section number (``_SECTION_NUM_PREFIX``), so journal-numbered headings
    # ("2 Methods", "2.1 Methods", "3 Results", "3. Discussion") match without
    # admitting numbered prose (the ``$`` anchor + section-word-alone keep
    # "3 patients were enrolled." out).
    #
    # The abstract cue ALSO tolerates a RUN-IN heading where the "Abstract:"
    # label and the abstract body share one paragraph ("Abstract: Neuroimaging
    # research depends on ..."), which real accepted papers use.  Two arms:
    #   * run-in:     ``abstract`` + ``:`` + whitespace + at least one body char
    #                 (``:\s+\S``) — the colon and body are MANDATORY here;
    #   * whole-line: the original bare/colon standalone form (``:?\s*$``).
    # The arms are MUTUALLY EXCLUSIVE on the char after the (optional) colon, so
    # the regex is linear-time — no nested optional quantifiers (the ReDoS class).
    #
    # This is the ONLY manuscript cue that gets run-in tolerance, and it is why
    # this entry is DECOUPLED from ``ABSTRACT_HEADING_RE`` (which stays strict
    # whole-line).  Run-in tolerance must NOT spread to the other IMRaD cues: a
    # STRUCTURED abstract's run-in sub-labels ("Methods:", "Results:",
    # "Conclusions:") would otherwise be mis-detected as the paper's actual
    # Methods/Results sections at the wrong location.  And journalprofile reads
    # the abstract BODY from ``paras[abstract_idx + 1:]`` after locating the
    # strict ``ABSTRACT_HEADING_RE`` — a run-in match there would drop the
    # same-line body text — so its pattern is deliberately left strict.
    "abstract": re.compile(r"^\s*abstract\s*(?::\s+\S|:?\s*$)", re.IGNORECASE),
    "introduction": re.compile(
        r"^\s*" + _SECTION_NUM_PREFIX
        + r"(?:introduction|background)\s*:?\s*$",
        re.IGNORECASE,
    ),
    "methods": re.compile(
        r"^\s*" + _SECTION_NUM_PREFIX + _METHODS_QUALIFIER
        + r"(?:materials\s+and\s+methods"
        r"|methods(?:\s+and\s+materials)?"
        r"|experimental\s+procedures)\s*:?\s*$",
        re.IGNORECASE,
    ),
    "results": re.compile(
        r"^\s*" + _SECTION_NUM_PREFIX
        + r"(?:results|results\s+and\s+discussion)\s*:?\s*$",
        re.IGNORECASE,
    ),
    "discussion": re.compile(
        r"^\s*" + _SECTION_NUM_PREFIX
        + r"(?:discussion|results\s+and\s+discussion)\s*:?\s*$",
        re.IGNORECASE,
    ),
    "references": REFERENCES_HEADING_RE,
}

# A combined "Results and Discussion" heading — one heading that doubles as both
# the Results and the Discussion section (structure._RD_COMBINED_CUE).  Tolerates
# the same optional leading decimal section number and trailing colon as the body
# cues above ("5 Results and Discussion", "Results and Discussion:").
RD_COMBINED_CUE: Pattern = re.compile(
    r"^\s*" + _SECTION_NUM_PREFIX + r"results\s+and\s+discussion\s*:?\s*$",
    re.IGNORECASE,
)

# Loose word-level section cues used for STYLE-detected headings only
# (structure._MANUSCRIPT_SECTION_WORD_CUES).  A paragraph already proven a
# heading by Word style/outline level need only *contain* one of these words to
# be credited (e.g. a Heading1 line "Methods and statistical analysis").
MANUSCRIPT_SECTION_WORD_CUES: dict = {
    "abstract": re.compile(r"\babstract\b", re.IGNORECASE),
    "introduction": re.compile(r"\b(introduction|background)\b", re.IGNORECASE),
    "methods": re.compile(
        r"\b(methods|materials\s+and\s+methods|experimental\s+procedures"
        r"|patients\s+and\s+methods)\b",
        re.IGNORECASE,
    ),
    "results": re.compile(r"\bresults\b", re.IGNORECASE),
    "discussion": re.compile(r"\bdiscussion\b", re.IGNORECASE),
    "references": re.compile(
        r"\b(references|bibliography|works\s+cited|literature\s+cited)\b",
        re.IGNORECASE,
    ),
}


# ---------------------------------------------------------------------------
# Heading predicates
# ---------------------------------------------------------------------------

def is_heading_by_pstyle(p) -> bool:
    """True if paragraph element *p* carries a Word Heading/Title pStyle or an
    explicit outline level.

    The single shared copy of the Word *structural* heading predicate.  Replaces
    BOTH ``conform._is_heading_by_pstyle`` and ``structure._is_heading_by_style``
    (which were near-verbatim copies of each other).  Includes the ``None`` guard
    from the ``structure`` copy so it is safe to call on ``p=None`` (conform's
    copy lacked the guard but was never called with ``None``; the guard is a
    strict superset, so it is behaviour-preserving for both callers).
    """
    if p is None:
        return False
    ppr = p.find(qn("w:pPr"))
    if ppr is None:
        return False
    pstyle = ppr.find(qn("w:pStyle"))
    if pstyle is not None:
        val = (pstyle.get(qn("w:val")) or "").lower()
        if val.startswith("heading") or val.startswith("title"):
            return True
    if ppr.find(qn("w:outlineLvl")) is not None:
        return True
    return False


def is_likely_heading(text: str, paras: Optional[List[str]] = None) -> bool:
    """Heuristic: a short line that does not end with a period is likely a heading.

    Mirrors ``structure._is_likely_heading``.  The legacy signature took a
    ``paras`` argument that it never actually used; it is accepted here (optional)
    purely so existing call sites migrate with a verbatim signature.
    """
    t = text.strip()
    if not t:
        return False
    wc = len(t.split())
    return wc <= 8 and not t.endswith(".")


# ---------------------------------------------------------------------------
# Decimal section numbering (hierarchy-aware section spans)
# ---------------------------------------------------------------------------

# Capture the numeric prefix and whether a heading delimiter (``.`` / ``)``)
# followed it.  ``group(2)`` is the delimiter (empty if the number was bare).
_HEADING_NUM_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)([.)]?)(?:\s|$)")
# A lone leading integer is only a section number if it is delimited ("2." /
# "2)") OR small enough to plausibly be a section index.  Without this, a year
# ("2024 was a good year") parsed as section (2024,).  Multi-component numbers
# ("2.4", "3.2") are always treated as section numbers.
_MAX_BARE_SECTION_NUM = 99
# Figure/table caption lines that look heading-like ("Table 1. ...", "Fig. 2",
# "Supplementary Figure S1") but are NOT section headings and must not bound a
# section.  Requires a following number so a real "Figures" section heading is
# not swallowed.
_CAPTION_RE = re.compile(
    r"^\s*(?:supp(?:lementary|l|\.)?\s+)?(?:table|fig(?:ure|\.|s)?|scheme|appendix)\s+s?\d",
    re.IGNORECASE,
)


def heading_number(text: Optional[str]) -> Optional[tuple]:
    """Leading decimal section number as an int tuple, or ``None``.

    ``"2. Methods"`` -> ``(2,)`` · ``"2.4 Clustering"`` -> ``(2, 4)`` ·
    ``"3.2 Results"`` -> ``(3, 2)`` · ``"Results"`` -> ``None``.  Lets callers
    treat ``2.4`` as a subsection of section ``2`` (same first component) rather
    than as a sibling that ends section ``2``.

    A bare leading integer with no delimiter is only accepted when it is small
    enough to be a plausible section index (``<= 99``); this rejects a year such
    as ``"2024 was a good year"`` while still accepting ``"2."``/``"2)"`` and any
    decimal heading.
    """
    if not text:
        return None
    m = _HEADING_NUM_RE.match(text)
    if not m:
        return None
    components = tuple(int(x) for x in m.group(1).split("."))
    delimiter = m.group(2)
    if len(components) == 1 and not delimiter and components[0] > _MAX_BARE_SECTION_NUM:
        return None
    return components


def is_caption_line(text: Optional[str]) -> bool:
    """True for a figure/table caption masquerading as a heading."""
    return bool(_CAPTION_RE.match(text or ""))


# ---------------------------------------------------------------------------
# The unified section locator
# ---------------------------------------------------------------------------

def _resolve_pattern(section: Union[str, Pattern], *, loose: bool) -> Pattern:
    """Accept either a compiled pattern or a section *key*.

    A string key resolves against ``MANUSCRIPT_SECTION_CUES`` first, then
    ``SECTION_CUES`` (grant), so callers may pass "methods" or "significance"
    interchangeably with a precompiled pattern.
    """
    if isinstance(section, str) and not isinstance(section, Pattern):
        if section in MANUSCRIPT_SECTION_CUES:
            return MANUSCRIPT_SECTION_CUES[section]
        if section in SECTION_CUES:
            return SECTION_CUES[section]
        raise KeyError(f"unknown section key {section!r}")
    return section  # already a compiled pattern


def _word_cue_for(section: Union[str, Pattern]) -> Optional[Pattern]:
    """The loose word-cue for a section *key* (or None for a raw pattern)."""
    if isinstance(section, str) and not isinstance(section, Pattern):
        return MANUSCRIPT_SECTION_WORD_CUES.get(section)
    return None


def find_section_idx(
    paras: Sequence[str],
    section: Union[str, Pattern],
    *,
    para_elems: Optional[Sequence] = None,
    mode: str = "strict",
    require_heading_for_prefix: bool = True,
) -> Optional[int]:
    """Return the index of the paragraph that begins *section*, or ``None``.

    ``section`` may be a compiled :class:`re.Pattern` or a section *key*
    ("significance"/"methods"/...).  A key resolves to the appropriate cue.

    mode="strict"
        Anchored ``.search`` anywhere in the paragraph.  Reproduces
        ``structure._find_section_idx``: the first paragraph for which the
        pattern matches *anywhere* wins.  (With a grant ``\\bword\\b`` cue this
        is the legacy Significance/Innovation/Approach behaviour; with a
        whole-line manuscript cue ``^...$`` the ``.search`` is effectively a
        whole-line match.)

    mode="loose"
        The union locator.  A paragraph qualifies (earliest wins) when ANY of:
          (a) the cue matches the stripped line as a whole-line ``.match``
              (manuscript whole-line cue);
          (b) the cue ``.search`` hits within the first ``HEADER_PREFIX_LEN``
              chars (a leading label such as "A. Significance" /
              "Significance: ..."); whether this *also* requires the paragraph
              to be heading-like is controlled by ``require_heading_for_prefix``;
          (c) the cue ``.search`` hits *anywhere* in a paragraph that is
              heading-like by :func:`is_likely_heading`; OR
          (d) ``para_elems`` is given and the paragraph *is* a heading by Word
              style / outline level (:func:`is_heading_by_pstyle`) and its text
              contains the looser word-cue (``MANUSCRIPT_SECTION_WORD_CUES``).
        The minimum index found by the text path (a-c) and the style path (d) is
        returned, exactly as the legacy union did.

        ``require_heading_for_prefix`` selects between the two legacy loose
        variants, which differ ONLY in branch (b):

          * ``True`` (default) reproduces ``structure._find_manuscript_section_idx``
            (and the union ``_find_manuscript_section_idx_union``): a leading-label
            match must ALSO be heading-like (``m.start() < 60 AND is_likely_heading``).

          * ``False`` reproduces ``structure._find_section_header_idx`` (grant
            C-checks): a leading-label match wins on ``m.start() < 60`` ALONE
            (a body paragraph that merely *starts* with the keyword qualifies).

        Branches (a), (c), (d) are identical across both variants.
    """
    pattern = _resolve_pattern(section, loose=(mode == "loose"))

    if mode == "strict":
        for i, p in enumerate(paras):
            if pattern.search(p):
                return i
        return None

    if mode != "loose":
        raise ValueError(f"mode must be 'strict' or 'loose', got {mode!r}")

    # --- loose: text path (a)-(c) ---
    text_idx: Optional[int] = None
    for i, p in enumerate(paras):
        stripped = p.strip()
        # (a) whole-line match on the stripped text
        if pattern.match(stripped):
            text_idx = i
            break
        m = pattern.search(p)
        if m is None:
            continue
        # (b) leading label: cue early in the paragraph.  In the header variant
        # (require_heading_for_prefix=False) this alone qualifies; in the
        # manuscript variant it must also be heading-like.
        if m.start() < HEADER_PREFIX_LEN:
            if not require_heading_for_prefix or is_likely_heading(p):
                text_idx = i
                break
        # (c) heading-like paragraph, cue anywhere
        if is_likely_heading(p):
            text_idx = i
            break

    # --- loose: Word-style path (d) ---
    style_idx: Optional[int] = None
    if para_elems is not None:
        word_cue = _word_cue_for(section)
        if word_cue is not None:
            for i, pe in enumerate(para_elems):
                if i >= len(paras):
                    break
                if is_heading_by_pstyle(pe) and word_cue.search(paras[i]):
                    style_idx = i
                    break

    candidates = [i for i in (text_idx, style_idx) if i is not None]
    return min(candidates) if candidates else None


# ---------------------------------------------------------------------------
# Section span / text extraction
# ---------------------------------------------------------------------------

def section_span(
    paras: Sequence[str],
    start_idx: int,
    *,
    section_indices: Optional[Sequence[int]] = None,
) -> tuple[int, int]:
    """Return the ``(begin, end)`` paragraph-index half-open span of a section.

    ``begin`` is ``start_idx + 1`` (the body after the heading).  ``end`` is:
      * the next index in ``section_indices`` greater than ``start_idx`` (the
        next *detected* section heading), when ``section_indices`` is supplied;
      * otherwise ``len(paras)`` (to the end of the document).

    ``section_indices`` need not be sorted.  Indices <= ``start_idx`` are
    ignored.  The returned span is always within ``[0, len(paras)]``.
    """
    n = len(paras)
    begin = min(start_idx + 1, n)
    end = n
    if section_indices:
        later = [j for j in section_indices if j > start_idx]
        if later:
            end = min(later)
    if end < begin:
        end = begin
    return begin, end


def section_text(
    paras: Sequence[str],
    start_idx: int,
    *,
    section_indices: Optional[Sequence[int]] = None,
    to_next_section: bool = True,
) -> str:
    """Text from the heading at ``start_idx`` to the next section.

    Two behaviours:

    * ``to_next_section=True`` (default): join every paragraph from
      ``start_idx + 1`` up to (but excluding) the next detected section heading
      (see :func:`section_span`).  Empty paragraphs are dropped; result is
      newline-joined.

    * ``to_next_section=False``: reproduce the legacy
      ``structure._section_text`` — collect non-blank lines starting after the
      heading and STOP at the first blank line that follows at least one
      collected line (i.e. the first paragraph block only).  ``section_indices``
      is ignored in this mode.
    """
    if not to_next_section:
        lines: List[str] = []
        for p in paras[start_idx + 1:]:
            if not p.strip():
                if lines:
                    break
                continue
            lines.append(p)
        return "\n".join(lines)

    begin, end = section_span(paras, start_idx, section_indices=section_indices)
    return "\n".join(p for p in paras[begin:end] if p.strip())


# ---------------------------------------------------------------------------
# Unified paragraph accessors
# ---------------------------------------------------------------------------
#
# IMPORTANT: two distinct accepted-text pipelines are centralised here.  Since
# the ``iter_paragraphs`` fix (commit 331cdb6) they agree on accepted-text
# *content* — both exclude tracked deletions, keep insertions, and render
# ``<w:br>``/``<w:cr>`` as a newline and ``<w:tab>`` as a tab.  They differ ONLY
# in **granularity** (per-``<w:p>`` vs render-then-split-on-newline), so they are
# still not freely interchangeable; choosing a single winner is a deferred,
# verification-gated migration decision.  This module exposes BOTH, clearly
# named, and does not pick:
#
#   * get_paragraphs_ooxml / get_paragraph_elements  — raw OOXML path.  Parses
#     word/document.xml directly and uses paragraph_text() (which excludes
#     tracked deletions).  Matches structure._get_paragraphs /
#     _get_paragraph_elements byte-for-byte.  One entry per ``<w:p>`` — the only
#     path that stays index-aligned with get_paragraph_elements.
#
#   * get_paragraphs_accepted — the read_views() accepted-view path.  Matches
#     journalprofile._paragraphs (renders the accepted view, then splits on
#     newlines; never raises).  Same content as the raw-OOXML path, but a block
#     pasted as one ``<w:p>`` with embedded line breaks splits into its
#     constituent lines here — a finer granularity that a render-then-split
#     cannot keep index-aligned with the per-``<w:p>`` element list.

def get_paragraphs_ooxml(path: Union[str, Path]) -> List[str]:
    """Paragraph texts via the raw-OOXML path (== ``structure._get_paragraphs``).

    Parses ``word/document.xml`` and returns ``paragraph_text(p)`` for each
    ``w:p`` in document order (accepted view: tracked deletions excluded).
    """
    doc = Docx(path)
    root = doc.read_tree(DOCUMENT)
    return [paragraph_text(p) for p in iter_paragraphs(root)]


def get_paragraph_elements(path: Union[str, Path]) -> List:
    """Raw lxml ``w:p`` elements via the OOXML path
    (== ``structure._get_paragraph_elements``).  Parallel/aligned with
    :func:`get_paragraphs_ooxml`.
    """
    doc = Docx(path)
    root = doc.read_tree(DOCUMENT)
    return list(iter_paragraphs(root))


def get_paragraphs_accepted(path: Union[str, Path]) -> List[str]:
    """Accepted-view paragraph *lines* (== ``journalprofile._paragraphs``).

    Uses the ``read_views`` accepted view and splits on newlines. This is
    DELIBERATELY NOT the same as :func:`get_paragraphs_ooxml`, and the two are
    not interchangeable — verified empirically:

    * Both yield the **accepted** text (tracked deletions excluded, insertions
      kept), and both now render an intra-paragraph line break (``<w:br>``/
      ``<w:cr>``) as a newline and a ``<w:tab>`` as a tab, so the *content*
      agrees.
    * They still differ in **granularity**: ``read_views`` is rendered then
      split on newlines, so a block pasted as one paragraph with line breaks
      (e.g. a copy-pasted reference list) splits into its constituent lines
      here, whereas :func:`get_paragraphs_ooxml` keeps it as one ``<w:p>`` entry
      (the embedded ``\n`` intact). Only the per-``w:p`` path stays index-aligned
      with :func:`get_paragraph_elements`; a render-then-split cannot.

    Use THIS for line-oriented counting (journalprofile's reference/entry
    counts); use :func:`get_paragraphs_ooxml` / :func:`get_paragraph_elements`
    for per-paragraph, style/outline-aware structural checks (they stay index-
    aligned, which a render-then-split cannot guarantee). Never raises: on any
    read error returns ``[]`` so the caller degrades to silence.
    """
    try:
        accepted = read_views(str(path)).get("accepted", "")
    except Exception:
        return []
    return [ln for ln in accepted.split("\n")]


# ---------------------------------------------------------------------------
# Body / reference-list boundary
# ---------------------------------------------------------------------------

# Minimum length of a sustained numbered-citation run that, in the ABSENCE of an
# explicit References heading, marks the start of the reference list.  A run of
# fewer than this many consecutive numbered citation-like paragraphs is treated
# as body text (e.g. a short numbered procedure list "1) First  2) Second"),
# NOT as the bibliography.  Three is the smallest run that reliably excludes
# such short in-body enumerations while still catching real (always longer)
# reference lists.
MIN_HEADINGLESS_REF_RUN: int = 3


def _looks_like_numbered_citation(p: str) -> bool:
    """A paragraph that is a numbered enumerator AND a citation entry.

    Requires BOTH (a) a leading numbered enumerator (shared ``NUMBERED_REF_RE``,
    which already tolerates the ``"1.\\tAuthor"`` tab form) AND (b) citation-like
    content once the enumerator is stripped (shared :func:`is_citation_line`).
    The strip is essential: ``is_citation_line``'s author-byline test is anchored
    at the start of the string, so it must see ``"Smith J, ... 2020"`` rather
    than ``"1.\\tSmith J, ... 2020"``.
    """
    if not NUMBERED_REF_RE.match(p):
        return False
    body = _REF_ENUMERATOR_RE.sub("", p, count=1)
    return is_citation_line(body)


def _has_reference_signal(text: str) -> bool:
    """True when *text* carries a genuine bibliographic signal beyond a bare inline
    identifier: an author-BYLINE shape at the entry start, or a real publication
    YEAR that is not merely a year-shaped run buried inside an inline identifier.

    A paragraph whose ONLY citation signal is an inline DOI/PMID (body prose citing
    a URL — no byline, no bibliographic date) has NEITHER, so it must not, on its
    own, mark the start of a heading-less reference list.  The year check blanks out
    whole identifier tokens (:data:`_IDENTIFIER_TOKEN_RE`) first, so a DOI such as
    ``10.1016/j.neuroimage.2019.5678`` does not contribute a false "2019".
    """
    s = text.strip()
    if _AUTHOR_HEAD_RE.match(s) or _AUTHOR_HEAD_SINGLE_RE.match(s):
        return True
    return bool(_YEAR_RE.search(_IDENTIFIER_TOKEN_RE.sub(" ", s)))


def _looks_like_reference_entry(p: str) -> bool:
    """A paragraph that looks like a heading-less reference-list entry.

    Either form qualifies:

    * a *numbered* citation entry (:func:`_looks_like_numbered_citation`), or
    * a *bare* (unnumbered) citation line (:func:`is_citation_line`) — the common
      NIH/Vancouver single-author or organisation form
      ``"Cohen AL. Title. Journal. Year"`` that carries no enumerator and no
      inline PMID/DOI.

    In BOTH forms the entry must ALSO carry a real bibliographic signal — an author
    byline and/or a publication year (:func:`_has_reference_signal`) — not merely an
    inline identifier.  Without this, three consecutive BODY paragraphs that each
    just *mention* an inline DOI/PMID (``is_citation_line`` returns True on the bare
    identifier alone) were misdetected as the reference section, under-counting the
    body page-span / A4 scan (a fail-open).  The byline/year requirement excludes
    that prose while keeping a genuine authors-plus-year bibliography.

    The per-line signal is still deliberately permissive beyond that; the further
    precision that keeps a short in-body enumeration (or a stray reference-shaped
    sentence) from being read as the bibliography comes from the >=
    :data:`MIN_HEADINGLESS_REF_RUN` *consecutive* run requirement in
    :func:`references_idx`.
    """
    if _looks_like_numbered_citation(p):
        # Test the signal on the entry BODY (after the enumerator) so a leading
        # "1." does not hide the byline.
        return _has_reference_signal(_REF_ENUMERATOR_RE.sub("", p, count=1))
    return is_citation_line(p) and _has_reference_signal(p)


def references_idx(paras: Sequence[str]) -> Optional[int]:
    """Index of the first paragraph belonging to the reference list, or None.

    Two-stage detection (see the module/task contract):

    1. **Explicit heading** — a standalone References / Bibliography / Literature
       Cited / Works Cited heading (shared :data:`REFERENCES_HEADING_RE` via
       :func:`find_section_idx`, ``mode="strict"``, exactly as
       ``journalprofile._references_idx`` does).  The reference section begins at
       that heading's index.
    2. **Heading-less reference list** — the start of a SUSTAINED run of
       >= :data:`MIN_HEADINGLESS_REF_RUN` consecutive paragraphs that each look
       like a reference entry (:func:`_looks_like_reference_entry`).  This covers
       BOTH the numbered NIH Research-Strategy list
       (``"1.\\tAuthor, Title. Journal. Year"``) AND a bare, *un*numbered
       single-author / organisation Vancouver list
       (``"Cohen AL. Title. Journal. Year"``) that carries no enumerator and no
       inline PMID/DOI.  The reference section begins at the first paragraph of
       that run.  The run requirement prevents a short in-body list
       ("1) First step 2) Second step 3) Third") — whose entries are not
       reference-shaped — from being misread as the bibliography.

    The explicit heading wins when both are present.
    """
    # 1. Explicit heading.
    idx = find_section_idx(paras, REFERENCES_HEADING_RE, mode="strict")
    if idx is not None:
        return idx

    # 2. Heading-less sustained reference run (numbered OR bare entries).
    run_start: Optional[int] = None
    run_len = 0
    for i, p in enumerate(paras):
        if _looks_like_reference_entry(p):
            if run_start is None:
                run_start = i
            run_len += 1
            if run_len >= MIN_HEADINGLESS_REF_RUN:
                return run_start
        else:
            run_start = None
            run_len = 0
    return None


def split_body_vs_references(
    paras: Sequence[str],
) -> tuple[list[str], list[str]]:
    """Split paragraphs into ``(body, references)``.

    ``references`` is the empty list when no reference section is detected, in
    which case ``body`` is every input paragraph.  Detection uses
    :func:`references_idx`.
    """
    idx = references_idx(paras)
    if idx is None:
        return list(paras), []
    return list(paras[:idx]), list(paras[idx:])
