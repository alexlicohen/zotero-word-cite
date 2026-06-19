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

# A surname token: a capital-initial word, optionally preceded by a lowercase
# nobiliary particle (van / von / de / der / del / di / da / la / le / dos /
# bin / al / mac / mc ...), e.g. "van der Berg", "de la Cruz".  Used as the
# leading-author building block for both the multi-author list pattern and the
# single-author bibliographic-entry pattern.
_PARTICLE = r"(?:(?:van|von|de|der|den|del|della|di|da|das|dos|du|la|le|lo|el|al|bin|ibn|mac|mc|st|ter|ten|ten)\s+){0,3}"
_SURNAME = r"[A-Z][A-Za-z'\-]+"
_INITIALS = r"[A-Z](?:\.?\s*[A-Z]){0,3}\.?"  # "JA" / "J.A." / "J A" / "J"

# Author-year list pattern: starts with "Surname AB," or "Surname AB and"
# Captures multi-author formats like "Smith AB, Jones CD, ..." / "Smith A, Jones
# B." / "van der Berg H, ...".  Lowercase nobiliary particles are now permitted
# in the leading surname so "van der Berg H, ..." is recognised.
_AUTHOR_LIST_START_RE = re.compile(
    rf"^\s*{_PARTICLE}{_SURNAME}(?:\s+{_INITIALS})?(?:\s*,|\s+and\b|\s+&)"
)

# Section labels that are sentence-case prose, NOT author fields.  An author
# manuscript / PDF→docx commonly opens a section with a 'Label.' lead-in
# ("Methods. Patients were recruited ...", "Data Availability. Data generated
# in 2022 ...").  The old organizational branch matched any "Capitalized words."
# lead-in, so such prose was wrongly classed as reference-shaped whenever a year
# happened to appear later in the line.  This negative-lookahead set excludes the
# common labels so they can never satisfy the organizational author shape.
_SECTION_LABEL_RE = (
    r"(?:Methods|Results|Background|Conclusions?|Discussion|Funding|Introduction"
    r"|Materials|Acknowledge?ments?|Data\s+Availability|Abstract"
    r"|Significance|Summary|Limitations?|Objectives?|Aims?|Hypothes[ei]s"
    # Post-reference sections that frequently open with a 'Label.' lead-in in
    # author manuscripts / PDF→docx and must never read as an author field.
    r"|Disclosures?|Author\s+Information|Author\s+Contributions?"
    r"|Additional\s+Information|Supporting\s+Information"
    r"|Competing\s+(?:Financial\s+)?Interests?|Conflicts?\s+of\s+Interest"
    r"|Online\s+Methods|Extended\s+Data|Correspondence|Footnotes?|Endnotes?)"
)

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
_CAP_WORD = r"[A-Z][A-Za-z&'\-]*"      # a capitalized word
_ALLCAPS_TOKEN = r"[A-Z][A-Z&'\-]+"    # an acronym / ALLCAPS token (≥2 chars)
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
_PLACEHOLDER_CONTENT_RE = re.compile(
    r"(?i)(cite|ref|citation|TODO|\?|et\s+al|10\.\d{4,}/)",  # citation-ish content
)
_PLACEHOLDER_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_PLACEHOLDER_BRACKET_RE = re.compile(r"\[([^\]]{1,80})\]")

# Patterns that identify definitely-citation brackets (override other filters)
_DEFINITE_PLACEHOLDER_RE = re.compile(
    r"(?i:^\s*(CITE|CITATION|REF|REF\?|CITE\?|ref\?|cite\?)\s*$)"
    r"|(?i:cite|ref|citation|todo|\?|et\s+al)",
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

    for pi, p in enumerate(paras):
        if pi in exclude_indices:
            # Reset any current run if we hit the heading region
            run_start = None
            run_len = 0
            continue

        txt = _para_accepted_text(p).strip()
        if not txt:
            # Blank paragraphs break the run
            if run_len >= _RUN_MIN_LEN and run_start is not None:
                best_run = range(run_start, run_start + run_len)
            run_start = None
            run_len = 0
            continue

        if _is_reference_shaped(txt):
            if run_start is None:
                run_start = pi
            run_len += 1
        else:
            # Non-ref paragraph — close current run if long enough
            if run_len >= _RUN_MIN_LEN and run_start is not None:
                best_run = range(run_start, run_start + run_len)
            run_start = None
            run_len = 0

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
            if not _is_reference_shaped(txt):
                continue
            numbering = _extract_numbering(txt)
            reflist.append({
                "index": reflist_counter,
                "text": txt,
                "numbering": numbering,
                "detected_by": "heading",
            })
            reflist_counter += 1

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
        # Skip paragraphs already classified as manual references (they are in fields)
        if pi in manual_para_indices:
            continue

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

        # Numeric: [12], [3,4], [5-7]
        for m in _NUMERIC_CITE_RE.finditer(txt):
            inner = m.group(1)
            if _is_numeric_cite(inner):
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
