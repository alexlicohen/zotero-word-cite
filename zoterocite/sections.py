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
# also matches).  citeconvert and refextract import THIS shared pattern.
REFERENCES_HEADING_RE: Pattern = re.compile(
    r"^\s*(references|reference\s+list|bibliography|works\s+cited"
    r"|literature\s+cited|citations|cited\s+literature)\s*:?\s*$",
    re.IGNORECASE,
)

# Manuscript (journal research-article) whole-line section cues.  Union of
# structure._MANUSCRIPT_SECTION_CUES (IMRaD) plus an "abstract" and "references"
# entry so the dict covers Abstract + IMRaD + References in one place.
#
# Each cue matches a *standalone heading* whose entire (stripped) text is the
# section name or a common synonym / combined form.  A combined "Results and
# Discussion" heading satisfies BOTH "results" and "discussion".
MANUSCRIPT_SECTION_CUES: dict = {
    # NOTE: structure's legacy abstract cue is whole-line *without* a trailing
    # colon (``^\s*abstract\s*$``); journalprofile's ``ABSTRACT_HEADING_RE``
    # tolerates a trailing colon.  These differ on "Abstract:" — to keep the
    # structure-consumer migration behaviour-preserving, the manuscript dict
    # keeps the structure form here.  The colon-tolerant variant remains exposed
    # separately as ``ABSTRACT_HEADING_RE`` (journalprofile's cue).
    "abstract": re.compile(r"^\s*abstract\s*$", re.IGNORECASE),
    "introduction": re.compile(r"^\s*(introduction|background)\s*$", re.IGNORECASE),
    "methods": re.compile(
        r"^\s*(methods"
        r"|materials\s+and\s+methods"
        r"|experimental\s+procedures"
        r"|patients\s+and\s+methods)\s*$",
        re.IGNORECASE,
    ),
    "results": re.compile(
        r"^\s*(results|results\s+and\s+discussion)\s*$", re.IGNORECASE
    ),
    "discussion": re.compile(
        r"^\s*(discussion|results\s+and\s+discussion)\s*$", re.IGNORECASE
    ),
    "references": REFERENCES_HEADING_RE,
}

# A combined "Results and Discussion" heading — one heading that doubles as both
# the Results and the Discussion section (structure._RD_COMBINED_CUE).
RD_COMBINED_CUE: Pattern = re.compile(
    r"^\s*results\s+and\s+discussion\s*$", re.IGNORECASE
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
# IMPORTANT: two distinct accepted-text pipelines are centralised here.  They
# are NOT interchangeable today and choosing a single winner is a deferred,
# verification-gated migration decision — this module exposes BOTH, clearly
# named, and does not pick:
#
#   * get_paragraphs_ooxml / get_paragraph_elements  — raw OOXML path.  Parses
#     word/document.xml directly and uses paragraph_text() (which excludes
#     tracked deletions).  Matches structure._get_paragraphs /
#     _get_paragraph_elements byte-for-byte.
#
#   * get_paragraphs_accepted — the read_views() accepted-view path.  Matches
#     journalprofile._paragraphs (splits the accepted view on newlines, never
#     raises).  This view differs from the raw-OOXML path in how it segments and
#     renders the accepted text, so the two can yield different paragraph lists.

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
