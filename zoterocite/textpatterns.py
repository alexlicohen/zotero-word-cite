"""Shared compiled regex constants for zotero-word-cite text analysis.

These are the canonical superset patterns consolidated from structure.py and
claimcheck.py.  All patterns are strictly recall-increasing: they match every
string matched by the legacy constants they replace.

Legacy mapping
--------------
PVALUE_RE        ← structure._PVALUE_RE  +  claimcheck._PVALUE_RE
EFFECT_SIZE_RE   ← structure._EFFECT_SIZE_RE  +  claimcheck._RATIO_RE  (union)
DISPERSION_RE    ← structure._M8_ERROR_DEF_RE  (SD/SEM/CI/confidence interval)
FIG_LABEL_RE          ← structure._FIG_LABEL_RE
TABLE_LABEL_RE        ← structure._TABLE_LABEL_RE
FIG_REF_RE            ← structure._FIG_REF_RE  +  claimcheck._FIG_TABLE_RE  (union)
TABLE_REF_RE          ← structure._TABLE_REF_RE  +  claimcheck._FIG_TABLE_RE  (union)
FIG_LEGEND_LINE_RE    ← structure._FIG_LEGEND_LINE_RE
TABLE_LEGEND_LINE_RE  ← structure._TABLE_LEGEND_LINE_RE
SUPPL_FIG_REF_RE         ← structure._SUPPL_FIG_REF_RE
SUPPL_TABLE_REF_RE       ← structure._SUPPL_TABLE_REF_RE
SUPPL_FIG_LEGEND_LINE_RE ← structure._SUPPL_FIG_LEGEND_LINE_RE
SUPPL_TABLE_LEGEND_LINE_RE ← structure._SUPPL_TABLE_LEGEND_LINE_RE
SIG_DEF_RE       ← structure._M8_SIG_DEF_RE
"""

import re

# ---------------------------------------------------------------------------
# p-value
# ---------------------------------------------------------------------------
# Union of:
#   structure: r"\bp\s*[<>=≤≥]\s*0?\.\d+|\bp[-–]\s*value"  (≤≥ and en-dash)
#   claimcheck: r"\bp\s*[<>=]\s*0?\.\d+|\bp[\s-]*values?\b"  (plural, \s-)
# Superset keeps ≤≥, en-dash, leading-dot (.05), plural "values", and \s-
PVALUE_RE = re.compile(
    r"\bp\s*[<>=≤≥]\s*0?\.\d+"   # p < 0.05 / p ≤ .05 / p = .03
    r"|\bp[-–\s]*values?\b",      # p-value, p – value, p value, p-values
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Effect size / risk ratios
# ---------------------------------------------------------------------------
# Union of:
#   structure._EFFECT_SIZE_RE: 95% CI, effect size, Cohen, OR=, HR=, β=, r=,
#                               eta squared, hedges, confidence interval
#   claimcheck._RATIO_RE: OR/HR/RR (=|of|:), odds/hazard/risk ratio, 95% CI,
#                         confidence interval
# Superset adds RR and the prose ratio forms (OR of / OR: / odds ratio etc.).
EFFECT_SIZE_RE = re.compile(
    r"\b95\s*%\s*CI\b"
    r"|\beffect\s+size\b"
    r"|\bCohen\b"
    r"|\b(?:OR|HR|RR)\b\s*(?:=|\bof\b|:)\s*\d"   # OR = 1.8, OR of 2, HR:
    r"|\bOR\s*="                                    # kept for bare "OR =" without digit
    r"|\bHR\s*="
    r"|\bβ\s*="
    r"|\br\s*=\s*[-.\d]"
    r"|\beta\s+squared\b"
    r"|\bhedges\b"
    r"|\b(?:odds|hazard|risk)\s+ratio\b"
    r"|\bconfidence\s+interval\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Dispersion / error-bar definitions (from structure._M8_ERROR_DEF_RE)
# ---------------------------------------------------------------------------
DISPERSION_RE = re.compile(
    r"\bSD\b"
    r"|\bstandard\s+deviation\b"
    r"|\bSEM\b"
    r"|\bstandard\s+error\b"
    r"|\bCI\b"
    r"|\bconfidence\s+interval\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Statistical-significance definition (from structure._M8_SIG_DEF_RE)
# ---------------------------------------------------------------------------
SIG_DEF_RE = re.compile(
    r"\*\s*p"
    r"|\bp\s*[<>=≤≥]"
    r"|\bp\s*-\s*value"
    r"|\bsignifican",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Figure / table labels (start-of-caption form)
# ---------------------------------------------------------------------------
# From structure._FIG_LABEL_RE / _TABLE_LABEL_RE (most complete).
FIG_LABEL_RE = re.compile(
    r"\b(?:Figure|Fig\.?)\s+(\d+)\b(?:\.|\s|$)",
    re.IGNORECASE,
)
TABLE_LABEL_RE = re.compile(
    r"\bTable\s+(\d+)\b(?:\.|\s|$)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Figure / table in-text references
# ---------------------------------------------------------------------------
# Union of structure._FIG_REF_RE (main only, no S\d) and claimcheck._FIG_TABLE_RE
# (which also matches "Figs." / "Figures" plural and "Tables" plural).
# We reproduce both capabilities in one pattern per type.
FIG_REF_RE = re.compile(
    r"\b(?:Fig(?:s|ure(?:s)?)?\.?)\s+(?!S\d)(\d+)",
    re.IGNORECASE,
)
TABLE_REF_RE = re.compile(
    r"\bTables?\s+(?!S\d)(\d+)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Figure / table number-list expanders (B3-st)
# ---------------------------------------------------------------------------
# These patterns capture the tail of a figure/table reference after the lead
# token has been consumed (e.g. after "Figure", "Figs", "Table", "Tables").
# They are used by extract_fig_refs / extract_table_refs below.

# "and N", ", N", "- N", "– N" (en-dash) continuations.  Group 1 is the
# continuation number.  The trailing word is captured by a NON-consuming
# lookahead (group 2) so it does NOT eat the separator of the *next*
# continuation — capturing it for real broke "1, 2 and 3" (the "and" after "2"
# got consumed, hiding the "and 3" continuation).  The lookahead lets the
# expander tell a bare figure number from a quantity modifying a noun
# ("23 patients", "100 cells") without disturbing the scan.
_NUM_CONT_RE = re.compile(
    r"(?:,|\band\b|[-–])\s*(\d+)(?=\s*(?P<word>[A-Za-z]+)|)", re.IGNORECASE
)

# Words that may follow a figure number and still leave it a figure number
# (connectives joining further figure/table references), as opposed to a noun
# that turns the number into a quantity.  "Figures 1 and 2 and Table 1" keeps 2
# because "and" is a connective; "Figure 1 and 23 patients" drops 23 because
# "patients" is a noun.
_REF_CONNECTIVES = frozenset({
    "and", "to", "through", "or",
    "fig", "figs", "figure", "figures",
    "table", "tables", "panel", "panels",
})


def _expand_ref_numbers(text: str) -> set:
    """Given text that starts with a number possibly followed by a number list/range,
    return the set of all integer values referenced.

    A continuation introduced by ``,`` / ``and`` / ``-`` is only treated as a
    figure-number continuation when the number is a BARE figure number — i.e.
    NOT immediately followed by an alphabetic *noun* word.  This stops prose
    quantities from leaking in ("Figure 1 and 23 patients" → ``{1}``, "Figure 1,
    100 cells" → ``{1}``) while still expanding genuine multi-number callouts
    ("Figures 1 and 2", "Figs 1-3").  A connective word ("and"/"to") or a
    fig/table token following the number does NOT disqualify it (so
    "Figures 1 and 2 and Table 1" keeps ``2``).

    The FIRST number (group(1) of the lead pattern) is always a figure number —
    the lead token ("Figure"/"Figs") guarantees it — so it is added
    unconditionally.

    Examples::

        "1 and 2"            → {1, 2}
        "1, 2 and 3"         → {1, 2, 3}
        "1-3"                → {1, 2, 3}
        "4"                  → {4}
        "1 and 23 patients"  → {1}
        "1, 100 cells"       → {1}
    """
    nums: set = set()
    # Find the first number
    first_m = re.match(r"\s*(\d+)", text)
    if not first_m:
        return nums
    first = int(first_m.group(1))
    nums.add(first)
    # Scan continuations
    prev = first
    for m in _NUM_CONT_RE.finditer(text):
        val = int(m.group(1))
        trailing_word = (m.group("word") or "").lower()
        # A continuation number immediately followed by a noun (a non-connective
        # alphabetic word) is a quantity, not a figure number — skip it (and do
        # not advance ``prev`` through it, so a later genuine continuation still
        # ranges/lists from the last real figure number).
        if trailing_word and trailing_word not in _REF_CONNECTIVES:
            continue
        # Determine if this is a range continuation (dash/en-dash) or a list item
        sep = m.group(0).lstrip()
        if sep and sep[0] in "-–":
            # Range: expand from prev+1 to val
            lo, hi = (min(prev, val), max(prev, val))
            nums.update(range(lo, hi + 1))
        else:
            nums.add(val)
        prev = val
    return nums


# Lead-token pattern for fig references.  The captured number group is a first
# number followed by any run of "<sep> <number>" continuations (sep = comma /
# "and" / "to" / "through" / "or" / hyphen / en-dash), and THEN — crucially —
# any alphabetic word that immediately follows the final number.  That trailing
# word is what lets ``_expand_ref_numbers`` distinguish a bare figure number
# ("Figures 1 and 2") from a quantity modifying a noun ("Figure 1 and 23
# patients").  Previously the capture used a flat ``[\d,\s\-–andAND]`` class
# that stopped at the first non-class letter, hiding "patients"/"cells" from the
# expander and letting the adjacent quantity leak in.
_REF_NUM_TAIL = (
    r"\d"                                          # first figure number
    r"(?:\s*(?:,|\band\b|\bto\b|\bthrough\b|\bor\b|[-–])\s*\d+)*"  # continuations
    r"\s*[A-Za-z]*"                                # optional trailing word
)
_FIG_REF_LEAD_RE = re.compile(
    r"\b(?:Fig(?:s|ure(?:s)?)?\.?)\s+(?!S\d)(" + _REF_NUM_TAIL + r")",
    re.IGNORECASE,
)
_TABLE_REF_LEAD_RE = re.compile(
    r"\bTables?\s+(?!S\d)(" + _REF_NUM_TAIL + r")",
    re.IGNORECASE,
)


def extract_fig_refs(text: str) -> set:
    """Return the set of all figure numbers referenced in *text*.

    Expands lists ("Figures 1 and 2" → {1, 2}) and ranges ("Figs 1-3" → {1,2,3}).
    """
    nums: set = set()
    for m in _FIG_REF_LEAD_RE.finditer(text or ""):
        nums |= _expand_ref_numbers(m.group(1))
    return nums


def extract_table_refs(text: str) -> set:
    """Return the set of all table numbers referenced in *text*.

    Expands lists ("Tables 1 and 2" → {1, 2}) and ranges ("Tables 1-3" → {1,2,3}).
    """
    nums: set = set()
    for m in _TABLE_REF_LEAD_RE.finditer(text or ""):
        nums |= _expand_ref_numbers(m.group(1))
    return nums


# ---------------------------------------------------------------------------
# Figure / table legend lines (paragraph starts with label)
# ---------------------------------------------------------------------------
FIG_LEGEND_LINE_RE = re.compile(
    r"^\s*(?:Figure|Fig\.?)\s+(?!S\d)(\d+)\.",
    re.IGNORECASE,
)
TABLE_LEGEND_LINE_RE = re.compile(
    r"^\s*Table\s+(?!S\d)(\d+)\.",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Supplementary figure / table references
# ---------------------------------------------------------------------------
SUPPL_FIG_REF_RE = re.compile(
    r"\b(?:Supplementary\s+|Suppl\.\s*)?(?:Figure|Fig\.?)\s+(S)(\d+)\b"
    r"|\b(?:Supplementary\s+|Suppl\.\s+)(?:Figure|Fig\.?)\s+(\d+)\b",
    re.IGNORECASE,
)
SUPPL_TABLE_REF_RE = re.compile(
    r"\bTable\s+(S)(\d+)\b"
    r"|\b(?:Supplementary\s+|Suppl\.\s+)Table\s+(\d+)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Supplementary legend lines
# ---------------------------------------------------------------------------
SUPPL_FIG_LEGEND_LINE_RE = re.compile(
    r"^\s*(?:Figure|Fig\.?)\s+(S)(\d+)\."
    r"|^\s*(?:Supplementary\s+|Suppl\.\s+)(?:Figure|Fig\.?)\s+(\d+)\.",
    re.IGNORECASE,
)
SUPPL_TABLE_LEGEND_LINE_RE = re.compile(
    r"^\s*Table\s+(S)(\d+)\."
    r"|^\s*(?:Supplementary\s+|Suppl\.\s+)Table\s+(\d+)\.",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Numbered reference-list enumerator (line-leading)
# ---------------------------------------------------------------------------
# Matches the enumerator at the start of a numbered bibliography entry, in every
# common style:
#     "1. ..."   "1) ..."   "[1] ..."   "[ 1 ] ..."   "(1) ..."   "( 1 ) ..."
#     "(1). ..."
# Requires a following whitespace + non-space character (an actual entry body),
# so a bare "1." on its own line does not match.
#
# Recall-superset of every legacy copy this replaces:
#   * journalprofile._NUMBERED_REF_RE  r"^\s*(?:\[\s*\d+\s*\]|\d+[.)])\s+\S"
#       — MISSED "(1)"-style enumerators, undercounting parenthesised reference
#         lists (the bug this fix closes).
#   * citeconvert._NUMBERED_REF_RE     r"^\s*(\[\d+\]|\(?\d+\)?[.)])\s+\S"
#   * refextract._NUMBERING_RE         r"^\s*(\[\d+\]|\(?\d+\)\.?|\d+\.)\s+\S"
#   * refextract._ENUM_RE              r"^\s*(\[\d+\]|\(\d+\)|\d+[.)])\s+\S"
# Verified to match every string each of those matched, plus the "(1)" form and
# the spaced bracket/paren variants none of them all covered.
NUMBERED_REF_RE = re.compile(
    r"^\s*(?:\[\s*\d+\s*\]|\(\s*\d+\s*\)|\(?\d+\)?[.)]|\d+[.)])\s+\S"
)

# ---------------------------------------------------------------------------
# DOI extraction
# ---------------------------------------------------------------------------
# Two shared DOI patterns, used for DIFFERENT jobs:
#
#   * DOI_BARE_RE — the bare-DOI body only, with the conservative character class
#     ``[-._;()/:A-Za-z0-9]+``.  This is the EXACT (byte-identical) pattern that
#     ``citeconvert._DOI_RE`` and ``refextract._DOI_RE`` historically open-coded;
#     they now alias this constant (a perfect single-source — no behaviour change).
#     It deliberately does NOT swallow quotes/brackets, so a DOI embedded in
#     ``[10.x/y]`` stops before the ``]``.
#
#   * DOI_RE — a recall-SUPERSET extractor that matches a DOI whether or not it
#     carries a ``doi:`` / ``https://doi.org/`` / ``dx.doi.org/`` prefix, capturing
#     the bare DOI in group(1).  Its body class ``[^\s"<>']+`` is broader (it
#     keeps brackets), so a trailing-punctuation strip is applied by
#     :func:`extract_dois`.  Use ``DOI_RE`` / ``extract_dois`` for general DOI
#     mining; the per-consumer copies in refstyle / refresolve / biocheck stay
#     local because each relies on its own distinct boundary behaviour (verified
#     against their tests — folding them in would change what they capture).
DOI_BARE_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", re.IGNORECASE)

DOI_RE = re.compile(
    r"(?:doi:\s*|https?://(?:dx\.)?doi\.org/)?"   # optional prefix (ignored)
    r"(10\.\d{4,9}/[^\s\"<>']+)",                 # the DOI itself -> group(1)
    re.IGNORECASE,
)

# Trailing punctuation that is part of the surrounding prose, not the DOI.
_DOI_TRAILING_PUNCT_RE = re.compile(r"[.,);:\]]+$")


def extract_dois(text: str) -> list[str]:
    """Return the de-duplicated DOIs in *text*, prefixed or bare.

    Each DOI is returned WITHOUT any ``doi:`` / URL prefix and with trailing
    prose punctuation (``. , ) ; : ]``) stripped.  De-duplication is
    case-insensitive but the first-seen surface form is preserved; order follows
    first appearance.  Returns ``[]`` for empty / ``None`` input.
    """
    out: list[str] = []
    seen: set = set()
    for m in DOI_RE.finditer(text or ""):
        doi = _DOI_TRAILING_PUNCT_RE.sub("", m.group(1))
        key = doi.lower()
        if doi and key not in seen:
            out.append(doi)
            seen.add(key)
    return out


# ---------------------------------------------------------------------------
# PMID extraction
# ---------------------------------------------------------------------------
# Matches "PMID: 12345" and the longer "PubMed PMID: 12345" form, capturing the
# digits in group(1).  Recall-superset of the biocheck local copy
# (``(?:PubMed\s+PMID:?\s*|PMID:\s*)(\d+)``): the optional ``:`` and the optional
# whitespace make it match every string that copy matched, plus a bare
# "PMID 12345" (no colon).
PMID_RE = re.compile(r"(?:PubMed\s+PMID|PMID)\s*:?\s*(\d+)", re.IGNORECASE)


def extract_pmids(text: str) -> list[str]:
    """Return the de-duplicated PMIDs (bare digit strings) in *text*.

    Recognises ``PMID: 12345``, ``PMID 12345``, and ``PubMed PMID: 12345``.
    Order follows first appearance; returns ``[]`` for empty / ``None`` input.
    """
    out: list[str] = []
    seen: set = set()
    for m in PMID_RE.finditer(text or ""):
        pmid = m.group(1)
        if pmid not in seen:
            out.append(pmid)
            seen.add(pmid)
    return out


# ---------------------------------------------------------------------------
# PMCID extraction
# ---------------------------------------------------------------------------
# A PubMed Central ID is the literal token ``PMC`` followed by digits.  Unlike a
# bare PMID, the ``PMC`` prefix is REQUIRED — this is what makes PMCID mining
# safe to run over arbitrary citation prose (a stray digit-run, e.g. the tail of
# a DOI ``10.x/abc12345.x``, is never mistaken for a PMCID).  Single owner of the
# PMCID regex, consolidated from the biosketch / biocheck / biotailor / biorender
# private ``PMC\d+`` copies.
PMCID_RE = re.compile(r"PMC\d+", re.IGNORECASE)


def extract_pmcids(text: str) -> list[str]:
    """Return the de-duplicated PMCIDs (``PMC`` + digits) in *text*.

    The surface form of the first occurrence is preserved; de-duplication is
    case-insensitive.  Order follows first appearance; returns ``[]`` for empty /
    ``None`` input.
    """
    out: list[str] = []
    seen: set = set()
    for m in PMCID_RE.finditer(text or ""):
        pmcid = m.group(0)
        key = pmcid.lower()
        if key not in seen:
            out.append(pmcid)
            seen.add(key)
    return out
