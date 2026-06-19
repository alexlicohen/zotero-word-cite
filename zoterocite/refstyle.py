"""Reference-list style-conformance checker (item #8 "refstyle").

A *highlighter*, never a rewriter: given a journal's ``reference_rules`` (the
``reference_rules`` object in a journal profile, e.g. Brain's AMA/Vancouver
numeric style) and a per-profile attribution string, it inspects the parsed
reference list of a manuscript and emits :class:`~zoterocite.findings.Finding`
objects flagging entries that look inconsistent with the journal's reference
style.  Every finding QUOTES the offending entry text (truncated) so a human
can verify the call — nothing is auto-corrected.

It reuses :func:`zoterocite.refextract.extract_references` to obtain the
per-entry reflist (``[{"index", "text", "numbering", ...}]``) — it never
re-parses the References block itself.

Checks implemented (ratified scope)
-----------------------------------
RELIABLE (WARN):
    * ``RS-numbering`` — the list's numbering style disagrees with the
      journal's (``numbering`` = ``"numbered"`` vs ``"author-date"``).
    * ``RS-disallowed-field`` — entries carry a field the formatted list omits
      (PMID / PMCID / accession number / "Accessed" URL); one finding per
      offending field class.

CONSERVATIVE (INFO, entry quoted):
    * ``RS-et-al`` — an entry lists >= ``et_al.min_authors`` author groups with
      no "et al." (the journal abbreviates long author lists).
    * ``RS-doi-coverage`` — DOIs are expected for online articles but almost no
      entry carries one.

Design contract
---------------
* Degrade to SILENCE on any malformed input — a missing References section, an
  empty/absent ``rules`` dict, an unreadable file — exactly as
  :func:`zoterocite.journalprofile.check_journal_compliance` does.  This module
  never raises out of :func:`check_reference_style`.
* Conservative by construction: the author-group parse and the disallowed-field
  regexes only fire on clear matches, preferring a false negative (silence) to a
  false positive (a wrong "verify this" on a correctly-formatted entry).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .findings import Finding
from .refextract import extract_references

# Truncation length for a quoted reference entry in a finding message.
_QUOTE_MAX = 120

# Minimum entries before RS-numbering will fire (a 1-2 entry list tells us
# nothing reliable about the numbering style).
_MIN_NUMBERING_ENTRIES = 3

# Fraction of entries that must disagree with the expected numbering style
# before RS-numbering fires.
_NUMBERING_THRESHOLD = 0.60

# Minimum entries before RS-doi-coverage will fire (a small list with no DOIs
# tells us nothing — print/older refs are legitimately DOI-less).
_MIN_DOI_ENTRIES = 10

# DOI fraction below which RS-doi-coverage fires.
_DOI_COVERAGE_THRESHOLD = 0.20

# ---------------------------------------------------------------------------
# Disallowed-field detection
# ---------------------------------------------------------------------------
#
# Each disallowed-list token maps to (human label, detector).  The detectors
# are deliberately tight — they fire only on clear matches:
#   * pmid         -> a "PMID" token or "PMID: <digits>"
#   * pmcid        -> a "PMCID" token (require the C so a bare "PMID" is NOT
#                     double-counted under both pmid and pmcid)
#   * accession    -> the word "accession"
#   * url_accessed -> an "Accessed" token (the AMA "Accessed <date>." tail),
#                     typically alongside a date or URL.
_PMID_RE = re.compile(r"\bPMID\b|PMID:\s*\d+", re.IGNORECASE)
_PMCID_RE = re.compile(r"\bPMCID\b|PMCID:\s*PMC?\d+", re.IGNORECASE)
_ACCESSION_RE = re.compile(r"\baccession\b", re.IGNORECASE)
# "Accessed" near a date (month name or a 4-digit year) OR an http(s) URL in the
# same entry — the AMA "Accessed Month DD, YYYY." retrieval tail.
_ACCESSED_RE = re.compile(r"\bAccessed\b", re.IGNORECASE)
_URL_RE = re.compile(r"https?://", re.IGNORECASE)
_DATEISH_RE = re.compile(
    r"\b(?:19|20)\d{2}\b"
    r"|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\b",
    re.IGNORECASE,
)


def _detect_pmid(text: str) -> bool:
    return bool(_PMID_RE.search(text))


def _detect_pmcid(text: str) -> bool:
    return bool(_PMCID_RE.search(text))


def _detect_accession(text: str) -> bool:
    return bool(_ACCESSION_RE.search(text))


def _detect_url_accessed(text: str) -> bool:
    """An 'Accessed' retrieval tail: 'Accessed' near a date, or with a URL."""
    if not _ACCESSED_RE.search(text):
        return False
    return bool(_DATEISH_RE.search(text) or _URL_RE.search(text))


# token -> (human label for the message, detector predicate)
_DISALLOWED_DETECTORS: Dict[str, tuple] = {
    "pmid": ("a PMID", _detect_pmid),
    "pmcid": ("a PMCID", _detect_pmcid),
    "accession": ("an accession number", _detect_accession),
    "url_accessed": ("an 'Accessed' retrieval URL/date", _detect_url_accessed),
}

# ---------------------------------------------------------------------------
# DOI detection
# ---------------------------------------------------------------------------
_DOI_RE = re.compile(r"10\.\d{4,9}/\S+")


# ---------------------------------------------------------------------------
# Author-group counting (RS-et-al) — intentionally conservative
# ---------------------------------------------------------------------------
#
# An author group at the START of a Vancouver/AMA entry looks like
# "Surname AB, " or "Surname A, " — a capitalised surname token followed by 1-4
# uppercase initials.  We count consecutive such groups BEFORE the title (i.e.
# before the first sentence-ending period that ends the author list).  Because
# this is a "verify this" hint, we only fire when we can confidently count the
# groups; on any ambiguity we count fewer (or zero) and stay silent.
#
# Strip any leading numbering token ("1.", "[1]", "(1)") first so the first
# author isn't mis-parsed.
_LEADING_NUM_RE = re.compile(r"^\s*(?:\[\s*\d+\s*\]|\(?\d+\)\.?|\d+\.)\s+")

# One author group: a capitalised surname (letters, optional hyphen/apostrophe),
# a space, then 1-4 uppercase initials (each optionally followed by a period,
# e.g. "AB", "A.B.", "A"), terminated by a comma OR the end of the author list.
# We anchor at the cursor and walk groups one at a time.
# NOTE: no leading ``^`` anchor — :meth:`re.Pattern.match` already anchors at
# the supplied ``pos`` (and ``^`` would only match absolute position 0, breaking
# the walk after the first group).  ``\s*`` consumes the inter-author space.
# A nobiliary particle ("van", "de", ...) may PRECEDE the surname (lowercase) or
# sit between surname words; the surname token itself may be capitalised.  The
# group is terminated by group(1):
#   ","  -> more authors follow (continue the walk past the comma)
#   "."  -> the author-list-ending period before the title (count, then stop)
#   ""   -> end of string (count, then stop)
_AUTHOR_GROUP_RE = re.compile(
    r"\s*"
    r"(?:(?:van|von|de|der|den|del|della|di|da|la|le|du|den)\s+)*"  # leading particle(s)
    r"[A-Z][A-Za-z'\-]+"                 # surname
    r"(?:\s+(?:van|von|de|der|den|del|della|di|da|la|le|du)\b)*"    # interior particle(s)
    r"\s+"
    r"(?:[A-Z]\.?){1,4}"                 # 1-4 uppercase initials (e.g. AB, A.B., A)
    r"\s*"
    r"(,|\.|$)"                          # comma (more), period (last), or end
)


def _count_author_groups(text: str) -> int:
    """Conservatively count leading 'Surname AB,' author groups in an entry.

    Strips a leading numbering token, then walks comma-separated author groups
    from the start.  The final group before the title is terminated by a period
    (the author-list-ending period) or end-of-string; either way it is counted,
    then the walk stops.  A group that does not match (typically the title)
    stops the walk.  Returns the count (0 when the entry does not begin with a
    recognisable author list).
    """
    # Drop a leading numbering token if present.
    s = _LEADING_NUM_RE.sub("", text).strip()
    count = 0
    cursor = 0
    n = len(s)
    while cursor < n:
        m = _AUTHOR_GROUP_RE.match(s, cursor)
        if not m:
            break
        count += 1
        end = m.end()
        # A comma means more authors follow; anything else (period / end of
        # string) is the final author group -> count it and stop.
        if m.group(1) != ",":
            break
        # Guard against zero-width progress (defensive).
        if end <= cursor:
            break
        cursor = end
    return count


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _quote(text: str) -> str:
    """Truncate an entry to ~_QUOTE_MAX chars for quoting in a finding."""
    t = " ".join((text or "").split())  # collapse embedded whitespace/newlines
    if len(t) > _QUOTE_MAX:
        return t[: _QUOTE_MAX - 1].rstrip() + "…"  # ellipsis
    return t


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_reference_style(
    path,
    *,
    rules: Dict[str, Any],
    source: str,
) -> List[Finding]:
    """Flag reference-list entries that disagree with the journal's style.

    Args:
        path: path to the manuscript ``.docx``.
        rules: the journal profile's ``reference_rules`` dict (numbering,
            in_text, et_al, disallow_in_list, doi_for_online, ...).
        source: per-profile attribution string stamped on every finding.

    Returns:
        A list of :class:`~zoterocite.findings.Finding`.  Every finding quotes
        the offending entry so a human can verify it.  Returns ``[]`` (never
        raises) on any malformed input, missing rules, or absent References
        section — degrade to silence, exactly like ``check_journal_compliance``.
    """
    findings: List[Finding] = []

    # Guard: no usable rules -> nothing to check.
    if not isinstance(rules, dict) or not rules:
        return findings

    # Pull the parsed reference list.  Any failure -> silence.
    try:
        reflist = extract_references(path).get("reflist", []) or []
    except Exception:
        return findings
    if not isinstance(reflist, list) or not reflist:
        return findings

    # Normalise entries to (text, numbering) — skip anything malformed.
    entries: List[Dict[str, Any]] = [
        e for e in reflist
        if isinstance(e, dict) and isinstance(e.get("text"), str) and e["text"].strip()
    ]
    if not entries:
        return findings

    findings.extend(_check_numbering(entries, rules, source))
    findings.extend(_check_disallowed_fields(entries, rules, source))
    findings.extend(_check_et_al(entries, rules, source))
    findings.extend(_check_doi_coverage(entries, rules, source))
    return findings


# ---------------------------------------------------------------------------
# RS-numbering
# ---------------------------------------------------------------------------

def _check_numbering(
    entries: List[Dict[str, Any]],
    rules: Dict[str, Any],
    source: str,
) -> List[Finding]:
    """WARN when the list's numbering style disagrees with the journal's.

    ``rules["numbering"]`` is ``"numbered"`` (Vancouver/AMA) or ``"author-date"``.
    An entry is "numbered" iff ``extract_references`` detected a leading number
    (its ``numbering`` field is not None).  Fires only with >= 3 entries and
    when >= 60% of entries are on the WRONG side.
    """
    style = rules.get("numbering")
    if style not in ("numbered", "author-date"):
        return []
    total = len(entries)
    if total < _MIN_NUMBERING_ENTRIES:
        return []

    numbered = sum(1 for e in entries if e.get("numbering") is not None)
    not_numbered = total - numbered

    findings: List[Finding] = []
    if style == "numbered":
        # Expect numbered; flag if predominantly NOT numbered.
        if not_numbered / total >= _NUMBERING_THRESHOLD:
            findings.append(Finding(
                check="RS-numbering",
                severity="WARN",
                message=(
                    f"{not_numbered} of {total} reference entries are not "
                    f"numbered; this journal uses a numbered (Vancouver/AMA) "
                    f"reference list. Verify the list is sequentially numbered."
                ),
                source=source,
            ))
    else:  # author-date
        if numbered / total >= _NUMBERING_THRESHOLD:
            findings.append(Finding(
                check="RS-numbering",
                severity="WARN",
                message=(
                    f"{numbered} of {total} reference entries are numbered; "
                    f"this journal uses an author-date reference list. Verify "
                    f"the list is not numbered."
                ),
                source=source,
            ))
    return findings


# ---------------------------------------------------------------------------
# RS-disallowed-field
# ---------------------------------------------------------------------------

def _check_disallowed_fields(
    entries: List[Dict[str, Any]],
    rules: Dict[str, Any],
    source: str,
) -> List[Finding]:
    """One WARN per disallowed-field class present in the list.

    ``rules["disallow_in_list"]`` is a list of token classes (pmid / pmcid /
    accession / url_accessed) the formatted list must NOT carry.  For each class
    found, emit a single WARN listing up to the first 3 offending entries and
    the total count.  Conservative: only clear matches fire.
    """
    disallow = rules.get("disallow_in_list")
    if not isinstance(disallow, (list, tuple)) or not disallow:
        return []

    findings: List[Finding] = []
    for token in disallow:
        spec = _DISALLOWED_DETECTORS.get(str(token).lower())
        if spec is None:
            continue
        label, detector = spec
        offenders = [e for e in entries if detector(e["text"])]
        if not offenders:
            continue
        examples = "; ".join(f"'{_quote(e['text'])}'" for e in offenders[:3])
        n = len(offenders)
        plural = "entry" if n == 1 else "entries"
        findings.append(Finding(
            check="RS-disallowed-field",
            severity="WARN",
            message=(
                f"{n} reference {plural} contain {label}, which AMA/Vancouver "
                f"style omits from the formatted reference list. Verify and "
                f"remove. e.g.: {examples}"
            ),
            source=source,
        ))
    return findings


# ---------------------------------------------------------------------------
# RS-et-al
# ---------------------------------------------------------------------------

def _check_et_al(
    entries: List[Dict[str, Any]],
    rules: Dict[str, Any],
    source: str,
) -> List[Finding]:
    """INFO per entry that lists >= min_authors author groups with no 'et al.'.

    Only fires when ``rules["et_al"]["min_authors"]`` is present.  For each
    entry, conservatively counts leading 'Surname AB,' author groups; if the
    count reaches the threshold AND the entry has no "et al", emit an INFO that
    quotes the entry.  The parse is deliberately conservative — it never fires
    on an entry it cannot confidently count.
    """
    et_al = rules.get("et_al")
    if not isinstance(et_al, dict):
        return []
    min_authors = et_al.get("min_authors")
    if not isinstance(min_authors, int) or min_authors <= 0:
        return []
    use_first = et_al.get("use_first")

    findings: List[Finding] = []
    for e in entries:
        text = e["text"]
        if re.search(r"et\s+al", text, re.IGNORECASE):
            continue
        n_authors = _count_author_groups(text)
        if n_authors >= min_authors:
            first_note = (
                f"the first {use_first} then 'et al.'"
                if isinstance(use_first, int) and use_first > 0
                else "the first few then 'et al.'"
            )
            findings.append(Finding(
                check="RS-et-al",
                severity="INFO",
                message=(
                    f"Entry lists >={min_authors} authors with no 'et al.'; "
                    f"this journal lists {first_note} for papers with more than "
                    f"{min_authors - 1} authors. Verify: '{_quote(text)}'"
                ),
                source=source,
            ))
    return findings


# ---------------------------------------------------------------------------
# RS-doi-coverage
# ---------------------------------------------------------------------------

def _check_doi_coverage(
    entries: List[Dict[str, Any]],
    rules: Dict[str, Any],
    source: str,
) -> List[Finding]:
    """One INFO when DOIs are expected but almost no entry carries one.

    Only fires when ``rules["doi_for_online"]`` is true AND there are >= 10
    entries (a small list tells us nothing — older/print refs are legitimately
    DOI-less).  Fires when the DOI-bearing fraction is < 0.20.
    """
    if not rules.get("doi_for_online"):
        return []
    total = len(entries)
    if total < _MIN_DOI_ENTRIES:
        return []
    with_doi = sum(1 for e in entries if _DOI_RE.search(e["text"]))
    if total <= 0:
        return []
    if with_doi / total < _DOI_COVERAGE_THRESHOLD:
        return [Finding(
            check="RS-doi-coverage",
            severity="INFO",
            message=(
                f"Only {with_doi} of {total} reference entries include a DOI; "
                f"this journal asks for DOIs on online articles (verify "
                f"older/print refs are genuinely DOI-less)."
            ),
            source=source,
        )]
    return []
